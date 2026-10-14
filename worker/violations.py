"""Violation open/resolve state machine.

Debounced deliberately: a dealer whose price dips a dollar under MAP for one
crawl should not open a violation, because an alert stream you learn to ignore
is worse than no alert stream. Two knobs:

  VIOLATION_CONSECUTIVE_CHECKS -- N consecutive sub-MAP observations required
  VIOLATION_MIN_DELTA_PCT      -- ignore dips smaller than this percentage

Resolution is symmetric: one observation at or above MAP closes the violation,
because a dealer who fixes their price deserves immediate credit.
"""
from __future__ import annotations

import logging
from decimal import Decimal
from typing import Optional

from app import db
from app.config import settings

log = logging.getLogger(__name__)


def delta_pct(price: Decimal, map_price: Decimal) -> Decimal:
    if map_price <= 0:
        return Decimal(0)
    return ((map_price - price) / map_price * Decimal(100)).quantize(Decimal("0.01"))


async def evaluate_prohibited(
    listing_id: int,
    price: Optional[Decimal],
    map_price: Optional[Decimal],
    url: str,
    html: Optional[str] = None,
    fetcher=None,
    source_tier: Optional[str] = None,
) -> Optional[str]:
    """Open a violation because the CHANNEL is prohibited, whatever the price.

    No debounce and no MAP comparison: on a channel where no reseller is
    authorised, the listing existing is the violation. Debouncing would only
    delay a finding that is already certain -- unlike a price, which can move.

    The listing being delisted is what resolves it, so resolution is handled by
    the recheck path when the URL stops returning a live item.
    """
    existing = await db.fetchval(
        "SELECT id FROM violations WHERE listing_id = $1 AND status = 'open'",
        listing_id)
    if existing:
        # Track the lowest price seen, purely as context for the notice.
        if price is not None:
            await db.execute(
                "UPDATE violations SET lowest_price = LEAST(coalesce(lowest_price, $2), $2) "
                "WHERE id = $1", existing, price)
        return None

    violation_id = await db.fetchval(
        """
        INSERT INTO violations (listing_id, status, kind, map_at_open,
                                opening_price, lowest_price)
        VALUES ($1, 'open', 'unauthorized', $2, $3, $3)
        ON CONFLICT (listing_id) WHERE status = 'open' DO NOTHING
        RETURNING id
        """,
        listing_id, map_price, price)
    if violation_id is None:
        return None

    log.info("UNAUTHORIZED listing violation %s opened for listing %s (%s)",
             violation_id, listing_id, url)
    from worker import evidence
    await evidence.capture(
        listing_id=listing_id, url=url, html=html, observed_price=price,
        map_price=map_price, fetcher=fetcher, violation_id=violation_id,
        source_tier=source_tier)
    return "opened"


async def evaluate(
    listing_id: int,
    price: Optional[Decimal],
    map_price: Decimal,
    url: str,
    html: Optional[str] = None,
    fetcher=None,
    source_tier: Optional[str] = None,
    below_map_streak: int = 1,
    policy: str = "map",
) -> Optional[str]:
    """Apply the state machine after a price observation.

    Returns 'opened', 'resolved', 'deepened', or None.

    `below_map_streak` is how many consecutive CHECKS have seen this listing
    under MAP, maintained on the listing row by the caller.
    """
    if policy == "prohibited":
        return await evaluate_prohibited(
            listing_id, price, map_price, url, html, fetcher, source_tier)

    open_row = await db.fetchrow(
        "SELECT id, lowest_price, worst_delta_pct FROM violations "
        "WHERE listing_id = $1 AND status = 'open' AND kind = 'map'",
        listing_id,
    )

    # No price extracted -- do not touch violation state. An extraction failure
    # is not evidence of compliance.
    if price is None:
        return None

    if price >= map_price:
        if open_row:
            await db.execute(
                """
                UPDATE violations
                   SET status = 'resolved', resolved_at = now(), resolved_price = $2
                 WHERE id = $1
                """,
                open_row["id"], price,
            )
            log.info("violation %s resolved for listing %s at %s",
                     open_row["id"], listing_id, price)
            return "resolved"
        return None

    # -- below MAP ----------------------------------------------------------
    d = delta_pct(price, map_price)

    if open_row:
        # Track the worst observed price for the life of the violation.
        if price < open_row["lowest_price"]:
            await db.execute(
                "UPDATE violations SET lowest_price = $2, worst_delta_pct = $3 WHERE id = $1",
                open_row["id"], price, d,
            )
            return "deepened"
        return None

    # Debounce before opening.
    if d < Decimal(str(settings.violation_min_delta_pct)):
        log.debug("listing %s below MAP by only %s%%, under threshold", listing_id, d)
        return None

    # Counted per CHECK, not per stored observation. Deriving it from
    # price_observations was wrong: those are written only on change, so a price
    # sitting stably below MAP produced one row and never reached the threshold.
    if below_map_streak < settings.violation_consecutive_checks:
        log.debug("listing %s below MAP, streak %s/%s -- waiting",
                  listing_id, below_map_streak, settings.violation_consecutive_checks)
        return None

    violation_id = await db.fetchval(
        """
        INSERT INTO violations (listing_id, status, map_at_open, opening_price,
                                lowest_price, worst_delta_pct)
        VALUES ($1, 'open', $2, $3, $3, $4)
        ON CONFLICT (listing_id) WHERE status = 'open' DO NOTHING
        RETURNING id
        """,
        listing_id, map_price, price, d,
    )
    if violation_id is None:
        return None

    log.info("violation %s OPENED for listing %s: %s vs MAP %s (-%s%%)",
             violation_id, listing_id, price, map_price, d)

    # Evidence is captured here and only here.
    from worker import evidence
    await evidence.capture(
        listing_id=listing_id, url=url, html=html, observed_price=price,
        map_price=map_price, fetcher=fetcher, violation_id=violation_id,
        source_tier=source_tier,
    )
    return "opened"
