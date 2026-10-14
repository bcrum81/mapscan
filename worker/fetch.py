"""Tiered fetcher.

Tier 1 is plain httpx and costs nothing. Tier 2 is a paid vendor and is used
only for (a) domains that have demonstrably blocked tier 1, and (b) evidence
screenshots, which happen on violation-open rather than on every check.

A domain is promoted to tier 2 by observed failure, never by configuration, so
we cannot accidentally pay for requests tier 1 would have served.
"""
from __future__ import annotations

import asyncio
import gzip
import logging
import time
import urllib.robotparser as robotparser
from dataclasses import dataclass
from typing import Optional
from urllib.parse import urljoin, urlparse

import httpx

from app.config import settings

log = logging.getLogger(__name__)

# Status codes that mean "bot-blocked", as distinct from "page is genuinely gone".
# Only these promote a domain to tier 2 -- a 404 is not a blocking signal.
BLOCK_STATUSES = {401, 403, 406, 409, 429, 451, 503}
TIER1_FAILURES_BEFORE_PROMOTION = 2

# A WAF often does not return a 403. It answers 307/302 to an interstitial and
# serves that with a 200, so a naive client reports success and hands a block
# page to the parser. Observed on shop.bigdealer.example: 307 ->
# /forbidden.html?... -> 200, 348 KB of "captcha"/"blocked" markup.
#
# Detecting this matters for data integrity, not just tidiness: an undetected
# block page becomes an extraction failure or, worse, a bogus price.
_BLOCK_PATH_MARKERS = (
    "forbidden", "access-denied", "accessdenied", "denied", "blocked",
    "captcha", "challenge", "bot-detect", "botdetect", "distil_r_", "px-captcha",
    "cdn-cgi/l/chk", "unavailable",
)

# Multi-word phrases only. Single words like "blocked" appear innocently in real
# product copy, and a false positive here would retire a working listing.
_BLOCK_BODY_PHRASES = (
    "access denied", "you have been blocked", "unusual traffic",
    "are you a robot", "verify you are human", "verify you are a human",
    "enable javascript and cookies", "cf-browser-verification",
    "checking your browser before", "request blocked", "bot detected",
    "automated access", "please complete the security check",
)


def looks_like_block_page(requested_url: str, final_url: str,
                          html: Optional[str]) -> Optional[str]:
    """Return a reason string if this response looks like an anti-bot wall."""
    req_path = (urlparse(requested_url).path or "").lower()
    fin = urlparse(final_url)
    fin_path = (fin.path or "").lower()

    # Redirected somewhere block-shaped that we did not ask for.
    if fin_path != req_path:
        for marker in _BLOCK_PATH_MARKERS:
            if marker in fin_path or marker in (fin.query or "").lower():
                return f"redirected to {marker!r} ({final_url[:120]})"

    if html:
        # Only inspect the head: matching the whole document raises the odds of
        # tripping on ordinary page copy.
        head = html[:20000].lower()
        for phrase in _BLOCK_BODY_PHRASES:
            if phrase in head:
                return f"body contains {phrase!r}"
    return None


@dataclass
class FetchResult:
    url: str
    final_url: str
    status: int
    html: Optional[str] = None
    screenshot: Optional[bytes] = None
    tier: int = 1
    error: Optional[str] = None
    blocked: bool = False
    etag: Optional[str] = None
    last_modified: Optional[str] = None

    @property
    def ok(self) -> bool:
        return self.status == 200 and bool(self.html)

    @property
    def not_modified(self) -> bool:
        """304: the page is byte-identical to what we last saw.

        Worth handling explicitly rather than treating as an error -- it is the
        cheapest possible successful check. No body transfers, no parsing, and
        where a paid tier-2 fetch would otherwise run, no credit is spent.
        """
        return self.status == 304


def registrable_domain(url: str) -> str:
    """Lowercased host with a leading www. stripped. Good enough for grouping."""
    host = (urlparse(url).hostname or "").lower()
    return host[4:] if host.startswith("www.") else host


class DomainThrottle:
    """Per-domain politeness gate.

    Concurrency is capped globally, but the delay is enforced per domain so one
    slow dealer cannot stall the whole sweep -- and so we never hammer a single
    host, which is the fastest way to get the shop's IP blocked.
    """

    def __init__(self, default_delay: float) -> None:
        self._default = default_delay
        self._last: dict[str, float] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._delays: dict[str, float] = {}

    def set_delay(self, domain: str, delay: Optional[float]) -> None:
        if delay and delay > 0:
            self._delays[domain] = max(delay, self._default)

    async def wait(self, domain: str) -> None:
        lock = self._locks.setdefault(domain, asyncio.Lock())
        delay = self._delays.get(domain, self._default)
        async with lock:
            elapsed = time.monotonic() - self._last.get(domain, 0.0)
            if elapsed < delay:
                await asyncio.sleep(delay - elapsed)
            self._last[domain] = time.monotonic()


