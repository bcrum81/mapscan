"""eBay Browse API client.

Used because nobody is authorised to resell on eBay, so every listing found is a
violation regardless of price. Searching by GTIN is exact, which matters more
here than anywhere else: an unauthorised-listing finding is an accusation, and a
fuzzy title match is not a good enough basis for one.

Auth is the client-credentials grant, which mints an *Application* token -- no
user consent needed since we only read public listings. **The token endpoint is
limited to 1,000 requests/day**, far below the Browse call allowance, so the
token is cached until shortly before it expires. Minting one per search would
exhaust the day's allowance in a single sweep of a 246-product catalogue.
"""
from __future__ import annotations

import base64
import logging
import time
from dataclasses import dataclass
from decimal import Decimal
from typing import Optional

import httpx

from app import settings_store

log = logging.getLogger(__name__)

TOKEN_URL = "https://api.ebay.com/identity/v1/oauth2/token"
BROWSE_URL = "https://api.ebay.com/buy/browse/v1/item_summary/search"
SCOPE = "https://api.ebay.com/oauth/api_scope"

# Sandbox equivalents, for validating credentials without touching production.
SANDBOX_TOKEN_URL = "https://api.sandbox.ebay.com/identity/v1/oauth2/token"
SANDBOX_BROWSE_URL = "https://api.sandbox.ebay.com/buy/browse/v1/item_summary/search"

_token_cache: dict[str, tuple[str, float]] = {}


@dataclass
class EbayListing:
    item_id: str
    title: str
    url: str
    price: Optional[Decimal] = None
    currency: str = "USD"
    seller: Optional[str] = None
    condition: Optional[str] = None
    gtin: Optional[str] = None
    mpn: Optional[str] = None
    matched_via: str = "gtin"


class EbayError(Exception):
    pass


async def _credentials() -> tuple[str, str, bool]:
    app_id = await settings_store.get("ebay_app_id", "") or ""
    cert_id = await settings_store.get("ebay_cert_id", "") or ""
    sandbox = (await settings_store.get("ebay_sandbox", "") or "").lower() in (
        "1", "true", "yes", "on")
    return app_id.strip(), cert_id.strip(), sandbox


async def configured() -> bool:
    app_id, cert_id, _ = await _credentials()
    return bool(app_id and cert_id)


async def get_token(force: bool = False) -> Optional[str]:
    """Mint (or reuse) an Application access token.

    Cached aggressively: the minting endpoint allows only 1,000 calls a day,
    while a single catalogue sweep makes hundreds of Browse calls.
    """
    app_id, cert_id, sandbox = await _credentials()
    if not (app_id and cert_id):
        return None

    key = f"{app_id}:{'sandbox' if sandbox else 'prod'}"
    cached = _token_cache.get(key)
    # Refresh a minute early rather than racing the expiry.
    if cached and not force and cached[1] > time.time() + 60:
        return cached[0]

    basic = base64.b64encode(f"{app_id}:{cert_id}".encode()).decode()
    url = SANDBOX_TOKEN_URL if sandbox else TOKEN_URL
    try:
        async with httpx.AsyncClient(timeout=25) as client:
            resp = await client.post(
                url,
                headers={"Authorization": f"Basic {basic}",
                         "Content-Type": "application/x-www-form-urlencoded"},
                data={"grant_type": "client_credentials", "scope": SCOPE},
            )
    except Exception as exc:
        raise EbayError(f"token request failed: {type(exc).__name__}: {exc}") from exc

    if resp.status_code >= 300:
        raise EbayError(f"token HTTP {resp.status_code}: {resp.text[:200]}")

    data = resp.json()
    token = data.get("access_token")
    if not token:
        raise EbayError("token response contained no access_token")
    _token_cache[key] = (token, time.time() + int(data.get("expires_in", 7200)))
    log.info("eBay application token minted (expires in %ss)", data.get("expires_in"))
    return token


def _parse_item(item: dict, matched_via: str) -> Optional[EbayListing]:
    item_id = item.get("itemId")
    url = item.get("itemWebUrl")
    if not item_id or not url:
        return None
    price = None
    currency = "USD"
    p = item.get("price") or {}
    if p.get("value") is not None:
        try:
            price = Decimal(str(p["value"]))
        except Exception:
            price = None
        currency = p.get("currency") or "USD"
    seller = (item.get("seller") or {}).get("username")
    return EbayListing(
        item_id=str(item_id),
        title=item.get("title") or "",
        url=url,
        price=price,
        currency=currency,
        seller=seller,
        condition=item.get("condition"),
        matched_via=matched_via,
    )


async def _search(params: dict, matched_via: str,
                  limit: int = 50) -> list[EbayListing]:
    token = await get_token()
    if not token:
        return []
    _, _, sandbox = await _credentials()
    url = SANDBOX_BROWSE_URL if sandbox else BROWSE_URL
    params = {**params, "limit": str(min(limit, 200))}
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(
                url,
                params=params,
                headers={
                    "Authorization": f"Bearer {token}",
                    # Required by Browse; also scopes results to the US site.
                    "X-EBAY-C-MARKETPLACE-ID": "EBAY_US",
                    "Accept": "application/json",
                },
            )
    except Exception as exc:
        raise EbayError(f"search failed: {type(exc).__name__}: {exc}") from exc

    if resp.status_code == 401:
        # Token rejected -- mint once more before giving up.
        await get_token(force=True)
        raise EbayError("search returned 401; token refreshed, retry the sweep")
    if resp.status_code == 429:
        raise EbayError("eBay rate limit reached (HTTP 429)")
    if resp.status_code >= 300:
        raise EbayError(f"search HTTP {resp.status_code}: {resp.text[:200]}")

    body = resp.json() or {}
    out: list[EbayListing] = []
    for item in body.get("itemSummaries") or []:
        parsed = _parse_item(item, matched_via)
        if parsed is not None:
            out.append(parsed)
    return out


async def search_by_gtin(upc: str, limit: int = 50) -> list[EbayListing]:
    """Exact GTIN search -- the only basis precise enough to accuse a seller."""
    if not upc:
        return []
    return await _search({"gtin": upc}, "gtin", limit)


async def search_by_keyword(query: str, limit: int = 50) -> list[EbayListing]:
    """Free-text search, for products with no UPC and for brand sweeps.

    Results are corroborated by the normal matcher before anything is recorded:
    a keyword hit alone is not evidence a listing is ours.
    """
    if not query:
        return []
    return await _search({"q": query}, "keyword", limit)


async def verify_credentials() -> tuple[bool, str]:
    """Prove the keys work, for the Settings test button."""
    app_id, cert_id, sandbox = await _credentials()
    if not (app_id and cert_id):
        return False, "eBay App ID and Cert ID are both required."
    try:
        token = await get_token(force=True)
    except EbayError as exc:
        return False, str(exc)
    if not token:
        return False, "No token returned."
    try:
        hits = await search_by_keyword("holster", limit=1)
    except EbayError as exc:
        return False, f"Token minted, but search failed: {exc}"
    env = "sandbox" if sandbox else "production"
    return True, f"eBay {env} credentials work (token minted, search returned {len(hits)} item(s))."
