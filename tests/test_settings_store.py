#!/usr/bin/env python3
"""Verify the encrypted settings store against the live database.

DESTRUCTIVE WITHOUT THE GUARD BELOW. This exercises the real app_settings
table, and the only keys set_value() accepts are the real ones the application
uses -- so the test necessarily writes over live credentials. On 2026-08-20 a
run of this file overwrote and then deleted a production Brave Search key,
which was unrecoverable because it had been entered after that night's backup.

Every key it touches is therefore snapshotted first and restored verbatim in a
finally block, ciphertext and all, whether the test passes, fails, or raises.
Add any new key to TOUCHED_KEYS before using it here.

Run from the app directory against a real database:
    set -a; . ./.env; set +a
    venv/bin/python tests/test_settings_store.py
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import db, settings_store  # noqa: E402

SECRET = "BSA-test-key-do-not-use-123456"

# Every app_settings key this file writes. Snapshotted and restored around the
# run so a test can never cost a real credential.
TOUCHED_KEYS = ("brave_search_key", "brave_monthly_cap", "extraction_model")

fails: list[str] = []


async def snapshot(keys) -> list[dict]:
    rows = await db.fetch(
        "SELECT key, value_enc, is_secret, updated_by, updated_at "
        "FROM app_settings WHERE key = ANY($1::text[])", list(keys))
    print(f"  snapshot: {len(rows)} existing value(s) held for restore "
          f"({', '.join(r['key'] for r in rows) or 'none'})")
    return [dict(r) for r in rows]


async def restore(keys, rows) -> None:
    """Put the real values back exactly as they were, ciphertext included."""
    await db.execute("DELETE FROM app_settings WHERE key = ANY($1::text[])", list(keys))
    for r in rows:
        await db.execute(
            "INSERT INTO app_settings (key, value_enc, is_secret, updated_by, updated_at) "
            "VALUES ($1,$2,$3,$4,$5)",
            r["key"], r["value_enc"], r["is_secret"], r["updated_by"], r["updated_at"])
    # Bump the version so the API and worker reload rather than serving the
    # test's values from their 30s cache.
    await db.execute("UPDATE settings_version SET version = version + 1 WHERE id = 1")
    await settings_store.refresh(force=True)
    print(f"  restored: {len(rows)} value(s) put back")


def check(name, got, want):
    ok = got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {name}: {got!r}" + ("" if ok else f" want {want!r}"))
    if not ok:
        fails.append(name)


async def main() -> int:
    await db.init_pool()
    saved = await snapshot(TOUCHED_KEYS)
    try:
        check("encryption available", settings_store.encryption_available(), True)

        # --- round trip -----------------------------------------------------
        await settings_store.set_value("brave_search_key", SECRET)
        check("round-trip", await settings_store.get("brave_search_key"), SECRET)

        # --- the DB must hold ciphertext, not the value ----------------------
        raw = await db.fetchval(
            "SELECT value_enc FROM app_settings WHERE key = 'brave_search_key'"
        )
        blob = bytes(raw)
        print(f"        stored as {len(blob)} bytes")
        check("plaintext absent from stored bytes", SECRET.encode() in blob, False)
        check("fernet envelope", blob[:4], b"gAAA")

        # --- status() must never leak a secret -------------------------------
        st = await settings_store.status()
        check("status present", st["brave_search_key"]["present"], True)
        check("status value withheld", st["brave_search_key"]["value"], None)
        check("status source", st["brave_search_key"]["source"], "database")
        check("status marks it secret", st["brave_search_key"]["is_secret"], True)

        # --- non-secrets ARE readable back, for form pre-fill ----------------
        await settings_store.set_value("brave_monthly_cap", "900")
        check("int read", await settings_store.get_int("brave_monthly_cap", 1000), 900)
        st = await settings_store.status()
        check("non-secret value returned", st["brave_monthly_cap"]["value"], "900")

        # --- db overrides env ------------------------------------------------
        import os
        os.environ["BRAVE_MONTHLY_BUDGET"] = "5"
        await settings_store.refresh(force=True)
        check("database wins over environment",
              await settings_store.get_int("brave_monthly_cap", 1000), 900)

        # --- env is the fallback when db is empty ----------------------------
        await settings_store.set_value("brave_monthly_cap", None)
        check("env fallback after clear",
              await settings_store.get_int("brave_monthly_cap", 1000), 5)
        del os.environ["BRAVE_MONTHLY_BUDGET"]

        # --- clearing ---------------------------------------------------------
        await settings_store.set_value("brave_search_key", None)
        check("cleared", await settings_store.get("brave_search_key"), None)
        check("row deleted",
              await db.fetchval("SELECT count(*) FROM app_settings WHERE key='brave_search_key'"), 0)

        # --- version bump drives cross-process invalidation -------------------
        v1 = await db.fetchval("SELECT version FROM settings_version WHERE id=1")
        await settings_store.set_value("extraction_model", "gpt-4o-mini")
        v2 = await db.fetchval("SELECT version FROM settings_version WHERE id=1")
        check("version bumped on write", v2 > v1, True)

        # --- unknown keys rejected --------------------------------------------
        try:
            await settings_store.set_value("not_a_real_setting", "x")
            check("unknown key rejected", "accepted", "rejected")
        except ValueError:
            print("  PASS  unknown key rejected")

    finally:
        # Before close_pool, and outside the try's success path, so an assertion
        # failure or an exception mid-test still gives the credentials back.
        await restore(TOUCHED_KEYS, saved)
        await db.close_pool()

    print()
    if fails:
        print(f"FAILED: {fails}")
        return 1
    print("ALL SETTINGS-STORE CHECKS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