class RobotsCache:
    """robots.txt per domain, fetched once per process run."""

    def __init__(self, client: httpx.AsyncClient) -> None:
        self._client = client
        self._cache: dict[str, robotparser.RobotFileParser] = {}
        self._sitemaps: dict[str, list[str]] = {}

    async def _load(self, domain: str) -> robotparser.RobotFileParser:
        if domain in self._cache:
            return self._cache[domain]
        rp = robotparser.RobotFileParser()
        rp.set_url(f"https://{domain}/robots.txt")
        try:
            resp = await self._client.get(f"https://{domain}/robots.txt", timeout=15)
            if resp.status_code == 200:
                rp.parse(resp.text.splitlines())
                self._sitemaps[domain] = [
                    line.split(":", 1)[1].strip()
                    for line in resp.text.splitlines()
                    if line.lower().startswith("sitemap:")
                ]
            else:
                # No robots.txt is permission, not prohibition.
                rp.parse([])
                self._sitemaps[domain] = []
        except Exception as exc:  # network error -- fail open, but log it
            log.warning("robots.txt fetch failed for %s: %s", domain, exc)
            rp.parse([])
            self._sitemaps[domain] = []
        self._cache[domain] = rp
        return rp

    async def allowed(self, url: str) -> bool:
        if not settings.respect_robots:
            return True
        domain = registrable_domain(url)
        rp = await self._load(domain)
        try:
            return rp.can_fetch(settings.user_agent, url)
        except Exception:
            return True

    async def crawl_delay(self, url: str) -> Optional[float]:
        domain = registrable_domain(url)
        rp = await self._load(domain)
        try:
            d = rp.crawl_delay(settings.user_agent)
            return float(d) if d else None
        except Exception:
            return None

    async def sitemaps(self, domain: str) -> list[str]:
        await self._load(domain)
        return self._sitemaps.get(domain, [])


# Hard ceiling on requests to a single host within one Fetcher session.
# Learned the hard way: an unfiltered scan fired 100+ sitemap requests at
# bigdealer.example in two minutes and tripped their WAF, blocking the whole
# domain. Sitemap filtering makes that unlikely now; this makes it impossible.
MAX_REQUESTS_PER_DOMAIN = 250


class DomainBudgetExceeded(Exception):
    """Raised when one host has absorbed its request ceiling for this session."""


