"""Mint a one-time password-reset link for a user, printed to this terminal.

For when nobody can log in to use the admin UI -- which is the situation this
exists for, since the reset flow is otherwise only reachable while signed in.
Email delivery is not involved, so it works even when the mail provider is
mid-setup.

Usage (from the app directory, with .env loaded):
    python tools/reset_password.py admin@example.com
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import auth, db


async def main() -> int:
    if len(sys.argv) != 2:
        print("usage: mapscan-reset <email>")
        return 2
    email = sys.argv[1].strip().lower()

    await db.init_pool()
    try:
        row = await db.fetchrow(
            "SELECT id, email, is_active FROM users WHERE email_lower = $1", email)
        if row is None:
            print(f"No user with email {email!r}.")
            known = await db.fetch("SELECT email FROM users ORDER BY id")
            print("Known users: " + ", ".join(r["email"] for r in known))
            return 1
        if not row["is_active"]:
            print(f"{row['email']} is DEACTIVATED -- reactivate it first, or the "
                  f"link will not let them in.")
            return 1

        raw = await auth.issue_token(row["id"], "reset", row["id"])
        print()
        print(f"One-time reset link for {row['email']} "
              f"(valid {auth.TOKEN_TTL_HOURS}h, single use):")
        print()
        print(f"  {auth.invite_url(raw)}")
        print()
        print("Anyone holding this link can set the password. Do not paste it into")
        print("a chat, a ticket, or anywhere it will be retained. Any previously")
        print("issued reset link for this user has just been invalidated.")
        print()
        return 0
    finally:
        await db.close_pool()


raise SystemExit(asyncio.run(main()))
