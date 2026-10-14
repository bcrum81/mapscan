"""arq tasks: nightly recheck, staggered discovery sweep, paste-a-URL domain scan.

arq is only the execution queue. `crawl_jobs` in Postgres is the durable source
of truth, so flushing Redis loses scheduling but never history.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Optional

from arq import cron
from arq.connections import RedisSettings

from app import db, settings_store
from app.config import settings
from worker import discovery, ebay, extract, listing_page, match, planner, violations
from worker.fetch import Fetcher, registrable_domain

logging.basicConfig(
    level=settings.log_level,
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
)
log = logging.getLogger("mapscan.worker")


# ---------------------------------------------------------------------------
# job bookkeeping
# ---------------------------------------------------------------------------
async def _job_start(job_id: int) -> None:
    await db.execute(
        "UPDATE crawl_jobs SET status='running', started_at=now() WHERE id=$1", job_id
    )


async def _job_progress(job_id: int, **fields: Any) -> None:
    await db.execute(
        "UPDATE crawl_jobs SET progress = progress || $2::jsonb WHERE id=$1",
        job_id, json.dumps(fields),
    )


async def _job_done(job_id: int, error: Optional[str] = None,
                    status: Optional[str] = None) -> None:
    """Close a job. `status` overrides the error/no-error default.

    The override exists for 'inconclusive': a run that completed cleanly but
    established nothing is neither 'done' nor 'failed', and calling it either
    misreports what happened.
    """
    await db.execute(
        """
        UPDATE crawl_jobs
           SET status = $2, finished_at = now(), error = $3
         WHERE id = $1
        """,
        job_id, status or ("failed" if error else "done"), error,
    )


async def _seller_id(domain: str) -> int:
    sid = await db.fetchval(
        """
        INSERT INTO sellers (domain) VALUES ($1)
        ON CONFLICT (domain) DO UPDATE SET domain = EXCLUDED.domain
        RETURNING id
        """,
        domain,
    )
    return sid


TIER2_DECAY_HOURS = 48


async def _note_tier1_failure(domain: str) -> None:
    """Promote a domain to tier 2 after repeated blocking, never before."""
    from worker.fetch import TIER1_FAILURES_BEFORE_PROMOTION
    await db.execute(
        """
        UPDATE sellers
           SET tier1_failures = tier1_failures + 1,
               fetch_tier = CASE
                   WHEN tier1_failures + 1 >= $2 THEN 2 ELSE fetch_tier END,
               tier_promoted_at = CASE
                   WHEN tier1_failures + 1 >= $2 AND fetch_tier < 2
                   THEN now() ELSE tier_promoted_at END
         WHERE domain = $1
        """,
        domain, TIER1_FAILURES_BEFORE_PROMOTION,
    )


async def _decay_tier2(domain: str) -> None:
    """Let a tier-2 promotion lapse.

    Bot protection is frequently rate-triggered rather than permanent, so a
    domain that blocked us during one aggressive scan is usually fine days
    later. Without decay, a single bad run retires a dealer for good.
    """
    await db.execute(
        """
        UPDATE sellers
           SET fetch_tier = 1, tier1_failures = 0, tier_promoted_at = NULL
         WHERE domain = $1 AND fetch_tier = 2
           AND tier_promoted_at IS NOT NULL
           AND tier_promoted_at < now() - make_interval(hours => $2)
        """,
        domain, TIER2_DECAY_HOURS,
    )


async def _rule_for(domain: str) -> Optional[dict]:
    row = await db.fetchrow(
        "SELECT price_selector, stock_selector, currency FROM extraction_rules WHERE domain=$1",
        domain,
    )
    return dict(row) if row else None


# ---------------------------------------------------------------------------
# price check for a single confirmed listing
# ---------------------------------------------------------------------------
async def _check_listing(fetcher: Fetcher, listing: dict) -> str:
    listing_id = listing["id"]
    url = listing["url"]
    domain = listing["domain"]
    map_price: Decimal = listing["map_price"]

    result = await fetcher.get(
        url,
        seller_tier=listing["fetch_tier"],
        etag=listing.get("etag"),
        last_modified=listing.get("last_modified"),
    )

    # -- 304: nothing changed. The cheapest possible successful check --------
    if result.not_modified:
        streak = (listing.get("unchanged_streak") or 0) + 1
        interval = planner.next_interval_hours(
            listing.get("check_interval_hours") or 24,
            price_changed=False,
            has_open_violation=bool(listing.get("has_open_violation")),
            unchanged_streak=streak,
        )
        # A 304 means the page is byte-identical, so a price that was below MAP
        # still is. The streak must keep advancing or an unchanging violation
        # would never mature past the debounce.
        was_below = bool(listing.get("has_open_violation")) or bool(
            await db.fetchval(
                "SELECT below_map FROM price_observations WHERE listing_id=$1 "
                "ORDER BY observed_at DESC LIMIT 1", listing_id)
        )
        below_streak = ((listing.get("below_map_streak") or 0) + 1) if was_below else 0
        await db.execute(
            """
            UPDATE listings
               SET last_checked = now(), last_error = NULL, consecutive_failures = 0,
                   unchanged_streak = $2, check_interval_hours = $3,
                   below_map_streak = $4,
                   next_check_at = now() + make_interval(hours => $3)
             WHERE id = $1
            """,
            listing_id, streak, interval, below_streak,
        )
        if was_below:
            row = await db.fetchrow(
                "SELECT price FROM price_observations WHERE listing_id=$1 "
                "ORDER BY observed_at DESC LIMIT 1", listing_id)
            if row and row["price"] is not None:
                await violations.evaluate(
                    listing_id=listing_id, price=row["price"], map_price=map_price,
                    url=url, html=None, fetcher=fetcher, source_tier="304",
                    below_map_streak=below_streak,
                )
        return "not_modified"

    if not result.ok:
        if result.blocked:
            await _note_tier1_failure(domain)
        failures = (listing.get("consecutive_failures") or 0) + 1
        # A page that 404s repeatedly is gone. Retire it rather than fetching a
        # tombstone every night forever.
        retire = result.status == 404 and failures >= 3
        await db.execute(
            """
            UPDATE listings
               SET last_checked = now(), last_error = $2,
                   consecutive_failures = $3,
                   retired_at = CASE WHEN $4 THEN now() ELSE retired_at END,
                   next_check_at = now() + make_interval(hours => LEAST(24 * $3, 168))
             WHERE id = $1
            """,
            listing_id, (result.error or "fetch failed")[:500], failures, retire,
        )
        return "retired" if retire else "fetch_failed"

    rule = await _rule_for(domain)
    ext = await extract.extract_price(result.html, result.final_url, fetcher, rule)

    below = bool(ext.price is not None and ext.price < map_price)

    last = await db.fetchrow(
        """
        SELECT price, observed_at FROM price_observations
        WHERE listing_id = $1 ORDER BY observed_at DESC LIMIT 1
        """,
        listing_id,
    )
    price_changed = last is None or last["price"] != ext.price
    stale = last is not None and last["observed_at"] < datetime.now(timezone.utc) - timedelta(hours=20)

    # Write an observation only on change, or once daily as a heartbeat.
    if price_changed or stale:
        await db.execute(
            """
            INSERT INTO price_observations
                (listing_id, price, currency, in_stock, extraction_tier,
                 fetch_tier, below_map)
            VALUES ($1,$2,$3,$4,$5,$6,$7)
            """,
            listing_id, ext.price, ext.currency, ext.in_stock, ext.tier,
            result.tier, below,
        )

    # Content hash catches "page changed but price did not" -- common on sites
    # that inject a session id or a rotating banner, which defeats ETag.
    content_hash = hashlib.sha256(
        (str(ext.price) + str(ext.in_stock)).encode()
    ).hexdigest()
    unchanged_streak = 0 if price_changed else (listing.get("unchanged_streak") or 0) + 1
    # Consecutive CHECKS below MAP. Independent of whether an observation row
    # was written, which only happens on a price change.
    below_streak = ((listing.get("below_map_streak") or 0) + 1) if below else 0
    interval = planner.next_interval_hours(
        listing.get("check_interval_hours") or 24,
        price_changed=price_changed,
        has_open_violation=bool(listing.get("has_open_violation")) or below,
        unchanged_streak=unchanged_streak,
    )

    await db.execute(
        """
        UPDATE listings
           SET last_checked = now(), last_error = NULL, consecutive_failures = 0,
               etag = $2, last_modified = $3, content_hash = $4,
               unchanged_streak = $5, check_interval_hours = $6,
               below_map_streak = $7,
               next_check_at = now() + make_interval(hours => $6)
         WHERE id = $1
        """,
        listing_id, result.etag, result.last_modified, content_hash,
        unchanged_streak, interval, below_streak,
    )

    outcome = await violations.evaluate(
        listing_id=listing_id, price=ext.price, map_price=map_price,
        url=result.final_url, html=result.html, fetcher=fetcher,
        source_tier=ext.tier, below_map_streak=below_streak,
        policy=listing.get("policy") or "map",
    )
    return outcome or ("below_map" if below else "ok")


# ---------------------------------------------------------------------------
# TASK: nightly recheck of all confirmed listings
# ---------------------------------------------------------------------------
async def recheck_all(ctx: dict, job_row_id: Optional[int] = None) -> dict:
    await db.init_pool()
    if job_row_id is None:
        job_row_id = await db.fetchval(
            "INSERT INTO crawl_jobs (kind, target, status) "
            "VALUES ('recheck','all confirmed listings','running') RETURNING id"
        )
    else:
        await _job_start(job_row_id)

    counts: dict[str, int] = {}
    try:
        # Only what is actually due. Adaptive cadence means a long-stable
        # listing is checked weekly rather than nightly, which is where most of
        # the fetch reduction comes from.
        rows = await db.fetch(
            """
            SELECT l.id, l.url, l.etag, l.last_modified, l.unchanged_streak,
                   l.below_map_streak,
                   l.check_interval_hours, l.consecutive_failures,
                   p.map_price, s.domain, s.fetch_tier, s.policy,
                   EXISTS (SELECT 1 FROM violations v
                            WHERE v.listing_id = l.id AND v.status = 'open')
                     AS has_open_violation
              FROM listings l
              JOIN products p ON p.id = l.product_id
              JOIN sellers  s ON s.id = l.seller_id
             WHERE l.status = 'confirmed'
               AND l.retired_at IS NULL
               AND p.active
               AND NOT s.robots_disallowed
               AND NOT s.excluded
               AND (l.next_check_at IS NULL OR l.next_check_at <= now())
             ORDER BY has_open_violation DESC, l.next_check_at NULLS FIRST
            """
        )
        total = len(rows)
        await _job_progress(job_row_id, total=total, checked=0)
        log.info("recheck: %s confirmed listings", total)

        async with Fetcher() as fetcher:
            for i, row in enumerate(rows, 1):
                try:
                    outcome = await _check_listing(fetcher, dict(row))
                except Exception as exc:
                    log.exception("recheck failed for listing %s", row["id"])
                    outcome = "error"
                counts[outcome] = counts.get(outcome, 0) + 1
                if i % 10 == 0 or i == total:
                    await _job_progress(job_row_id, checked=i, counts=counts)

        await _job_progress(job_row_id, checked=total, counts=counts)
        await _job_done(job_row_id)
    except Exception as exc:
        log.exception("recheck_all failed")
        await _job_done(job_row_id, str(exc)[:1000])
        raise
    return counts


# ---------------------------------------------------------------------------
# TASK: staggered discovery sweep (stays inside the PSE free tier)
# ---------------------------------------------------------------------------
async def discovery_sweep(ctx: dict, job_row_id: Optional[int] = None,
                          max_queries: Optional[int] = None) -> dict:
    """Spend the fewest paid queries that could still surface a NEW dealer.

    See worker/planner.py for the reasoning. In short: one query per product at
    most, UPC-preferred, skipping products whose sellers we can already scan for
    free, with exponential backoff on unproductive SKUs and a reserve kept back
    from the monthly cap.
    """
    await db.init_pool()
    if job_row_id is None:
        job_row_id = await db.fetchval(
            "INSERT INTO crawl_jobs (kind, target, status) "
            "VALUES ('discovery_sweep','planned sweep','running') RETURNING id"
        )
    else:
        await _job_start(job_row_id)

    stats = {"planned": 0, "queries": 0, "hits": 0,
             "new_listings": 0, "new_domains": 0, "skipped": 0}

    try:
        cap = await settings_store.get_int("brave_monthly_cap", 1000)
        used = await discovery.monthly_used()
        allowance = await planner.spendable(cap, used)
        requested = max_queries or await settings_store.get_int(
            "discovery_queries_per_run", 40
        )
        budget = min(requested, allowance)

        await _job_progress(job_row_id, monthly_cap=cap, monthly_used=used,
                            allowance=allowance, budget=budget)

        if budget <= 0:
            await _job_progress(
                job_row_id, phase="done",
                note=f"no query budget: {used}/{cap} used this month "
                     f"({int(planner.BUDGET_RESERVE_FRACTION*100)}% held in reserve)",
                **stats)
            await _job_done(job_row_id)
            return stats

        planned = await planner.plan(budget)
        stats["planned"] = len(planned)
        await _job_progress(job_row_id, **stats)
        log.info("discovery sweep: %d queries planned (budget %d, %d/%d used this month)",
                 len(planned), budget, used, cap)

        async with Fetcher() as fetcher:
            for pq in planned:
                hits, spent = await discovery.search(fetcher, pq.query)
                stats["queries"] += spent
                stats["hits"] += len(hits)

                if spent == 0:
                    # No key, or the cap was hit mid-run. Stop the whole sweep
                    # rather than iterating the rest of the plan for nothing.
                    log.info("discovery halted: no query budget available")
                    await _job_progress(job_row_id, note="halted: no query budget")
                    break

                new_listings = 0
                new_domains = 0
                for hit in hits:
                    created, fresh_domain = await _record_candidate(
                        pq.product, hit.url, hit.title, hit.source
                    )
                    if created:
                        new_listings += 1
                    if fresh_domain:
                        new_domains += 1

                stats["new_listings"] += new_listings
                stats["new_domains"] += new_domains
                await planner.record_outcome(pq, len(hits), new_listings, new_domains)
                await _job_progress(job_row_id, **stats)

        await _job_progress(job_row_id, phase="done", **stats)
        await _job_done(job_row_id)
    except Exception as exc:
        log.exception("discovery_sweep failed")
        await _job_done(job_row_id, str(exc)[:1000])
        raise
    return stats


async def _record_candidate(product: dict, url: str, seen_title: Optional[str],
                            via: str) -> tuple[bool, bool]:
    """Insert a listing row if new. Returns (created, domain_was_new).

    domain_was_new is the metric that actually matters for search value: a paid
    query that finds a previously-unknown dealer pays for itself many times over,
    because that domain then gets scanned for free.
    """
    domain = registrable_domain(url)
    if not domain or discovery.is_excluded(url):
        return False, False
    excluded = await db.fetchval(
        "SELECT excluded FROM sellers WHERE domain = $1", domain)
    if excluded:
        return False, False
    known = await db.fetchval("SELECT 1 FROM sellers WHERE domain = $1", domain)
    seller_id = await _seller_id(domain)
    if not known:
        await db.execute(
            "UPDATE sellers SET first_seen_via = $2 WHERE id = $1", seller_id, via
        )

    m = match.score_match(
        product, seen_title,
        auto_confirm=settings.match_auto_confirm,
        auto_reject=settings.match_auto_reject,
    )
    # Search-result titles are weak evidence; never auto-confirm from them alone.
    status = "rejected" if m.decision == "rejected" else "candidate"

    created = await db.fetchval(
        """
        INSERT INTO listings (product_id, seller_id, url, status, match_score,
                              match_signals, title_seen, discovered_via)
        VALUES ($1,$2,$3,$4,$5,$6::jsonb,$7,$8)
        ON CONFLICT (product_id, url) DO NOTHING
        RETURNING id
        """,
        product["id"], seller_id, url, status, m.score,
        json.dumps(m.signals), seen_title, via,
    )
    return created is not None, not known


# ---------------------------------------------------------------------------
# TASK: paste-a-URL domain scan
# ---------------------------------------------------------------------------
async def domain_scan(ctx: dict, job_row_id: int, domain_or_url: str) -> dict:
    """Scan one dealer domain for our products.

    Sitemap-first, and slug-filtered BEFORE fetching. A dealer sitemap can list
    5,000 products; scoring slugs against our ~300 titles costs nothing and
    typically reduces that to a few dozen fetches.
    """
    await db.init_pool()
    await _job_start(job_row_id)

    stats = {"sitemap_urls": 0, "candidates": 0, "fetched": 0,
             "confirmed": 0, "rejected": 0, "review": 0, "blocked": 0}
    domain = registrable_domain(
        domain_or_url if "//" in domain_or_url else f"https://{domain_or_url}"
    )

    try:
        if not domain:
            raise ValueError(f"could not parse a domain from {domain_or_url!r}")

        # Refuse before spending a single request.
        if await db.fetchval("SELECT excluded FROM sellers WHERE domain = $1", domain):
            await _job_progress(
                job_row_id, phase="done",
                note=f"{domain} is on the excluded list, so it was not scanned. "
                     f"Re-enable it on the Sellers page to scan it again.",
                **stats)
            await _job_done(job_row_id)
            return stats

        products = await db.fetch(
            "SELECT id, sku, title, upc, description, map_price FROM products WHERE active"
        )
        # Matching compares sitemap URL slugs against product titles, so with an
        # empty catalogue a scan cannot return anything but zeros however well
        # the dealer's site responds. Say that, rather than reporting a
        # successful no-op that looks like "this dealer carries nothing".
        if not products:
            await _seller_id(domain)
            await _job_progress(
                job_row_id, phase="done",
                note="No products loaded, so there is nothing to match against. "
                     "Import the product spreadsheet first, then re-run this scan.",
                **stats,
            )
            await _job_done(job_row_id)
            return stats

        by_id = {r["id"]: dict(r) for r in products}
        titles = {r["id"]: r["title"] for r in products}

        # Brand gating for the slug pre-filter. Our own titles never contain the
        # brand name, but a dealer's URLs do -- and without this the filter
        # matches every competitor sharing a gun platform.
        default_brand = (settings.brand_name.split() or [""])[0]
        raw_brand = (await settings_store.get("brand_tokens", default_brand)
                     or default_brand)
        brand_tokens = {
            re.sub(r"[^a-z0-9]", "", b.lower())
            for b in raw_brand.split(",") if b.strip()
        }
        brand_tokens.discard("")
        line_tokens = match.derive_line_tokens(titles)
        sku_index = match.build_sku_index(by_id)
        log.info("prefilter gating on brand=%s lines=%s",
                 sorted(brand_tokens), sorted(line_tokens))

        async with Fetcher() as fetcher:
            # --- listing-page mode -------------------------------------------
            # A pasted URL with a path -- a brand, category or search page -- is
            # scoped to what we care about already. Harvesting it costs a
            # handful of requests, where enumerating the same dealer's sitemap
            # costs tens of thousands and is mostly other brands' products.
            if listing_page.looks_like_listing_url(domain_or_url):
                seller_id = await _seller_id(domain)
                await _decay_tier2(domain)
                tier = await db.fetchval(
                    "SELECT fetch_tier FROM sellers WHERE id = $1", seller_id) or 1

                async def _page_progress(pages, found, current):
                    await _job_progress(job_row_id, phase="harvesting listing",
                                        pages_fetched=pages, urls_found=found)

                await _job_progress(job_row_id, phase="harvesting listing",
                                    mode="listing_page")
                h = await listing_page.harvest(
                    fetcher, domain_or_url, seller_tier=tier, on_page=_page_progress)
                stats["sitemap_urls"] = len(h.urls)
                log.info("listing harvest %s: %d urls over %d page(s) (%s)",
                         domain_or_url, len(h.urls), h.pages_fetched,
                         h.stopped_because)

                if h.js_rendered and not h.products:
                    await db.execute(
                        "UPDATE sellers SET notes = $2 WHERE id = $1",
                        seller_id,
                        "page is JavaScript-rendered; needs a fetch vendor")
                    await _job_progress(
                        job_row_id, phase="done",
                        note=f"{domain} builds its product grid with JavaScript, so "
                             f"the HTML we receive contains no products or prices. "
                             f"This is not evidence they carry nothing. To monitor "
                             f"this dealer, set a fetch vendor in Settings "
                             f"(ScrapingBee or Zyte) -- both render JavaScript.",
                        **stats)
                    await _job_done(job_row_id)
                    return stats

                if h.redirected_away and not h.products and not h.urls:
                    await _job_progress(
                        job_row_id, phase="done",
                        note=f"That URL redirected to {h.redirected_away} , which "
                             f"is not the page you asked for. The link is "
                             f"probably wrong or expired -- check the brand/"
                             f"category id. No products were read from it.",
                        **stats)
                    await _job_done(job_row_id)
                    return stats

                if h.blocked:
                    await db.execute(
                        "UPDATE sellers SET fetch_tier = 2, notes = $2 WHERE id = $1",
                        seller_id, (h.stopped_because or "blocked")[:500])
                    stats["blocked"] = 1
                    await _job_progress(
                        job_row_id, phase="done",
                        note=f"{domain} blocked this listing page ({h.stopped_because}). "
                             f"Configure a fetch vendor in Settings.", **stats)
                    await _job_done(job_row_id)
                    return stats

                # Cards that carried a price need no page fetch at all.
                if h.products:
                    priced = await _process_listing_products(
                        job_row_id, domain, seller_id, h.products,
                        by_id, sku_index, stats)
                    # Anything we could not price from the card still needs its
                    # own page, so fall through with just those URLs.
                    done_urls = {lp.url for lp in h.products if lp.price}
                    h.urls = [u for u in h.urls if u not in done_urls]
                    if not h.urls:
                        await _job_progress(
                            job_row_id, phase="done",
                            note=f"listing page: {h.pages_fetched} page(s); "
                                 f"{priced} product(s) priced directly from the "
                                 f"cards, no per-product fetches needed",
                            **stats)
                        await _job_done(job_row_id)
                        return stats

                if h.urls:
                    return await _process_candidate_urls(
                        job_row_id, fetcher, domain, seller_id, h.urls,
                        by_id, titles, brand_tokens, line_tokens, sku_index,
                        stats,
                        # A brand or search page is ALREADY scoped to us, so a
                        # slug need not repeat the brand name to qualify.
                        # Brand gating stays ON: link extraction inevitably
                        # picks up the site's own navigation, and the gate is
                        # what removes it. _process_candidate_urls retries
                        # without the gate if this finds nothing.
                        require_brand=True,
                        allow_ungated_retry=True,
                        discovered_via="listing_page",
                        source_note=f"listing page: {h.pages_fetched} page(s), "
                                    f"stopped because {h.stopped_because}",
                    )

                await _job_progress(
                    job_row_id,
                    note=f"listing page yielded no product links "
                         f"({h.stopped_because}); falling back to sitemap")

            # --- sitemap mode -------------------------------------------------
            # The pasted host is often not the one carrying the catalogue, so
            # fall back to the apex and www variants before giving up.
            await _job_progress(job_row_id, phase="resolving host")
            host, sitemaps, host_note = await discovery.resolve_scan_host(
                fetcher, domain_or_url
            )
            # Store the registrable domain, not the host that answered: the
            # www variant is the same dealer and must not become a second seller.
            if host:
                domain = registrable_domain(f"https://{host}")
            seller_id = await _seller_id(domain)
            await _job_progress(job_row_id, phase="sitemap", host=domain,
                                sitemaps=len(sitemaps),
                                host_note=host_note or None)
            # Record the sitemap on the seller. This is the promotion that
            # retires paid search for this domain: once we can enumerate their
            # catalogue for free, searching for individual SKUs there is waste.
            await db.execute(
                """
                UPDATE sellers
                   SET sitemap_url = $2, sitemap_ok = $3, last_sitemap_scan_at = now()
                 WHERE id = $1
                """,
                seller_id, sitemaps[0] if sitemaps else None, bool(sitemaps),
            )

            # Drop the 90-odd listing/review/unorderable sitemaps before
            # spending a single request on them.
            sitemaps, sm_stats = discovery.prioritise_sitemaps(sitemaps)
            await _job_progress(job_row_id, phase="sitemap", sitemap_filter=sm_stats,
                                sitemaps=len(sitemaps))
            log.info("%s: %s -> expanding %d sitemap(s)", domain, sm_stats, len(sitemaps))

            async def _sitemap_progress(done, total, found):
                await _job_progress(job_row_id, phase="expanding sitemap",
                                    sitemap_children=f"{done}/{total}",
                                    urls_found=found)

            urls: list[str] = []
            for sm in sitemaps:
                urls.extend(await discovery.expand_sitemap(
                    fetcher, sm, on_progress=_sitemap_progress))
                if len(urls) >= 50_000:
                    break
            urls = [u for u in dict.fromkeys(urls) if discovery.looks_like_product_url(u)]
            stats["sitemap_urls"] = len(urls)
            await db.execute(
                "UPDATE sellers SET sitemap_url_count = $2 WHERE id = $1",
                seller_id, len(urls),
            )
            await _job_progress(job_row_id, phase="filtering", **stats)

            if not urls:
                # Distinguish "walled off" from "no sitemap" -- they need
                # completely different responses from the operator.
                probe = await fetcher.get(f"https://{domain}/")
                if probe.blocked:
                    note = (f"{domain} is behind bot protection ({probe.error}). "
                            "Direct fetching will not work: configure a fetch "
                            "vendor in Settings, or scan a different host.")
                    await db.execute(
                        "UPDATE sellers SET fetch_tier = 2, notes = $2 WHERE id = $1",
                        seller_id, note[:500],
                    )
                    stats["blocked"] = 1
                elif host_note:
                    note = host_note
                else:
                    note = ("no usable sitemap; add a per-domain selector rule or "
                            "paste individual product URLs")
                await _job_progress(job_row_id, phase="done", note=note, **stats)
                await _job_done(job_row_id)
                return stats

            return await _process_candidate_urls(
                job_row_id, fetcher, domain, seller_id, urls,
                by_id, titles, brand_tokens, line_tokens, sku_index, stats,
                require_brand=True, discovered_via="sitemap",
                source_note=f"sitemap: {len(urls)} product URLs",
            )
    except Exception as exc:
        log.exception("domain_scan failed for %s", domain_or_url)
        await _job_done(job_row_id, str(exc)[:1000])
        raise
    return stats


async def _process_listing_products(
    job_row_id: int, domain: str, seller_id: int, products: list,
    by_id: dict, sku_index: dict, stats: dict,
) -> int:
    """Record products read straight off a listing page, prices included.

    The cheapest possible path: identity comes from the card's own SKU or URL,
    the price comes from the card, and no product page is fetched. On a large
    retailer this is the difference between four requests and two hundred --
    which on a bot-protected site is the difference between working and being
    blocked.
    """
    recorded = 0
    for lp in products:
        if not lp.price:
            continue
        # Their composite SKU usually embeds ours (6QB-HT3-FGC-AB-0018-30).
        pid = match.find_sku_in_text(lp.sku or "", sku_index)
        if pid is None:
            pid = match.find_sku_in_text(lp.url, sku_index)
        if pid is None:
            continue
        product = by_id.get(pid)
        if product is None:
            continue

        price = extract.parse_price(lp.price)
        if price is None:
            continue

        signals = {
            "sku_in_text": {"sku": match.normalize_sku(product["sku"]),
                            "found_in": ["listing card"]},
            "identifier_confirmed": True,
            "source": "listing page card",
        }
        listing_id = await db.fetchval(
            """
            INSERT INTO listings (product_id, seller_id, url, status, match_score,
                                  match_signals, title_seen, sku_seen,
                                  discovered_via, last_checked, next_check_at)
            VALUES ($1,$2,$3,'confirmed',100,$4::jsonb,$5,$6,'listing_card',
                    now(), now() + interval '24 hours')
            ON CONFLICT (product_id, url) DO UPDATE
                SET status = 'confirmed', match_score = 100,
                    match_signals = EXCLUDED.match_signals,
                    title_seen = EXCLUDED.title_seen,
                    sku_seen = EXCLUDED.sku_seen,
                    last_checked = now()
            RETURNING id
            """,
            product["id"], seller_id, lp.url, json.dumps(signals),
            lp.title, lp.sku,
        )
        if listing_id is None:
            continue

        map_price = product["map_price"]
        below = price < map_price
        last = await db.fetchrow(
            "SELECT price FROM price_observations WHERE listing_id=$1 "
            "ORDER BY observed_at DESC LIMIT 1", listing_id)
        if last is None or last["price"] != price:
            await db.execute(
                """
                INSERT INTO price_observations
                    (listing_id, price, currency, extraction_tier, fetch_tier, below_map)
                VALUES ($1,$2,'USD','listing_card',1,$3)
                """,
                listing_id, price, below)

        streak = await db.fetchval(
            "UPDATE listings SET below_map_streak = CASE WHEN $2 "
            "THEN below_map_streak + 1 ELSE 0 END WHERE id = $1 "
            "RETURNING below_map_streak", listing_id, below)

        await violations.evaluate(
            listing_id=listing_id, price=price, map_price=map_price,
            url=lp.url, html=None, fetcher=None, source_tier="listing_card",
            below_map_streak=streak or 0)

        recorded += 1
        stats["confirmed"] = stats.get("confirmed", 0) + 1
        if recorded % 20 == 0:
            await _job_progress(job_row_id, priced_from_cards=recorded, **stats)

    await _job_progress(job_row_id, priced_from_cards=recorded, **stats)
    log.info("listing cards on %s: %d product(s) priced without a page fetch",
             domain, recorded)
    return recorded


async def _process_candidate_urls(
    job_row_id: int, fetcher: Fetcher, domain: str, seller_id: int,
    urls: list[str], by_id: dict, titles: dict, brand_tokens: set,
    line_tokens: set, sku_index: dict, stats: dict,
    require_brand: bool = True,
    source_note: str = "",
    discovered_via: str = "sitemap",
    allow_ungated_retry: bool = False,
) -> dict:
    """Pre-filter a URL list, then verify and record the plausible ones.

    Shared by both scan modes. `require_brand` is False for listing-page scans:
    a brand or search page is already scoped to us, so a slug need not repeat
    the brand name to qualify.
    """
    # -- the cheap pre-filter --------------------------------------
    candidates: list[tuple[str, int]] = []
    for u in urls:
        hits = match.slug_prefilter(
            titles, u, brand_tokens=brand_tokens, line_tokens=line_tokens,
            sku_index=sku_index, require_brand=require_brand,
        )
        for pid, _ratio in hits[:1]:
            candidates.append((u, pid))
    stats["candidates"] = len(candidates)

    # A listing page is scoped to us by definition, so if the brand gate finds
    # nothing it is more likely the dealer omits our name from slugs than that
    # they carry nothing. Retry ungated, but capped -- an ungated pass over a
    # navigation-heavy page is exactly how 178 useless fetches happened.
    if not candidates and allow_ungated_retry and urls:
        UNGATED_CAP = 60
        for u in urls:
            hits = match.slug_prefilter(
                titles, u, brand_tokens=brand_tokens, line_tokens=line_tokens,
                sku_index=sku_index, require_brand=False,
            )
            for pid, _ratio in hits[:1]:
                candidates.append((u, pid))
            if len(candidates) >= UNGATED_CAP:
                break
        stats["candidates"] = len(candidates)
        stats["ungated_retry"] = True
        await _job_progress(
            job_row_id,
            note=f"no slug carried your brand or model names; retried without "
                 f"the brand gate, capped at {UNGATED_CAP} fetches")

    if not candidates:
        # An informative result, not a failure: no URL on this domain
        # carries the brand or any model-line name.
        await _job_progress(
            job_row_id, phase="done",
            note=f"Scanned {len(urls)} product URLs on {domain}; none "
                 f"carry your brand or model-line names, so this dealer "
                 f"does not appear to list your products. No pages were "
                 f"fetched.",
            **stats,
        )
        await _job_done(job_row_id)
        return stats

    # A large retailer can yield hundreds of candidates. Cap the fetches
    # per scan and report the truncation, rather than running for hours
    # or silently implying full coverage.
    MAX_CANDIDATE_FETCHES = 400
    truncated = 0
    if len(candidates) > MAX_CANDIDATE_FETCHES:
        truncated = len(candidates) - MAX_CANDIDATE_FETCHES
        candidates = candidates[:MAX_CANDIDATE_FETCHES]
        await _job_progress(
            job_row_id,
            note=f"{truncated} further candidates not fetched this run "
                 f"(cap {MAX_CANDIDATE_FETCHES}); re-run to continue",
        )
    await _job_progress(job_row_id, phase="fetching", **stats)
    log.info("scan %s: %s urls -> %s candidates (require_brand=%s)",
             domain, len(urls), len(candidates), require_brand)

    # -- verify only the plausible ones ----------------------------
    for i, (url, pid) in enumerate(candidates, 1):
        product = by_id.get(pid)
        if product is None:
            continue
        tier = await db.fetchval(
            "SELECT fetch_tier FROM sellers WHERE id = $1", seller_id
        ) or 1
        res = await fetcher.get(url, seller_tier=tier)
        stats["fetched"] += 1
        if not res.ok:
            if res.blocked:
                await _note_tier1_failure(domain)
            continue

        rule = await _rule_for(domain)
        ext = await extract.extract_price(res.html, res.final_url, fetcher, rule)
        m = match.score_match(
            product,
            seen_title=ext.title,
            seen_sku=ext.sku,
            seen_upc=ext.upc,
            seen_url=res.final_url,
            auto_confirm=settings.match_auto_confirm,
            auto_reject=settings.match_auto_reject,
        )
        stats[{"confirmed": "confirmed", "rejected": "rejected"}
              .get(m.decision, "review")] += 1

        listing_id = await db.fetchval(
            """
            INSERT INTO listings (product_id, seller_id, url, status,
                                  match_score, match_signals, title_seen,
                                  sku_seen, upc_seen, discovered_via,
                                  last_checked)
            VALUES ($1,$2,$3,$4,$5,$6::jsonb,$7,$8,$9,$10,now())
            ON CONFLICT (product_id, url) DO UPDATE
                SET match_score = EXCLUDED.match_score,
                    next_check_at = now() + interval '24 hours',
                    match_signals = EXCLUDED.match_signals,
                    title_seen = EXCLUDED.title_seen,
                    sku_seen = EXCLUDED.sku_seen,
                    upc_seen = EXCLUDED.upc_seen,
                    last_checked = now()
            RETURNING id
            """,
            product["id"], seller_id, res.final_url, m.decision, m.score,
            json.dumps(m.signals), ext.title, ext.sku, ext.upc,
            discovered_via,
        )

        if m.decision == "confirmed" and ext.price is not None:
            below = ext.price < product["map_price"]
            await db.execute(
                """
                INSERT INTO price_observations
                    (listing_id, price, currency, in_stock,
                     extraction_tier, fetch_tier, below_map)
                VALUES ($1,$2,$3,$4,$5,$6,$7)
                """,
                listing_id, ext.price, ext.currency, ext.in_stock,
                ext.tier, res.tier, below,
            )
            await violations.evaluate(
                listing_id=listing_id, price=ext.price,
                map_price=product["map_price"], url=res.final_url,
                html=res.html, fetcher=fetcher, source_tier=ext.tier,
            )

        if i % 5 == 0 or i == len(candidates):
            await _job_progress(job_row_id, **stats)


    await _job_progress(job_row_id, phase="done", note=source_note or None, **stats)
    await _job_done(job_row_id)
    return stats


# ---------------------------------------------------------------------------
# TASK: eBay sweep -- a prohibited channel, so ANY listing is a violation
# ---------------------------------------------------------------------------
async def ebay_sweep(ctx: dict, job_row_id: Optional[int] = None,
                     max_products: Optional[int] = None) -> dict:
    """Search eBay for our products. Every hit is a violation.

    GTIN search is used wherever a UPC exists, because an unauthorised-listing
    finding names a specific seller, and a fuzzy title match is not a sound
    basis for that. Products without a UPC fall back to a quoted-title search,
    and those results still go through the normal matcher before anything is
    recorded.
    """
    await db.init_pool()
    if job_row_id is None:
        job_row_id = await db.fetchval(
            "INSERT INTO crawl_jobs (kind, target, status) "
            "VALUES ('ebay_sweep','eBay marketplace','running') RETURNING id")
    else:
        await _job_start(job_row_id)

    stats = {"searched": 0, "hits": 0, "new_listings": 0,
             "violations_opened": 0, "sellers": 0}
    try:
        if not await ebay.configured():
            await _job_progress(
                job_row_id, phase="done",
                note="eBay App ID and Cert ID are not set. Add them in Settings, "
                     "then use 'Test eBay credentials' before sweeping.",
                **stats)
            await _job_done(job_row_id)
            return stats

        seller_id = await _seller_id("ebay.com")
        await db.execute(
            "UPDATE sellers SET policy = 'prohibited' WHERE id = $1", seller_id)

        limit = max_products or await settings_store.get_int(
            "ebay_products_per_run", 250)
        products = await db.fetch(
            "SELECT id, sku, title, upc, description, map_price "
            "FROM products WHERE active ORDER BY id LIMIT $1", limit)
        await _job_progress(job_row_id, planned=len(products), **stats)
        log.info("eBay sweep: %d product(s) to search", len(products))

        seen_sellers: set[str] = set()
        failures = 0
        fatal: Optional[str] = None
        last_error: Optional[str] = None
        for i, prod in enumerate(products, 1):
            product = dict(prod)
            try:
                if product["upc"]:
                    hits = await ebay.search_by_gtin(product["upc"])
                else:
                    hits = await ebay.search_by_keyword(f'"{product["title"]}"')
            except ebay.EbayError as exc:
                failures += 1
                last_error = str(exc)[:200]
                log.warning("eBay search failed for %s: %s", product["sku"], exc)
                await _job_progress(job_row_id, last_error=str(exc)[:200], **stats)
                # A rate limit will not clear within this run, so stop rather
                # than burning the rest of the catalogue on failures.
                if "rate limit" in str(exc).lower() or "429" in str(exc):
                    await _job_progress(
                        job_row_id,
                        note="stopped early: eBay rate limit reached. Remaining "
                             "products will be covered on the next sweep.")
                    break
                # Bad credentials fail identically for every product, and each
                # attempt re-mints a token against an endpoint capped at 1,000
                # calls/day. Continuing spent ~250 of that allowance per sweep
                # to learn one fact 246 times over, and could lock out the rest
                # of the day. Stop on the first one.
                if _is_auth_failure(exc):
                    fatal = str(exc)[:200]
                    break
                continue

            stats["searched"] += 1
            stats["hits"] += len(hits)

            for hit in hits:
                opened, created, seller = await _record_ebay_listing(
                    product, hit, seller_id)
                if created:
                    stats["new_listings"] += 1
                if opened:
                    stats["violations_opened"] += 1
                if seller:
                    seen_sellers.add(seller)

            stats["sellers"] = len(seen_sellers)
            if i % 10 == 0 or i == len(products):
                await _job_progress(job_row_id, **stats)

        # A sweep that searched nothing has not established that nothing is
        # listed -- it has established nothing at all. Reporting "no listings
        # matched" off the back of 246 consecutive auth failures is a false
        # all-clear on the one channel where every listing is a violation, so
        # a run that never got a single successful search fails loudly instead.
        if products and (fatal or stats["searched"] == 0):
            reason = fatal or last_error or "every eBay search failed"
            note = (f"SWEEP FAILED -- searched 0 of {len(products)} product(s). "
                    f"This is NOT an all-clear: nothing was checked. {reason}")
            await _job_progress(job_row_id, phase="failed", note=note,
                                failures=failures, **stats)
            await _job_done(job_row_id, note[:1000])
            log.error("ebay sweep failed without searching anything: %s", reason)
            return stats

        # The sandbox carries no real inventory -- a keyword search for
        # "holster" returns zero items there -- so a sandbox sweep CANNOT find
        # a violation, and its "nothing matched" is a property of the
        # environment, not of the marketplace. Saying otherwise would be a
        # false all-clear dressed up with a plausible searched-count.
        _, _, sandbox = await ebay._credentials()
        if sandbox and not stats["hits"]:
            note = (f"INCONCLUSIVE -- ran against the eBay SANDBOX, which holds "
                    f"no real listings. Searched {stats['searched']} product(s) "
                    f"and matched nothing, but that proves nothing about the "
                    f"live marketplace. Switch to production in Settings.")
            await _job_progress(job_row_id, phase="inconclusive", note=note,
                                sandbox=True, failures=failures, **stats)
            await _job_done(job_row_id, status="inconclusive")
            log.warning("ebay sweep ran against sandbox; result is not an all-clear")
            return stats

        note = (f"{stats['hits']} eBay listing(s) matched across "
                f"{len(seen_sellers)} seller(s); {stats['violations_opened']} "
                f"violation(s) opened. Every listing on eBay is a violation -- "
                f"no reseller is authorised there."
                if stats["hits"] else
                f"Searched {stats['searched']} product(s); no eBay listings "
                f"matched. Nothing of ours appears to be listed there.")
        if sandbox:
            note = "[SANDBOX -- not the live marketplace] " + note
        if failures:
            # Partial coverage is a real result, but the gap has to be visible.
            note += (f" {failures} product(s) could not be searched and were "
                     f"NOT checked.")
        await _job_progress(job_row_id, phase="done", note=note,
                            failures=failures, **stats)
        await _job_done(job_row_id)
    except Exception as exc:
        log.exception("ebay_sweep failed")
        await _job_done(job_row_id, str(exc)[:1000])
        raise
    return stats


def _is_auth_failure(exc: Exception) -> bool:
    """Is this an eBay error that every remaining product will hit identically?

    Credential and authorisation problems are properties of the keyset, not of
    the product being searched, so retrying 245 more times cannot succeed -- it
    only spends the 1,000/day token allowance discovering the same thing again.
    """
    text = str(exc).lower()
    return (
        "invalid_client" in text
        or "invalid_grant" in text
        or "invalid_scope" in text
        or "unauthorized_client" in text
        or "token http 401" in text
        or "token http 403" in text
    )


async def _record_ebay_listing(product: dict, hit, seller_id: int
                               ) -> tuple[bool, bool, Optional[str]]:
    """Record one eBay hit. Returns (violation_opened, listing_created, seller)."""
    # A GTIN search is exact by construction; a keyword hit still has to earn it.
    if hit.matched_via == "gtin":
        decision, score = "confirmed", 100.0
        signals = {"gtin": {"searched": product["upc"], "via": "ebay gtin search"},
                   "identifier_confirmed": True}
    else:
        m = match.score_match(
            product, seen_title=hit.title, seen_url=hit.url,
            auto_confirm=settings.match_auto_confirm,
            auto_reject=settings.match_auto_reject)
        decision, score, signals = m.decision, m.score, m.signals
        if decision == "rejected":
            return False, False, None

    existing = await db.fetchval(
        "SELECT id FROM listings WHERE product_id = $1 AND url = $2",
        product["id"], hit.url)

    listing_id = await db.fetchval(
        """
        INSERT INTO listings (product_id, seller_id, url, status, match_score,
                              match_signals, title_seen, discovered_via,
                              marketplace_seller, external_id, listing_condition,
                              last_checked, next_check_at)
        VALUES ($1,$2,$3,$4,$5,$6::jsonb,$7,'ebay',$8,$9,$10,now(),
                now() + interval '24 hours')
        ON CONFLICT (product_id, url) DO UPDATE
            SET match_score = EXCLUDED.match_score,
                match_signals = EXCLUDED.match_signals,
                title_seen = EXCLUDED.title_seen,
                marketplace_seller = EXCLUDED.marketplace_seller,
                listing_condition = EXCLUDED.listing_condition,
                last_checked = now()
        RETURNING id
        """,
        product["id"], seller_id, hit.url, decision, score,
        json.dumps(signals), hit.title, hit.seller, hit.item_id, hit.condition)
    if listing_id is None:
        return False, False, hit.seller

    if hit.price is not None:
        below = (product["map_price"] is not None
                 and hit.price < product["map_price"])
        last = await db.fetchrow(
            "SELECT price FROM price_observations WHERE listing_id=$1 "
            "ORDER BY observed_at DESC LIMIT 1", listing_id)
        if last is None or last["price"] != hit.price:
            await db.execute(
                "INSERT INTO price_observations "
                "(listing_id, price, currency, extraction_tier, fetch_tier, below_map) "
                "VALUES ($1,$2,$3,'ebay_api',1,$4)",
                listing_id, hit.price, hit.currency, below)

    outcome = None
    if decision == "confirmed":
        outcome = await violations.evaluate(
            listing_id=listing_id, price=hit.price,
            map_price=product["map_price"], url=hit.url, html=None,
            fetcher=None, source_tier="ebay_api", policy="prohibited")
    return outcome == "opened", existing is None, hit.seller


async def prune_observations(ctx: dict, keep_days: int = 400) -> int:
    await db.init_pool()
    # Never prune an observation attached to a violation window: that is the
    # historical record a dealer dispute would rest on.
    deleted = await db.fetchval(
        """
        WITH doomed AS (
            SELECT o.id FROM price_observations o
             WHERE o.observed_at < now() - ($1 || ' days')::interval
               AND NOT EXISTS (
                   SELECT 1 FROM violations v
                    WHERE v.listing_id = o.listing_id
                      AND o.observed_at BETWEEN v.opened_at - interval '7 days'
                                           AND COALESCE(v.resolved_at, now())
               )
             LIMIT 50000
        )
        DELETE FROM price_observations WHERE id IN (SELECT id FROM doomed)
        RETURNING 1
        """,
        str(keep_days),
    )
    return deleted or 0


# ---------------------------------------------------------------------------
# arq worker settings
# ---------------------------------------------------------------------------
async def startup(ctx: dict) -> None:
    await db.init_pool()
    settings.evidence_dir.mkdir(parents=True, exist_ok=True)
    # Read through settings_store, not `settings`: credentials entered in the
    # admin UI live in the database and .env is empty for all of them. Reading
    # the env directly made this banner report "vendor=disabled" while the
    # fetcher was in fact using Zyte -- a status line that contradicts the
    # running system is worse than no status line.
    vendor = (await settings_store.get("fetch_vendor", "") or "").strip().lower()
    vendor_key = await settings_store.get(f"{vendor}_key", "") if vendor else ""
    llm_on = await extract.llm_enabled()
    log.info("worker up; evidence dir %s; vendor=%s; llm=%s",
             settings.evidence_dir,
             f"{vendor} (no key)" if vendor and not vendor_key
             else (vendor or "disabled"),
             (await settings_store.get("extraction_model",
                                       extract.DEFAULT_EXTRACTION_MODEL))
             if llm_on else "disabled")


async def shutdown(ctx: dict) -> None:
    await db.close_pool()


class WorkerSettings:
    functions = [recheck_all, discovery_sweep, domain_scan, ebay_sweep,
                 prune_observations]
    on_startup = startup
    on_shutdown = shutdown
    redis_settings = RedisSettings.from_dsn(settings.redis_url)
    max_jobs = 2               # one vCPU; two concurrent jobs is the ceiling
    job_timeout = 60 * 60 * 6  # a full recheck can legitimately take hours
    keep_result = 60 * 60
    cron_jobs = [
        # Nightly recheck at 03:20 UTC -- deliberately offset from typical
        # 02:00-03:00 backup and ETL windows on a shared host.
        cron(recheck_all, hour=3, minute=20),
        # Discovery sweep at 07:00 UTC, staying inside the PSE free tier.
        cron(discovery_sweep, hour=7, minute=0),
        # eBay sweep at 05:40 UTC: exact GTIN searches, well inside the daily
        # Browse allowance, and offset from the other jobs.
        cron(ebay_sweep, hour=5, minute=40),
        # Retention, weekly.
        cron(prune_observations, weekday="sun", hour=5, minute=0),
    ]
