"""Query planning: spend as few paid Brave queries as possible.

The economics this is built around:

    A paid search query's job is to find a DOMAIN.
    A free sitemap scan then enumerates every one of our products on it.

So there is no reason to search per-product across a 300-SKU catalog. A dealer
carrying the line carries many SKUs, so a rotating sample surfaces them, and
from then on that domain is scanned for free. Five rules follow:

 1. **UPC only, where a UPC exists.** One query per product instead of two, and
    a bare UPC is the highest-precision query available. A title query is only
    worth paying for when there is no UPC to search.

 2. **Don't search a domain we can already scan.** A product whose every known
    seller is sitemap-covered has nothing left for search to find.

 3. **Exponential backoff per product.** A SKU that has surfaced nothing new
    across several sweeps is unlikely to next sweep either, so it earns a longer
    interval: 3, 6, 12 ... capped at 60 days.

 4. **Never repeat a query inside the cache window.** Brave charges for a query
    that returns zero results, so paying twice for the same answer is pure loss.

 5. **Keep a reserve.** The scheduled sweep stops before exhausting the monthly
    cap, so an ad-hoc investigation is still possible late in the month.
"""
from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from typing import Optional

from app import db

log = logging.getLogger(__name__)

BACKOFF_BASE_DAYS = 3
BACKOFF_MAX_DAYS = 60
QUERY_CACHE_DAYS = 21
# Fraction of the monthly cap the scheduled sweep will not touch.
BUDGET_RESERVE_FRACTION = 0.20


@dataclass
class PlannedQuery:
    product_id: int
    product: dict
    kind: str            # upc | title
    query: str
    query_hash: str
    reason: str


def query_hash(query: str, provider: str = "brave") -> str:
    return hashlib.sha256(f"{provider}::{query}".encode("utf-8")).hexdigest()


def build_query(product: dict) -> Optional[tuple[str, str, str]]:
    """(kind, query, reason) -- at most ONE paid query per product.

    UPC is preferred and a title query is the fallback, never an addition. This
    halves spend versus querying both, and drops the noisier of the two.
    """
    upc = (product.get("upc") or "").strip()
    if upc:
        return "upc", f'"{upc}"', "upc exact"
    title = (product.get("title") or "").strip()
    if title:
        # Quoted phrase: an unquoted title matches every holster on the web.
        return "title", f'"{title}"', "no upc, title phrase"
    return None


async def recently_queried(qhash: str, days: int = QUERY_CACHE_DAYS) -> bool:
    hit = await db.fetchval(
        "SELECT 1 FROM search_query_log "
        "WHERE query_hash = $1 AND ran_at > now() - ($2 || ' days')::interval "
        "LIMIT 1",
        qhash, str(days),
    )
    return bool(hit)


async def spendable(monthly_cap: int, used: int) -> int:
    """Queries the scheduled sweep may spend, keeping the reserve intact."""
    usable = int(monthly_cap * (1.0 - BUDGET_RESERVE_FRACTION))
    return max(0, usable - used)


async def plan(max_queries: int) -> list[PlannedQuery]:
    """Choose which products to spend queries on this run."""
    if max_queries <= 0:
        return []

    rows = await db.fetch(
        """
        SELECT p.id, p.sku, p.title, p.upc, p.description, p.map_price,
               c.consecutive_empty, c.last_searched_at, c.next_eligible_at,
               -- how many sellers we know for this product...
               (SELECT count(*) FROM listings l
                 WHERE l.product_id = p.id AND l.status = 'confirmed') AS confirmed,
               -- ...and how many of those we can already scan for free
               (SELECT count(*) FROM listings l
                  JOIN sellers s ON s.id = l.seller_id
                 WHERE l.product_id = p.id AND l.status = 'confirmed'
                   AND s.sitemap_ok) AS sitemap_covered
          FROM products p
          LEFT JOIN discovery_cursor c ON c.product_id = p.id
         WHERE p.active
           AND (c.next_eligible_at IS NULL OR c.next_eligible_at <= now())
         ORDER BY
           -- never-searched first, then longest-idle
           c.last_searched_at ASC NULLS FIRST,
           -- prefer products with a UPC: cheaper and far more precise
           (p.upc IS NULL) ASC,
           p.id ASC
         LIMIT $1
        """,
        max_queries * 4,   # over-fetch, since some will be filtered below
    )

    planned: list[PlannedQuery] = []
    skipped_cached = skipped_covered = 0

    for r in rows:
        if len(planned) >= max_queries:
            break
        product = dict(r)

        # Rule 2: every known seller is already free to scan.
        if product["confirmed"] > 0 and product["confirmed"] == product["sitemap_covered"]:
            skipped_covered += 1
            continue

        built = build_query(product)
        if built is None:
            continue
        kind, q, reason = built
        qh = query_hash(q)

        # Rule 4: already paid for this answer recently.
        if await recently_queried(qh):
            skipped_cached += 1
            continue

        planned.append(PlannedQuery(product["id"], product, kind, q, qh, reason))

    if skipped_cached or skipped_covered:
        log.info("planner: %d queries planned; skipped %d cached, %d sitemap-covered",
                 len(planned), skipped_cached, skipped_covered)
    return planned


