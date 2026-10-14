#!/usr/bin/env python3
"""Prove the planner's query reduction against the live schema.

Seeds a synthetic 300-SKU catalogue, runs the planner, and measures spend under
each rule. Cleans up after itself.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import db  # noqa: E402
from worker import planner  # noqa: E402

fails: list[str] = []
TAG = "PLANTEST-"


def check(name, got, want):
    ok = got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {name}: {got!r}" + ("" if ok else f" want {want!r}"))
    if not ok:
        fails.append(name)


def note(msg):
    print(f"        {msg}")


async def cleanup():
    await db.execute("DELETE FROM search_query_log WHERE query LIKE '%' || $1 || '%'", TAG)
    await db.execute("DELETE FROM products WHERE sku LIKE $1 || '%'", TAG)
    await db.execute("DELETE FROM sellers WHERE domain LIKE $1 || '%'", TAG.lower())


async def main() -> int:
    await db.init_pool()
    try:
        await cleanup()

        # --- seed 300 SKUs: 240 with a UPC, 60 without --------------------
        for i in range(300):
            upc = f"00123456{i:05d}" if i < 240 else None
            await db.execute(
                "INSERT INTO products (sku, title, upc, map_price) VALUES ($1,$2,$3,$4)",
                f"{TAG}{i:03d}", f"{TAG} IWB Holster Model {i} for Sig P{300+i}",
                upc, 64.99,
            )
        total = await db.fetchval("SELECT count(*) FROM products WHERE sku LIKE $1||'%'", TAG)
        check("seeded catalogue", total, 300)

        print("\n== Rule 1: one query per product, UPC preferred ==")
        with_upc = await db.fetchrow(
            "SELECT id, sku, title, upc, description FROM products "
            "WHERE sku LIKE $1||'%' AND upc IS NOT NULL LIMIT 1", TAG)
        no_upc = await db.fetchrow(
            "SELECT id, sku, title, upc, description FROM products "
            "WHERE sku LIKE $1||'%' AND upc IS NULL LIMIT 1", TAG)
        k1, q1, r1 = planner.build_query(dict(with_upc))
        k2, q2, r2 = planner.build_query(dict(no_upc))
        check("with UPC -> upc query", k1, "upc")
        check("without UPC -> title query", k2, "title")
        note(f"upc query:   {q1}")
        note(f"title query: {q2}")
        note("the old build_queries() emitted BOTH kinds per product")
        note(f"old cost for 300 SKUs: {240*2 + 60*1} queries")
        note(f"new cost for 300 SKUs: {300} queries  (before any other rule)")

        print("\n== Rule 5: reserve is held back from the monthly cap ==")
        check("cap 1000, used 0   -> spendable", await planner.spendable(1000, 0), 800)
        check("cap 1000, used 700 -> spendable", await planner.spendable(1000, 700), 100)
        check("cap 1000, used 850 -> spendable", await planner.spendable(1000, 850), 0)
        note("20% reserve keeps an ad-hoc scan possible late in the month")

        print("\n== plan() respects the per-run budget ==")
        p = await planner.plan(40)
        check("plan size capped at budget", len(p), 40)
        check("all planned are UPC queries (preferred first)",
              all(x.kind == "upc" for x in p), True)
        check("zero budget -> empty plan", len(await planner.plan(0)), 0)

        print("\n== Rule 4: a query already paid for is not repeated ==")
        first = p[0]
        await db.execute(
            "INSERT INTO search_query_log (query_hash, query, provider, product_id, result_count) "
            "VALUES ($1,$2,'brave',$3,0)",
            first.query_hash, first.query, first.product_id,
        )
        check("logged query now reads as cached",
              await planner.recently_queried(first.query_hash), True)
        p2 = await planner.plan(40)
        check("cached product dropped from the new plan",
              first.product_id in [x.product_id for x in p2], False)
        note("a zero-result query still costs one, so never pay twice inside the window")

        print("\n== Rule 3: exponential backoff on unproductive SKUs ==")
        target = p2[0]
        for expected in (3, 6, 12, 24, 48, 60, 60):
            await planner.record_outcome(target, result_count=0, new_listings=0, new_domains=0)
            row = await db.fetchrow(
                "SELECT consecutive_empty, "
                "EXTRACT(day FROM (next_eligible_at - now()))::int AS days "
                "FROM discovery_cursor WHERE product_id = $1", target.product_id)
            got = row["days"]
            ok = abs(got - expected) <= 1        # clock rounding
            print(f"  {'PASS' if ok else 'FAIL'}  empty #{row['consecutive_empty']} -> next in ~{got}d (want ~{expected}d)")
            if not ok:
                fails.append(f"backoff {expected}")
            target.product["consecutive_empty"] = row["consecutive_empty"]
        note("capped at 60 days, so a dead SKU costs ~6 queries a year, not 12 a month")

        print("\n== a productive query resets the backoff ==")
        await planner.record_outcome(target, result_count=8, new_listings=2, new_domains=1)
        row = await db.fetchrow(
            "SELECT consecutive_empty, EXTRACT(day FROM (next_eligible_at - now()))::int AS days "
            "FROM discovery_cursor WHERE product_id = $1", target.product_id)
        check("streak reset", row["consecutive_empty"], 0)
        check("back to base interval", abs(row["days"] - 3) <= 1, True)

        print("\n== Rule 2: sitemap-covered products are skipped ==")
        seller_id = await db.fetchval(
            "INSERT INTO sellers (domain, sitemap_ok, sitemap_url) "
            "VALUES ($1, true, 'https://x/sitemap.xml') RETURNING id",
            f"{TAG.lower()}dealer.example")
        covered = await db.fetchrow(
            "SELECT id FROM products WHERE sku LIKE $1||'%' AND upc IS NOT NULL "
            "ORDER BY id DESC LIMIT 1", TAG)
        await db.execute(
            "INSERT INTO listings (product_id, seller_id, url, status) "
            "VALUES ($1,$2,$3,'confirmed')",
            covered["id"], seller_id, f"https://{TAG.lower()}dealer.example/p/1")
        # make it the most eligible candidate
        await db.execute(
            "INSERT INTO discovery_cursor (product_id, last_searched_at) VALUES ($1, NULL) "
            "ON CONFLICT (product_id) DO UPDATE SET last_searched_at = NULL, "
            "next_eligible_at = NULL", covered["id"])
        p3 = await planner.plan(300)
        check("product whose only seller is sitemap-covered is skipped",
              covered["id"] in [x.product_id for x in p3], False)
        note("search finds DEALERS; a free sitemap scan then enumerates their catalogue")

        print("\n== recheck cadence (crawl-side saving) ==")
        check("open violation stays at 24h",
              planner.next_interval_hours(96, False, True, 9), 24)
        check("price moved -> back to 24h",
              planner.next_interval_hours(96, True, False, 0), 24)
        check("stable, streak below threshold -> hold",
              planner.next_interval_hours(24, False, False, 1), 24)
        check("stable, streak reached -> double",
              planner.next_interval_hours(24, False, False, 3), 48)
        check("doubles again", planner.next_interval_hours(48, False, False, 4), 96)
        check("capped at one week", planner.next_interval_hours(96, False, False, 9), 168)
        check("stays capped", planner.next_interval_hours(168, False, False, 20), 168)
    finally:
        await cleanup()
        await db.close_pool()

    print()
    if fails:
        print(f"FAILED: {fails}")
        return 1
    print("ALL PLANNER CHECKS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
