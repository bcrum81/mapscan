"""Prohibited-channel violations: the listing existing IS the violation.

eBay is policy='prohibited' because no reseller is authorised there, so a
listing is flagged whatever its price. That is the opposite of the MAP path,
which only opens below MAP and only after a debounce streak. Test 2 is the
control that proves the dispatch actually branches instead of both paths
happening to agree.

Requires the database. Creates rows under the reserved .invalid TLD so they
can never collide with a real seller, and removes exactly what it created.
"""
import asyncio, sys
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import db
from worker import violations

HOST = "synthetic-ebay-test.invalid"
FAILS = []


def check(label, got, want):
    ok = got == want
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}: got {got!r}, want {want!r}")
    if not ok:
        FAILS.append(label)


async def mk_listing(seller_id, product_id, url):
    return await db.fetchval(
        "INSERT INTO listings (product_id, seller_id, url, status, discovered_via) "
        "VALUES ($1,$2,$3,'confirmed','test') RETURNING id",
        product_id, seller_id, url)


async def cleanup(created):
    """Best-effort, and never let one failure strand the rest of the rows."""
    for lid in created["listings"]:
        for sql in (
            "DELETE FROM evidence WHERE violation_id IN "
            "  (SELECT id FROM violations WHERE listing_id=$1)",
            "DELETE FROM violations WHERE listing_id=$1",
            "DELETE FROM price_observations WHERE listing_id=$1",
            "DELETE FROM listings WHERE id=$1",
        ):
            try:
                await db.execute(sql, lid)
            except Exception as exc:
                print(f"  cleanup warning ({sql.split()[2]}): {type(exc).__name__}: {exc}")
    for pid in created["products"]:
        try:
            await db.execute("DELETE FROM products WHERE id=$1", pid)
        except Exception as exc:
            print(f"  cleanup warning (products): {exc}")
    for sid in created["sellers"]:
        try:
            await db.execute("DELETE FROM sellers WHERE id=$1", sid)
        except Exception as exc:
            print(f"  cleanup warning (sellers): {exc}")


async def main():
    await db.init_pool()
    created = {"sellers": [], "products": [], "listings": []}
    try:
        proh = await db.fetchval(
            "INSERT INTO sellers (domain, name, is_authorized, policy) "
            "VALUES ($1,'synthetic prohibited',false,'prohibited') RETURNING id", HOST)
        mapseller = await db.fetchval(
            "INSERT INTO sellers (domain, name, is_authorized, policy) "
            "VALUES ($1,'synthetic map',false,'map') RETURNING id", "map-" + HOST)
        created["sellers"] += [proh, mapseller]

        prod = await db.fetchval(
            "INSERT INTO products (sku, title, upc, map_price, msrp, active) "
            "VALUES ('SYNTH-TEST-1','Synthetic Test Holster','0000000000000',"
            "100.00,120.00,true) RETURNING id")
        created["products"].append(prod)

        MAP = Decimal("100.00")

        print("\n1. Prohibited channel, price ABOVE MAP")
        l1 = await mk_listing(proh, prod, f"https://{HOST}/itm/111")
        created["listings"].append(l1)
        await violations.evaluate(l1, Decimal("119.99"), MAP,
                                  f"https://{HOST}/itm/111", policy="prohibited")
        row = await db.fetchrow(
            "SELECT status, kind FROM violations WHERE listing_id=$1", l1)
        check("violation opened despite compliant price", row is not None, True)
        check("kind", row["kind"] if row else None, "unauthorized")
        check("status", row["status"] if row else None, "open")

        print("\n2. CONTROL -- identical price under 'map' policy opens nothing")
        l2 = await mk_listing(mapseller, prod, f"https://map-{HOST}/p/222")
        created["listings"].append(l2)
        await violations.evaluate(l2, Decimal("119.99"), MAP,
                                  f"https://map-{HOST}/p/222", policy="map")
        check("no violation",
              await db.fetchval("SELECT count(*) FROM violations WHERE listing_id=$1", l2), 0)

        print("\n3. Prohibited channel, price UNKNOWN (auction / best offer)")
        l3 = await mk_listing(proh, prod, f"https://{HOST}/itm/333")
        created["listings"].append(l3)
        await violations.evaluate(l3, None, MAP, f"https://{HOST}/itm/333",
                                  policy="prohibited")
        row3 = await db.fetchrow(
            "SELECT kind, opening_price FROM violations WHERE listing_id=$1", l3)
        check("opens with no price at all",
              row3 is not None and row3["kind"] == "unauthorized", True)
        check("opening_price stays null", row3["opening_price"] if row3 else "?", None)

        print("\n4. Idempotency across rechecks")
        await violations.evaluate(l1, Decimal("95.00"), MAP,
                                  f"https://{HOST}/itm/111", policy="prohibited")
        check("still exactly one violation",
              await db.fetchval("SELECT count(*) FROM violations WHERE listing_id=$1", l1), 1)
        check("lowest_price tracked downward",
              await db.fetchval("SELECT lowest_price FROM violations WHERE listing_id=$1", l1),
              Decimal("95.00"))
    finally:
        await cleanup(created)
        left = await db.fetchval(
            "SELECT count(*) FROM sellers WHERE domain LIKE '%.invalid'")
        print(f"\n  cleanup: synthetic sellers remaining = {left}")
        if left:
            FAILS.append("cleanup left rows behind")
        await db.close_pool()

    print("\n" + ("ALL PASS" if not FAILS else f"FAILURES: {FAILS}"))
    return 1 if FAILS else 0


sys.exit(asyncio.run(main()))
