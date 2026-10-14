#!/usr/bin/env python3
"""Bootstrap the first administrator.

Creates an admin with a generated initial password and `must_change_password`
set, so the very first sign-in is forced through the change-password page. The
password is printed once and never stored in plaintext.

    python -m scripts.create_admin admin@example.com

Re-running for an existing email resets that account's password instead of
failing, which is also the recovery path if you lock yourself out.
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import auth, db          # noqa: E402
from app.config import settings   # noqa: E402


async def run(email: str, password: str | None) -> int:
    await db.init_pool()
    try:
        clean = email.strip()
        lower = clean.lower()
        if "@" not in clean:
            print(f"error: {clean!r} is not an email address", file=sys.stderr)
            return 1

        generated = password is None
        pw = password or auth.generate_password()
        pw_hash = await auth.hash_password(pw)

        existing = await db.fetchrow(
            "SELECT id, is_admin FROM users WHERE email_lower = $1", lower
        )
        if existing:
            await db.execute(
                """
                UPDATE users
                   SET password_hash = $2, must_change_password = true,
                       is_admin = true, is_active = true, password_changed_at = NULL
                 WHERE id = $1
                """,
                existing["id"], pw_hash,
            )
            # Kill any outstanding invite/reset links for this account.
            await db.execute(
                "UPDATE auth_tokens SET used_at = now() "
                "WHERE user_id = $1 AND used_at IS NULL",
                existing["id"],
            )
            action = "RESET"
            uid = existing["id"]
        else:
            uid = await db.fetchval(
                """
                INSERT INTO users (email, email_lower, password_hash, is_admin,
                                   is_active, must_change_password)
                VALUES ($1,$2,$3,true,true,true)
                RETURNING id
                """,
                clean, lower, pw_hash,
            )
            action = "CREATED"

        await auth.record_event(
            "password_changed", lower, uid, None, f"bootstrap via create_admin ({action})"
        )

        bar = "=" * 66
        print(bar)
        print(f"  ADMIN {action}: {clean}")
        print(bar)
        if generated:
            print(f"  Initial password:  {pw}")
            print()
            print("  Store this now -- it is not recoverable and is not stored in")
            print("  plaintext anywhere. You will be forced to change it on first")
            print("  sign-in before any page is reachable.")
        else:
            print("  Password set from the value you supplied.")
            print("  You will still be forced to change it on first sign-in.")
        print()
        print(f"  Sign in at: {settings.public_base_url.rstrip('/')}/login")
        print(bar)
        return 0
    finally:
        await db.close_pool()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("email")
    ap.add_argument("--password", default=None,
                    help="set a specific initial password instead of generating one")
    args = ap.parse_args()
    if args.password:
        try:
            auth.validate_password(args.password, args.email)
        except Exception as exc:
            detail = getattr(exc, "detail", str(exc))
            print(f"error: {detail}", file=sys.stderr)
            return 1
    return asyncio.run(run(args.email, args.password))


if __name__ == "__main__":
    raise SystemExit(main())
