"""Print the stored eBay verification token, for pasting into eBay's portal.

This value is a shared secret with eBay by design -- it has to be typed into
their Alerts & Notifications form -- so unlike an API key it is meant to be
readable back out. Everything else in app_settings stays write-only.
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import db, settings_store, ebay_notify


async def main() -> int:
    await db.init_pool()
    token = await settings_store.get("ebay_verification_token", "") or ""
    endpoint = await ebay_notify.endpoint_url()
    await db.close_pool()

    if not token:
        print("No verification token is set.")
        print("Set one in Settings -> eBay -> Verification token.")
        return 1

    problem = ebay_notify.token_problem(token)
    print()
    print("Paste these into developer.ebay.com -> your app ->")
    print("Alerts & Notifications -> Marketplace Account Deletion:")
    print()
    print(f"  Endpoint URL       {endpoint}")
    print(f"  Verification token {token}")
    print()
    print(f"  (token is {len(token)} chars"
          + (f"; WARNING: {problem}" if problem else "; valid for eBay's rules")
          + ")")
    print()
    return 0


sys.exit(asyncio.run(main()))
