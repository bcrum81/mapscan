"""eBay Marketplace Account Deletion/Closure notifications.

eBay will not activate a production keyset until this endpoint exists and
answers their challenge, which is why the production keys returned
`invalid_client` while the sandbox ones worked. It is a compliance
requirement, not an optional feature.

Two jobs on one URL:

  GET  ?challenge_code=X  -- eBay's ownership check. Answer with
       sha256(challengeCode + verificationToken + endpointUrl), hex, as
       {"challengeResponse": "..."}. The three parts are concatenated in that
       exact order with no separator, and `endpointUrl` must be byte-identical
       to the URL registered in the developer portal -- a trailing slash or a
       http/https mismatch changes the digest and eBay rejects it. That is the
       single most common way this fails, so the URL used is logged.

  POST -- an actual deletion notice. eBay retries on any non-2xx and disables
       a keyset whose endpoint keeps failing, so this ALWAYS answers 200 once
       the body is readable. A processing problem is logged and retried on our
       side; it is never signalled by failing their delivery.

What we hold that belongs to an eBay user: the seller's username, on listings
discovered through the Browse API. On notice, that username is cleared. The
violation and its price history survive without it -- once the username is
gone the row is no longer about a person, it is about a listing. See
`handle_deletion` for the retention note.
"""
from __future__ import annotations

import hashlib
import logging
from typing import Any, Optional

from app import db, settings_store
from app.config import settings

log = logging.getLogger(__name__)

# The path half of the endpoint. Kept here so the route, the digest and the
# admin UI cannot drift apart.
PATH = "/ebay/marketplace-account-deletion"

# eBay's stated bounds for the verification token.
TOKEN_MIN, TOKEN_MAX = 32, 80


async def endpoint_url() -> str:
    """The exact URL eBay has registered, which the digest is computed over.

    Overridable because the digest depends on an exact string match and the
    registered value is whatever was typed into eBay's portal -- if that ever
    differs from PUBLIC_BASE_URL + PATH, the override is the fix.
    """
    override = (await settings_store.get("ebay_deletion_endpoint", "") or "").strip()
    if override:
        return override
    return settings.public_base_url.rstrip("/") + PATH


def challenge_response(challenge_code: str, token: str, endpoint: str) -> str:
    """sha256(challengeCode + verificationToken + endpointUrl) as lowercase hex."""
    h = hashlib.sha256()
    h.update(challenge_code.encode("utf-8"))
    h.update(token.encode("utf-8"))
    h.update(endpoint.encode("utf-8"))
    return h.hexdigest()


def token_problem(token: str) -> Optional[str]:
    """Why eBay would reject this verification token, or None if it is fine."""
    if not token:
        return "not set"
    if not (TOKEN_MIN <= len(token) <= TOKEN_MAX):
        return f"must be {TOKEN_MIN}-{TOKEN_MAX} characters (this one is {len(token)})"
    bad = {c for c in token if not (c.isalnum() or c in "_-")}
    if bad:
        return ("may only contain letters, digits, underscore and hyphen "
                f"(found {''.join(sorted(bad))!r})")
    return None


def _extract(payload: dict[str, Any]) -> tuple[Optional[str], Optional[str], Optional[str]]:
    """Pull (notification_id, username, user_id) out of eBay's envelope.

    Defensive about shape: this is parsing someone else's payload on a public
    endpoint, and a KeyError here would turn into a 500 that eBay reads as a
    dead endpoint.
    """
    note = payload.get("notification") or {}
    data = note.get("data") or {}
    return (
        note.get("notificationId"),
        data.get("username"),
        data.get("userId"),
    )


async def handle_deletion(payload: dict[str, Any]) -> dict[str, Any]:
    """Honour a deletion notice and record that we did.

    RETENTION NOTE -- the default is to clear the username and keep the
    violation. Clearing is what the obligation is actually about: the username
    is the personal data, while "this URL was priced below MAP on this date" is
    a fact about a listing. Keeping the violation also means a dealer notice
    already sent does not become unsupportable overnight.

    If a stricter reading is wanted, the listing rows can be deleted outright
    instead -- but that is a policy decision with legal weight, so it is not
    made silently here.

    The audit row stores eBay's own notification id plus a SHA-256 of the
    username: enough to prove the notice was processed and to drop duplicates,
    without keeping the name it was about.
    """
    notification_id, username, user_id = _extract(payload)

    if not username:
        log.warning("eBay deletion notice %s carried no username; nothing to clear",
                    notification_id)
        await _record(notification_id, None, 0)
        return {"anonymized": 0, "username_present": False}

    user_sha = hashlib.sha256(username.encode("utf-8")).hexdigest()

    # Already handled? eBay retries, and a retry must not be treated as new.
    if notification_id:
        seen = await db.fetchval(
            "SELECT id FROM ebay_deletion_requests WHERE notification_id = $1",
            notification_id)
        if seen is not None:
            log.info("eBay deletion notice %s already processed; ignoring retry",
                     notification_id)
            return {"anonymized": 0, "duplicate": True}

    cleared = await db.fetchval(
        """
        WITH hit AS (
            UPDATE listings
               SET marketplace_seller = NULL
             WHERE discovered_via = 'ebay'
               AND marketplace_seller = $1
            RETURNING id
        )
        SELECT count(*) FROM hit
        """,
        username,
    ) or 0

    await _record(notification_id, user_sha, cleared)
    log.info("eBay account deletion honoured (notice %s): cleared the seller name "
             "from %d listing(s)", notification_id, cleared)
    return {"anonymized": cleared, "username_present": True}


async def _record(notification_id: Optional[str], username_sha: Optional[str],
                  cleared: int) -> None:
    await db.execute(
        """
        INSERT INTO ebay_deletion_requests
               (notification_id, username_sha256, listings_anonymized)
        VALUES ($1, $2, $3)
        ON CONFLICT (notification_id) DO NOTHING
        """,
        notification_id, username_sha, cleared,
    )
