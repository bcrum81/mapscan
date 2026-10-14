"""eBay token caching -- the 1,000/day mint cap makes this load-bearing.

A catalogue sweep makes one Browse call per product (246 today). If each one
minted its own token, four sweeps would exhaust the daily cap and the feature
would fail in a way that looks like an auth problem rather than a quota one.

No network, no credentials, no database: httpx and the settings lookup are
both stubbed, so this runs anywhere.
"""
import asyncio, sys, time, types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from worker import ebay

FAILS = []
mint_calls = 0
search_calls = 0


def check(label, got, want):
    ok = got == want
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}: got {got!r}, want {want!r}")
    if not ok:
        FAILS.append(label)


class FakeResponse:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self._payload = payload
        self.text = str(payload)

    def json(self):
        return self._payload


class FakeClient:
    def __init__(self, *a, **kw):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, **kw):          # token endpoint
        global mint_calls
        mint_calls += 1
        return FakeResponse(200, {"access_token": f"tok-{mint_calls}",
                                  "expires_in": 7200})

    async def get(self, url, **kw):           # browse endpoint
        global search_calls
        search_calls += 1
        return FakeResponse(200, {"itemSummaries": [{
            "itemId": "v1|123|0",
            "itemWebUrl": "https://www.ebay.com/itm/123",
            "title": "Henry Holsters IWB",
            "price": {"value": "59.99", "currency": "USD"},
            "seller": {"username": "somereseller"},
            "condition": "New",
        }]})


async def main():
    global mint_calls, search_calls
    ebay.httpx = types.SimpleNamespace(AsyncClient=FakeClient)

    async def fake_credentials():
        return ("APPID", "CERTID", False)
    ebay._credentials = fake_credentials
    ebay._token_cache.clear()

    print("\n1. A 246-product sweep mints exactly one token")
    for _ in range(246):
        await ebay.search_by_gtin("0850000123458")
    check("browse calls", search_calls, 246)
    check("token mints", mint_calls, 1)

    print("\n2. Parsing pulls the fields the sweep depends on")
    hits = await ebay.search_by_gtin("0850000123458")
    h = hits[0]
    check("price parsed as Decimal", str(h.price), "59.99")
    check("seller username", h.seller, "somereseller")
    check("item url", h.url, "https://www.ebay.com/itm/123")
    check("matched_via", h.matched_via, "gtin")

    print("\n3. An expired cache entry re-mints (and only once)")
    before = mint_calls
    key = "APPID:prod"
    tok, _ = ebay._token_cache[key]
    ebay._token_cache[key] = (tok, time.time() - 1)   # already expired
    await ebay.search_by_gtin("0850000123458")
    await ebay.search_by_gtin("0850000123458")
    check("re-minted exactly once after expiry", mint_calls - before, 1)

    print("\n4. Near-expiry token refreshes early rather than racing the clock")
    before = mint_calls
    tok, _ = ebay._token_cache[key]
    ebay._token_cache[key] = (tok, time.time() + 30)  # inside the 60s margin
    await ebay.search_by_gtin("0850000123458")
    check("refreshed inside the 60s margin", mint_calls - before, 1)

    print("\n5. No credentials -> no network call at all")
    async def no_credentials():
        return ("", "", False)
    ebay._credentials = no_credentials
    ebay._token_cache.clear()
    before_m, before_s = mint_calls, search_calls
    out = await ebay.search_by_gtin("0850000123458")
    check("returns empty", out, [])
    check("no token minted", mint_calls - before_m, 0)
    check("no search issued", search_calls - before_s, 0)

    print("\n" + ("ALL PASS" if not FAILS else f"FAILURES: {FAILS}"))
    return 1 if FAILS else 0


sys.exit(asyncio.run(main()))