class Fetcher:
    def __init__(self) -> None:
        self._client: Optional[httpx.AsyncClient] = None
        self._sem = asyncio.Semaphore(settings.max_concurrent_fetches)
        self._throttle = DomainThrottle(settings.per_domain_delay)
        # robots.txt and sitemaps are small static XML, not rendered product
        # pages, so they get a lighter delay. A large retailer can declare 194
        # sitemaps -- at the product-page delay that is ~10 minutes of waiting
        # before a scan yields its first URL. A declared Crawl-delay still wins.
        self._static_throttle = DomainThrottle(
            min(1.0, settings.per_domain_delay)
        )
        self.robots: Optional[RobotsCache] = None
        self._request_counts: dict[str, int] = {}
        # Vendor config comes from the encrypted settings store, not the
        # environment -- a key pasted into the Settings page must actually be
        # used. Resolved once per session so a scan does not hit the database
        # on every request.
        self._vendor: str = ""
        self._vendor_key: str = ""

    async def __aenter__(self) -> "Fetcher":
        self._client = httpx.AsyncClient(
            follow_redirects=True,
            timeout=settings.fetch_timeout,
            headers={
                "User-Agent": settings.user_agent,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
            },
            limits=httpx.Limits(max_connections=settings.max_concurrent_fetches * 2),
        )
        self.robots = RobotsCache(self._client)
        await self._load_vendor_config()
        return self

    async def _load_vendor_config(self) -> None:
        from app import settings_store
        try:
            vendor = (await settings_store.get("fetch_vendor", "") or "").strip().lower()
            if vendor == "scrapingbee":
                key = await settings_store.get("scrapingbee_key", "") or ""
            elif vendor == "zyte":
                key = await settings_store.get("zyte_key", "") or ""
            else:
                vendor, key = "", ""
        except Exception as exc:
            # A settings-store failure must not take the whole scan down; tier 1
            # still works without a vendor.
            log.warning("could not load fetch-vendor settings: %s", exc)
            vendor, key = "", ""
        self._vendor, self._vendor_key = vendor, key
        if vendor and not key:
            log.warning("fetch vendor %r is selected but no API key is set", vendor)
        elif vendor:
            log.info("fetch vendor: %s (tier 2 available)", vendor)

    @property
    def vendor_enabled(self) -> bool:
        return bool(self._vendor and self._vendor_key)

    async def __aexit__(self, *exc_info) -> None:
        if self._client is not None:
            await self._client.aclose()

    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            raise RuntimeError("Fetcher must be used as an async context manager")
        return self._client

    def _charge(self, url: str) -> bool:
        """Count a request against the host's ceiling. False once exhausted."""
        domain = registrable_domain(url)
        n = self._request_counts.get(domain, 0) + 1
        self._request_counts[domain] = n
        if n == MAX_REQUESTS_PER_DOMAIN:
            log.warning("request ceiling (%d) reached for %s; no further requests "
                        "to this host this session", MAX_REQUESTS_PER_DOMAIN, domain)
        return n <= MAX_REQUESTS_PER_DOMAIN

    def domain_requests(self, domain: str) -> int:
        return self._request_counts.get(domain, 0)

    # -- tier 1 ------------------------------------------------------------
    async def _tier1(self, url: str, etag: Optional[str] = None,
                     last_modified: Optional[str] = None) -> FetchResult:
        domain = registrable_domain(url)
        delay = await self.robots.crawl_delay(url)
        self._throttle.set_delay(domain, delay)
        await self._throttle.wait(domain)

        # Conditional request: lets the server answer 304 with no body.
        headers = {}
        if etag:
            headers["If-None-Match"] = etag
        if last_modified:
            headers["If-Modified-Since"] = last_modified

        try:
            resp = await self.client.get(url, headers=headers or None)
        except Exception as exc:
            return FetchResult(url, url, 0, tier=1, error=f"{type(exc).__name__}: {exc}")

        if resp.status_code == 304:
            return FetchResult(url, str(resp.url), 304, tier=1,
                               etag=etag, last_modified=last_modified)

        html = resp.text if resp.status_code == 200 else None
        blocked = resp.status_code in BLOCK_STATUSES
        error = None if resp.status_code == 200 else f"HTTP {resp.status_code}"

        # A 200 that is actually a wall.
        if resp.status_code == 200:
            reason = looks_like_block_page(url, str(resp.url), html)
            if reason:
                log.info("block page detected for %s: %s", url, reason)
                blocked = True
                html = None                      # never hand a wall to the parser
                error = f"anti-bot page: {reason}"

        return FetchResult(
            url=url,
            final_url=str(resp.url),
            status=resp.status_code,
            html=html,
            tier=1,
            blocked=blocked,
            error=error,
            etag=resp.headers.get("etag"),
            last_modified=resp.headers.get("last-modified"),
        )

    # -- tier 2 ------------------------------------------------------------
    async def _tier2(self, url: str, screenshot: bool = False) -> FetchResult:
        if not self.vendor_enabled:
            return FetchResult(
                url, url, 0, tier=2,
                error="tier 2 requested but no fetch vendor is configured "
                      "(set one in Settings)",
            )
        if self._vendor == "scrapingbee":
            return await self._scrapingbee(url, screenshot)
        if self._vendor == "zyte":
            return await self._zyte(url, screenshot)
        return FetchResult(url, url, 0, tier=2,
                           error=f"unknown fetch vendor {self._vendor!r}")

    async def _scrapingbee(self, url: str, screenshot: bool) -> FetchResult:
        params = {
            "api_key": self._vendor_key,
            "url": url,
            "render_js": "true",
            "premium_proxy": "true",
            "country_code": "us",
        }
        if screenshot:
            params["screenshot"] = "true"
            params["screenshot_full_page"] = "true"
        try:
            resp = await self.client.get(
                "https://app.scrapingbee.com/api/v1/", params=params, timeout=120
            )
        except Exception as exc:
            return FetchResult(url, url, 0, tier=2, error=f"{type(exc).__name__}: {exc}")
        if resp.status_code != 200:
            return FetchResult(url, url, resp.status_code, tier=2,
                               error=f"scrapingbee HTTP {resp.status_code}")
        if screenshot:
            return FetchResult(url, url, 200, screenshot=resp.content, tier=2)
        return FetchResult(url, url, 200, html=resp.text, tier=2)

    async def _zyte(self, url: str, screenshot: bool) -> FetchResult:
        payload: dict = {"url": url, "browserHtml": True}
        if screenshot:
            payload["screenshot"] = True
            # Without fullPage, Zyte returns the 1920x1080 VIEWPORT only. A
            # dealer product page is routinely 4000-5000px tall, so the price
            # -- the entire point of the capture -- often sits below the fold.
            # Note the format is not ours to choose: Zyte rejects
            # {"format": "png", "fullPage": true} with HTTP 422, "Full page
            # screenshots are only supported with JPEG format". Full-page wins,
            # so these arrive as JPEG and evidence.py names them accordingly.
            payload["screenshotOptions"] = {"fullPage": True}
        try:
            resp = await self.client.post(
                "https://api.zyte.com/v1/extract",
                json=payload,
                auth=(self._vendor_key, ""),
                timeout=120,
            )
        except Exception as exc:
            return FetchResult(url, url, 0, tier=2, error=f"{type(exc).__name__}: {exc}")
        if resp.status_code != 200:
            return FetchResult(url, url, resp.status_code, tier=2,
                               error=f"zyte HTTP {resp.status_code}")
        data = resp.json()
        shot = None
        if screenshot and data.get("screenshot"):
            import base64
            shot = base64.b64decode(data["screenshot"])
        return FetchResult(url, url, 200, html=data.get("browserHtml"),
                           screenshot=shot, tier=2)

    # -- public ------------------------------------------------------------
    async def get(self, url: str, seller_tier: int = 1,
                  etag: Optional[str] = None,
                  last_modified: Optional[str] = None) -> FetchResult:
        """Fetch a page, escalating tier 1 -> tier 2 only on a blocking signal."""
        async with self._sem:
            if not self._charge(url):
                return FetchResult(url, url, 0,
                                   error=f"per-host request ceiling "
                                         f"({MAX_REQUESTS_PER_DOMAIN}) reached")
            if not await self.robots.allowed(url):
                return FetchResult(url, url, 0, error="disallowed by robots.txt")

            if seller_tier >= 2:
                if self.vendor_enabled:
                    # Tier 2 vendors do not proxy conditional headers, so a paid
                    # fetch is always a full fetch. One more reason to keep
                    # domains on tier 1 wherever they will tolerate it.
                    return await self._tier2(url)
                # Promoted to tier 2 but no vendor exists. Falling through to
                # tier 1 rather than failing: a domain that blocked us once is
                # often reachable later, and refusing to try would leave it
                # permanently dead with no way to recover. The block detector
                # still decides on the evidence of this attempt.
                log.info("%s is tier 2 but no fetch vendor is configured; "
                         "retrying on tier 1", registrable_domain(url))

            result = await self._tier1(url, etag, last_modified)
            if result.not_modified or result.ok:
                return result
            # Escalate only for genuine blocking, and only if a vendor exists.
            if result.blocked and self.vendor_enabled:
                log.info("tier 1 blocked on %s (HTTP %s), escalating", url, result.status)
                escalated = await self._tier2(url)
                if escalated.ok:
                    return escalated
                return escalated if escalated.error else result
            return result

    async def get_text(self, url: str) -> Optional[str]:
        """Plain fetch for robots/sitemaps.

        These are usually unprotected, but "usually" is not "always" -- a WAF in
        front of the whole host walls the sitemap too, so the same block test
        applies. Transparently gunzips, since large catalogues commonly serve
        sitemap.xml.gz.
        """
        async with self._sem:
            if not self._charge(url):
                return None
            domain = registrable_domain(url)
            declared = await self.robots.crawl_delay(url)
            self._static_throttle.set_delay(domain, declared)
            await self._static_throttle.wait(domain)
            try:
                resp = await self.client.get(url, timeout=30)
            except Exception as exc:
                log.debug("get_text failed for %s: %s", url, exc)
                return None
            if resp.status_code != 200:
                return None

            body = resp.content
            # gzip magic, or a .gz URL. httpx decodes Content-Encoding but not a
            # genuinely gzipped file served as an octet-stream.
            if body[:2] == b"\x1f\x8b":
                try:
                    body = gzip.decompress(body)
                except OSError as exc:
                    log.warning("could not gunzip %s: %s", url, exc)
                    return None
                text = body.decode("utf-8", errors="replace")
            else:
                text = resp.text

            reason = looks_like_block_page(url, str(resp.url), text)
            if reason:
                log.info("block page detected fetching %s: %s", url, reason)
                return None
            return text

    async def screenshot(self, url: str) -> Optional[bytes]:
        """Evidence capture. Requires a vendor -- there is no browser on this box."""
        if not self.vendor_enabled:
            return None
        async with self._sem:
            res = await self._tier2(url, screenshot=True)
            return res.screenshot


def absolutize(base: str, href: str) -> str:
    return urljoin(base, href)
