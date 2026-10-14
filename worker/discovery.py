"""Discovery: finding candidate URLs.

Three sources, none of which fetch product pages -- discovery produces URLs,
the fetcher decides what they cost.

  * Brave Search -- the web-search provider. Independent index, own crawler.
  * Sitemaps     -- for paste-a-URL domain scans. Plain XML, never bot-blocked,
                    and unaffected by any search-API change. Highest yield.

Google's Custom Search JSON API was deliberately NOT used: it is closed to new
customers and is discontinued on 2027-01-01, so it is not something to build a
dependency on. Providers go through `search()` and share the SearchHit shape, so
adding a second provider later is a change in one function.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import date
from typing import Optional
from urllib.parse import urlparse

from app import db, settings_store
from app.config import settings

log = logging.getLogger(__name__)


# Marketplaces we deliberately do not scrape. Amazon's ToS prohibits it and
# enforcement is real -- use SP-API or Brand Registry violation reporting.
EXCLUDED_HOSTS = {
    "amazon.com", "amazon.ca", "amzn.to",
    "facebook.com", "instagram.com", "pinterest.com", "youtube.com",
    "reddit.com", "x.com", "twitter.com", "tiktok.com",
    "google.com", "bing.com",
}
# Never flag ourselves: OWN_DOMAINS is the brand's own storefront(s).
EXCLUDED_HOSTS |= set(settings.own_domains)

_SITEMAP_CANDIDATES = (
    "/sitemap.xml",
    "/sitemap_index.xml",
    "/sitemap_products_1.xml",      # Shopify
    "/wp-sitemap.xml",              # WordPress 5.5+
    "/product-sitemap.xml",         # Yoast/WooCommerce
)

_LOC_RE = re.compile(r"<loc>\s*([^<\s]+)\s*</loc>", re.I)
_SITEMAPINDEX_RE = re.compile(r"<sitemapindex", re.I)

# --------------------------------------------------------------------------
# Sitemap classification.
#
# Large retailers publish a taxonomy of sitemaps, most of which are useless to
# us. BigDealer declares 194: ninety are category/brand/department LISTING
# pages, nine are products they cannot even sell ("unorderable"), and only seven
# hold actual purchasable product pages. Enumerating blindly spent 40,000 URLs
# on unsellable inventory before hitting the cap.
#
# Names are matched, not contents, so this costs nothing.
# --------------------------------------------------------------------------
# Strongly wanted: individual, purchasable product pages.
# NB: checked BEFORE _SITEMAP_SKIP, so "posts-product" wins over "posts-post".
_SITEMAP_PREFER = ("product.entity.orderable", "product-sitemap", "sitemap_products",
                   "products.xml", "product.xml", "posts-product")
# Never useful: listings, reviews, Q&A, editorial, and unsellable stock.
_SITEMAP_SKIP = (
    "product-list", "unorderable", "review", "qna", "dynamic_landing",
    "info.flag", "family_category", "brand_category", "department.",
    "landing", "blog", "article",
    # Yoast / RankMath
    "page-sitemap", "post-sitemap", "category-sitemap", "author", "tag-sitemap",
    # WordPress 5.5+ core sitemaps
    "posts-post", "posts-page", "taxonomies", "users-",
)


def classify_sitemap(url: str) -> int:
    """Rank a sitemap by how likely it holds individual product pages.

    2 = preferred, 1 = neutral, 0 = skip.
    """
    name = url.rsplit("/", 1)[-1].lower()
    if any(k in name for k in _SITEMAP_PREFER):
        return 2
    if any(k in name for k in _SITEMAP_SKIP):
        return 0
    return 1


def prioritise_sitemaps(urls: list[str]) -> tuple[list[str], dict]:
    """Keep only plausible product sitemaps, best first.

    If nothing scores above zero we return the original list: a small shop with a
    single generic /sitemap.xml must still work, and over-filtering it into
    nothing would be worse than a little noise.
    """
    ranked = [(classify_sitemap(u), u) for u in urls]
    preferred = [u for r, u in ranked if r == 2]
    neutral = [u for r, u in ranked if r == 1]
    skipped = [u for r, u in ranked if r == 0]
    kept = preferred + neutral
    stats = {"declared": len(urls), "preferred": len(preferred),
             "neutral": len(neutral), "skipped": len(skipped)}
    if not kept:
        return urls, {**stats, "note": "no sitemap looked product-shaped; using all"}
    return kept, stats


MAX_SITEMAP_CHILDREN = 200
# How many robots-declared sitemaps to probe before concluding the host is
# unreachable. Deliberately small -- see discover_sitemaps().
MAX_SITEMAP_PROBES = 3


@dataclass
class SearchHit:
    url: str
    title: Optional[str] = None
    snippet: Optional[str] = None
    source: str = "brave"


def host_of(url: str) -> str:
    h = (urlparse(url).hostname or "").lower()
    return h[4:] if h.startswith("www.") else h


def is_excluded(url: str) -> bool:
    h = host_of(url)
    return any(h == bad or h.endswith("." + bad) for bad in EXCLUDED_HOSTS)


# ---------------------------------------------------------------------------
# Budget enforcement
# ---------------------------------------------------------------------------
def _period_for(provider: str) -> date:
    """Brave bills monthly, so the budget period is the first of the month."""
    today = date.today()
    return date(today.year, today.month, 1)


async def _budget_remaining(provider: str) -> int:
    period = _period_for(provider)
    cap = await settings_store.get_int("brave_monthly_cap", 1000)
    used = await db.fetchval(
        "SELECT used FROM search_budget WHERE provider = $1 AND period = $2",
        provider, period,
    )
    return max(0, cap - (used or 0))


async def _budget_consume(provider: str, n: int = 1) -> None:
    period = _period_for(provider)
    await db.execute(
        """
        INSERT INTO search_budget (provider, period, used)
        VALUES ($1, $2, $3)
        ON CONFLICT (provider, period) DO UPDATE SET used = search_budget.used + $3
        """,
        provider, period, n,
    )


# ---------------------------------------------------------------------------
# Brave Search
# ---------------------------------------------------------------------------
async def monthly_used(provider: str = "brave") -> int:
    """Queries charged this calendar month."""
    return await db.fetchval(
        "SELECT used FROM search_budget WHERE provider = $1 AND period = $2",
        provider, _period_for(provider),
    ) or 0


async def brave_search(fetcher, query: str, count: int = 10) -> list[SearchHit]:
    key = await settings_store.get("brave_search_key")
    if not key:
        return []
    if await _budget_remaining("brave") <= 0:
        log.info("brave monthly budget exhausted; skipping %r", query)
        return []
    try:
        resp = await fetcher.client.get(
            "https://api.search.brave.com/res/v1/web/search",
            params={"q": query, "count": min(count, 20)},
            headers={
                "Accept": "application/json",
                "X-Subscription-Token": key,
            },
            timeout=30,
        )
    except Exception as exc:
        log.warning("brave request failed: %s", exc)
        return []

    await _budget_consume("brave", 1)

    if resp.status_code != 200:
        log.warning("brave HTTP %s", resp.status_code)
        return []

    results = ((resp.json() or {}).get("web") or {}).get("results") or []
    hits = []
    for r in results:
        link = r.get("url")
        if not link or is_excluded(link):
            continue
        hits.append(SearchHit(link, r.get("title"), r.get("description"), "brave"))
    return hits


async def search(fetcher, query: str, num: int = 10) -> tuple[list[SearchHit], int]:
    """Run one discovery query.

    Returns (hits, queries_consumed) so the caller charges its budget for what
    was actually spent rather than assuming one query per call -- relevant
    because a request that returns zero results still costs a query.
    """
    if not await settings_store.get("brave_search_key"):
        log.warning("no Brave API key configured (set it in Settings); web "
                    "discovery is disabled. Sitemap scans still work.")
        return [], 0
    if await _budget_remaining("brave") <= 0:
        log.info("brave monthly cap reached; skipping %r", query)
        return [], 0
    hits = await brave_search(fetcher, query, count=num)
    return hits, 1


def build_queries(product: dict) -> list[tuple[str, str]]:
    """(query_kind, query) pairs for one product, cheapest signal first."""
    queries: list[tuple[str, str]] = []
    upc = (product.get("upc") or "").strip()
    if upc:
        # A bare UPC search is the single highest-precision query available.
        queries.append(("upc", f'"{upc}"'))
    title = (product.get("title") or "").strip()
    if title:
        queries.append(("title", f'"{title}"'))
    return queries


# ---------------------------------------------------------------------------
# Sitemap discovery (paste-a-URL domain scan)
# ---------------------------------------------------------------------------
async def discover_sitemaps(fetcher, domain: str) -> list[str]:
    """Locate sitemap URLs that ACTUALLY PARSE.

    robots.txt declaring a sitemap is not evidence the sitemap is reachable. A
    WAF in front of the host walls the sitemap while robots.txt stays public, so
    a declared-but-unfetchable sitemap previously got counted as found -- which
    surfaced as the misleading "sitemaps: 1, sitemap_urls: 0" pairing. Every
    candidate is now verified before being returned.
    """
    declared = list(dict.fromkeys(await fetcher.robots.sitemaps(domain)))

    # Verification must SHORT-CIRCUIT. www.bigdealer.example declares 194
    # sitemaps; probing every one at the per-domain politeness delay would spend
    # ~10 minutes before the scan produced a single URL. Probe a few: reachability
    # is a property of the host, so if the first usable one parses, the rest are
    # taken on trust and any individual failure is handled during expansion.
    probes = 0
    for sm in declared:
        if probes >= MAX_SITEMAP_PROBES:
            break
        probes += 1
        body = await fetcher.get_text(sm)
        if body and "<loc" in body.lower():
            log.info("%s: %d sitemap(s) declared, verified via %s",
                     domain, len(declared), sm)
            # Put the one we proved first so expansion starts on solid ground.
            return [sm] + [x for x in declared if x != sm]
        log.info("sitemap declared but not usable: %s", sm)

    # Nothing declared worked (or nothing was declared) -- try the usual paths.
    for path in _SITEMAP_CANDIDATES:
        url = f"https://{domain}{path}"
        body = await fetcher.get_text(url)
        if body and "<loc" in body.lower():
            return [url]
    return []


async def resolve_scan_host(fetcher, pasted: str) -> tuple[str, list[str], str]:
    """Find the host on this site that actually serves a usable sitemap.

    A pasted URL is frequently not the host with the catalogue on it --
    `shop.bigdealer.example` is WAF-walled while `www.bigdealer.example` serves 194
    sitemaps. Rather than reporting "no sitemap found" and leaving the user to
    guess, try the apex and www variants before giving up.

    Returns (host_used, verified_sitemaps, note).

    `host_used` is the host that answered, which may be the www variant. Callers
    must normalise it with host_of() before storing a seller, or the same dealer
    ends up recorded twice -- once as example.com and once as www.example.com,
    with its listings split between them.
    """
    host = host_of(pasted if "//" in pasted else f"https://{pasted}")
    if not host:
        return "", [], f"could not parse a hostname from {pasted!r}"

    candidates = [host]
    # Derive apex + www for a subdomain like shop.example.com -> example.com/www.
    parts = host.split(".")
    if len(parts) > 2:
        apex = ".".join(parts[-2:])
        for alt in (f"www.{apex}", apex):
            if alt not in candidates:
                candidates.append(alt)
    elif len(parts) == 2:
        alt = f"www.{host}"
        if alt not in candidates:
            candidates.append(alt)

    tried: list[str] = []
    for cand in candidates:
        sitemaps = await discover_sitemaps(fetcher, cand)
        if sitemaps:
            note = ("" if cand == host
                    else f"{host} served no usable sitemap; scanned {cand} instead")
            return cand, sitemaps, note
        tried.append(cand)

    return host, [], ("no usable sitemap on any of: " + ", ".join(tried))


async def expand_sitemap(fetcher, sitemap_url: str, depth: int = 0,
                         max_urls: int = 50_000, on_progress=None) -> list[str]:
    """Recursively expand a sitemap (or sitemap index) into page URLs.

    Large retailers publish sitemap indexes with hundreds of children -- 194 for
    bigdealer.example -- so the child limit is a real ceiling, not a formality.
    When it truncates, the caller says so rather than implying full coverage.
    """
    if depth > 3:
        return []
    body = await fetcher.get_text(sitemap_url)     # gunzips transparently
    if not body:
        return []

    locs = _LOC_RE.findall(body)
    if not locs:
        return []

    # A <sitemapindex> means these locs are more sitemaps, not pages.
    if _SITEMAPINDEX_RE.search(body):
        urls: list[str] = []
        children = locs[:MAX_SITEMAP_CHILDREN]
        if len(locs) > MAX_SITEMAP_CHILDREN:
            log.info("sitemap index %s has %d children; expanding first %d",
                     sitemap_url, len(locs), MAX_SITEMAP_CHILDREN)
        for i, child in enumerate(children, 1):
            urls.extend(await expand_sitemap(fetcher, child, depth + 1, max_urls))
            if on_progress is not None and (i % 10 == 0 or i == len(children)):
                await on_progress(i, len(children), len(urls))
            if len(urls) >= max_urls:
                log.info("hit max_urls=%d expanding %s", max_urls, sitemap_url)
                break
        return urls[:max_urls]

    return locs[:max_urls]


def looks_like_product_url(url: str) -> bool:
    """Drop obvious non-product paths before slug matching."""
    path = urlparse(url).path.lower()
    if any(seg in path for seg in (
        "/blog/", "/news/", "/pages/", "/policies/", "/account", "/cart",
        "/checkout", "/search", "/tag/", "/author/", "/category/", "/collections/all",
        "/faq", "/about", "/contact", "/terms", "/privacy", "/shipping",
    )):
        return False
    if path.endswith((".pdf", ".jpg", ".jpeg", ".png", ".gif", ".webp", ".xml", ".css", ".js")):
        return False
    return True