async def record_outcome(pq: PlannedQuery, result_count: int,
                         new_listings: int, new_domains: int) -> None:
    """Log the spend and set this product's next eligibility."""
    await db.execute(
        """
        INSERT INTO search_query_log
            (query_hash, query, provider, product_id, result_count,
             new_listings, new_domains)
        VALUES ($1,$2,'brave',$3,$4,$5,$6)
        """,
        pq.query_hash, pq.query, pq.product_id, result_count,
        new_listings, new_domains,
    )

    if new_listings > 0:
        # Productive: search again on the base interval and reset backoff.
        await db.execute(
            """
            INSERT INTO discovery_cursor
                (product_id, last_searched_at, next_eligible_at,
                 consecutive_empty, last_new_listing_at, total_queries)
            VALUES ($1, now(), now() + ($2 || ' days')::interval, 0, now(), 1)
            ON CONFLICT (product_id) DO UPDATE SET
                last_searched_at = now(),
                next_eligible_at = now() + ($2 || ' days')::interval,
                consecutive_empty = 0,
                last_new_listing_at = now(),
                total_queries = discovery_cursor.total_queries + 1
            """,
            pq.product_id, str(BACKOFF_BASE_DAYS),
        )
        return

    # Rule 3: nothing new -> back off, doubling each time, capped.
    empty = (pq.product.get("consecutive_empty") or 0) + 1
    delay = min(BACKOFF_BASE_DAYS * (2 ** (empty - 1)), BACKOFF_MAX_DAYS)
    await db.execute(
        """
        INSERT INTO discovery_cursor
            (product_id, last_searched_at, next_eligible_at,
             consecutive_empty, total_queries)
        VALUES ($1, now(), now() + ($2 || ' days')::interval, $3, 1)
        ON CONFLICT (product_id) DO UPDATE SET
            last_searched_at = now(),
            next_eligible_at = now() + ($2 || ' days')::interval,
            consecutive_empty = $3,
            total_queries = discovery_cursor.total_queries + 1
        """,
        pq.product_id, str(delay), empty,
    )


# ---------------------------------------------------------------------------
# recheck cadence -- the crawl-side equivalent
# ---------------------------------------------------------------------------
# A listing whose price has not moved in weeks does not need checking nightly.
# One that is currently violating does. These bounds keep an eye on the ones
# that matter while cutting total fetches substantially.
CHECK_MIN_HOURS = 24        # violating, or price just moved
CHECK_MAX_HOURS = 24 * 7    # long-stable
UNCHANGED_BEFORE_BACKOFF = 3


def next_interval_hours(current: int, price_changed: bool,
                        has_open_violation: bool, unchanged_streak: int) -> int:
    """Adaptive recheck interval for one listing."""
    if has_open_violation or price_changed:
        # Anything actively wrong stays on the tightest cadence.
        return CHECK_MIN_HOURS
    if unchanged_streak < UNCHANGED_BEFORE_BACKOFF:
        return max(CHECK_MIN_HOURS, current)
    return min(current * 2, CHECK_MAX_HOURS)
