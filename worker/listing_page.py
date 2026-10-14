"""Harvest product URLs from a listing page (brand, category, or search results).

Far better than enumerating a catalogue when the dealer exposes a page scoped to
our brand. BigDealer's /s/henry-holsters carries 186 of our products across
~4 paginated pages -- roughly 4 requests to find them, against 36,000 URLs of
sitemap enumeration that mostly is not ours.

Pagination follows rel="next", which is the standard signal and is what
BigDealer emits (`?_iv_page=2`). Guessing `?page=N` is a fallback only: on
that same site `?p=2` silently returns page 1 again, so a guess-and-compare loop
must detect the no-op rather than paginate forever.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Optional
from urllib.parse import urljoin, urlparse, urlunparse

from selectolax.parser import HTMLParser

from worker.discovery import host_of, looks_like_product_url

log = logging.getLogger(__name__)

MAX_PAGES = 25
MAX_URLS = 2_000

# Markers of a client-rendered page. If the HTML names our brand but carries no
# product links and no prices, the grid is almost certainly fetched by
# JavaScript after load -- AngularDealer is AngularJS and serves exactly that. Saying
# so beats reporting "this dealer carries nothing of yours", which is what it
# looks like otherwise.
_JS_APP_MARKERS = ("ng-app", "__NEXT_DATA__", "window.__INITIAL", "data-reactroot",
                   "data-vue", "id=\"root\"", "id=\"__nuxt\"", "ng-controller")
_PRICE_TEXT_RE = re.compile(r"[$£€]\s?\d{1,5}[.,]\d{2}")


def looks_client_rendered(html: str, product_links: int) -> bool:
    """No products and no prices in the markup, but a JS app shell present."""
    if product_links > 3:
        return False
    if _PRICE_TEXT_RE.search(html or ""):
        return False
    low = (html or "")[:200000].lower()
    return any(m.lower() in low for m in _JS_APP_MARKERS)


# Paths that are navigation or merchandising rather than a single product.
_NON_PRODUCT_HINTS = (
    "-brand.html", "-new.html", "-on-sale.html", "-final-sale.html",
    "-clearance.html", "-all.html", "/brands/", "/c/", "/s/",
)


@dataclass
class ListingProduct:
    """A product read directly off a listing page, price included."""
    url: str
    price: Optional[str] = None
    sku: Optional[str] = None
    title: Optional[str] = None
    brand: Optional[str] = None
    source: str = "data_attrs"


# Attribute names seen carrying a price on product cards. Ecommerce analytics
# markup is fairly standardised around these.
_PRICE_ATTRS = ("data-price", "data-product-price", "data-final-price",
                "data-sale-price", "data-item-price")
_SKU_ATTRS = ("data-sku", "data-product-sku", "data-product-id", "data-item-sku")
_URL_ATTRS = ("data-url", "data-product-url", "data-href", "href")
_NAME_ATTRS = ("data-name", "data-product-name", "data-title", "title")
_BRAND_ATTRS = ("data-brand", "data-product-brand", "data-manufacturer")


def _first_attr(node, names) -> Optional[str]:
    for n in names:
        v = node.attributes.get(n)
        if v and v.strip():
            return v.strip()
    return None


def extract_listing_products(html: str, base_url: str) -> list[ListingProduct]:
    """Read products AND prices straight off the listing page.

    Worth a lot more than it looks. BigDealer's brand page carries
    `data-sku`, `data-url` and `data-price` on every card, so four paginated
    fetches yield all 186 products with prices -- against ~190 individual page
    fetches, which is enough traffic to trip their bot protection. Cheaper,
    faster, and far politer.

    Only cards that expose a price are returned; anything else still needs its
    own page fetch.
    """
    tree = HTMLParser(html)
    out: list[ListingProduct] = []
    seen: set[str] = set()

    for attr in _PRICE_ATTRS:
        for node in tree.css(f"[{attr}]"):
            price = _first_attr(node, (attr,))
            if not price:
                continue
            # Identity may sit on the same element or on a nearby ancestor.
            url = _first_attr(node, _URL_ATTRS)
            sku = _first_attr(node, _SKU_ATTRS)
            name = _first_attr(node, _NAME_ATTRS)
            brand = _first_attr(node, _BRAND_ATTRS)
            hop = node.parent
            depth = 0
            while hop is not None and depth < 4 and not (url and sku):
                url = url or _first_attr(hop, _URL_ATTRS)
                sku = sku or _first_attr(hop, _SKU_ATTRS)
                name = name or _first_attr(hop, _NAME_ATTRS)
                brand = brand or _first_attr(hop, _BRAND_ATTRS)
                hop = hop.parent
                depth += 1
            if not url:
                continue
            absolute = _canonical(urljoin(base_url, url))
            if not _same_site(absolute, base_url) or absolute in seen:
                continue
            seen.add(absolute)
            out.append(ListingProduct(url=absolute, price=price, sku=sku,
                                      title=name, brand=brand))
    return out


@dataclass
class Harvest:
    urls: list[str] = field(default_factory=list)
    products: list[ListingProduct] = field(default_factory=list)
    pages_fetched: int = 0
    stopped_because: str = ""
    blocked: bool = False
    redirected_away: Optional[str] = None
    js_rendered: bool = False

    @property
    def ok(self) -> bool:
        return bool(self.urls)


def _same_site(a: str, b: str) -> bool:
    return host_of(a) == host_of(b)


def _canonical(url: str) -> str:
    """Drop the fragment and any trailing '?' so dedupe works."""
    p = urlparse(url)
    return urlunparse((p.scheme, p.netloc, p.path, p.params, p.query, ""))


def looks_like_listing_url(url: str) -> bool:
    """Does this URL look like a listing page rather than a site root?

    Used to decide whether a pasted URL should be crawled as a listing or sent
    down the sitemap path.
    """
    path = (urlparse(url).path or "/").rstrip("/")
    return bool(path) and path != ""


def scope_token(listing_url: str) -> Optional[str]:
    """A distinguishing token from the listing URL's own path.

    A brand page at /s/henry-holsters names what it contains. Sites with flat
    URL schemes -- BigDealer serves both /henry-holsters-cipher-...html and
    /bolt-carrier-group.html at the root -- give no other way to tell a product
    link from a navigation link, and harvesting a brand page yielded 850 links
    of which most were the site's own category menu.
    """
    path = (urlparse(listing_url).path or "").strip("/")
    if not path:
        return None

    generic = {"products", "collections", "category", "categories", "catalog",
               "search", "shop", "brands", "brand", "store", "page", "index"}

    # Consider EVERY segment, not just the last. AngularDealer's brand URL is
    # /henry-holsters/b -- the meaningful token is the first segment, and taking
    # the last one yielded "b", which is too short to use. The longest
    # non-generic segment is the discriminating one in practice.
    best: Optional[str] = None
    for seg in path.split("/"):
        seg = re.sub(r"\.(html?|php|aspx?)$", "", seg, flags=re.I)
        token = re.sub(r"[^a-z0-9]", "", seg.lower())
        if len(token) < 6 or token in generic or token.isdigit():
            continue
        if best is None or len(token) > len(best):
            best = token
    return best


def extract_product_links(html: str, base_url: str,
                          prefer_token: Optional[str] = None) -> list[str]:
    """Same-site links from a listing page that look like individual products.

    When `prefer_token` is given and a meaningful number of links carry it, only
    those are kept -- that is what separates the brand's products from the
    surrounding navigation on a flat-URL site.
    """
    tree = HTMLParser(html)
    out: list[str] = []
    seen: set[str] = set()
    for node in tree.css("a[href]"):
        href = (node.attributes.get("href") or "").strip()
        if not href or href.startswith(("#", "javascript:", "mailto:", "tel:")):
            continue
        absolute = _canonical(urljoin(base_url, href))
        if not _same_site(absolute, base_url):
            continue
        if not looks_like_product_url(absolute):
            continue
        low = absolute.lower()
        if any(h in low for h in _NON_PRODUCT_HINTS):
            continue
        if absolute in seen:
            continue
        seen.add(absolute)
        out.append(absolute)

    if prefer_token:
        scoped = [u for u in out
                  if prefer_token in re.sub(r"[^a-z0-9]", "", urlparse(u).path.lower())]
        # Only trust the narrowing if it actually found a body of links; a
        # handful could just be breadcrumbs back to the listing itself.
        if len(scoped) >= 3:
            log.info("listing scope %r narrowed %d links to %d",
                     prefer_token, len(out), len(scoped))
            return scoped
    return out


def find_next_page(html: str, base_url: str) -> Optional[str]:
    """rel="next", from either <link> or <a>. The standard signal, so preferred."""
    tree = HTMLParser(html)
    for selector in ('link[rel="next"]', 'a[rel="next"]'):
        node = tree.css_first(selector)
        if node is not None:
            href = (node.attributes.get("href") or "").strip()
            if href:
                nxt = _canonical(urljoin(base_url, href))
                if _same_site(nxt, base_url) and nxt != _canonical(base_url):
                    return nxt
    # Some templates put rel on a wrapper; fall back to a scan of anchor rels.
    for node in tree.css("a[href]"):
        rel = (node.attributes.get("rel") or "").lower()
        if "next" in rel.split():
            href = (node.attributes.get("href") or "").strip()
            if href:
                nxt = _canonical(urljoin(base_url, href))
                if _same_site(nxt, base_url) and nxt != _canonical(base_url):
                    return nxt
    return None


async def harvest(fetcher, start_url: str, seller_tier: int = 1,
                  max_pages: int = MAX_PAGES, max_urls: int = MAX_URLS,
                  on_page=None) -> Harvest:
    """Walk a listing page and its rel=next chain, collecting product URLs."""
    result = Harvest()
    seen_pages: set[str] = set()
    seen_products: set[str] = set()
    url: Optional[str] = _canonical(start_url)
    token = scope_token(start_url)
    if token:
        log.info("listing scope token from URL: %r", token)

    while url and result.pages_fetched < max_pages:
        if url in seen_pages:
            result.stopped_because = "pagination looped back to a page already seen"
            break
        seen_pages.add(url)

        res = await fetcher.get(url, seller_tier=seller_tier)
        result.pages_fetched += 1

        if res.blocked:
            result.blocked = True
            result.stopped_because = f"blocked: {res.error}"
            break
        if not res.ok:
            result.stopped_because = res.error or f"HTTP {res.status}"
            break

        # A silent redirect to somewhere unrelated is a common trap: AngularDealer
        # answers an unrecognised brand id by redirecting to its generic /brand
        # index, which looks exactly like "this dealer carries nothing of yours"
        # unless the redirect is noticed and reported.
        if result.pages_fetched == 1:
            asked = urlparse(url).path.rstrip("/").lower()
            landed = urlparse(res.final_url).path.rstrip("/").lower()
            if landed != asked and not landed.startswith(asked):
                result.redirected_away = res.final_url

        # Prices on the card save an entire fetch per product.
        for lp in extract_listing_products(res.html, res.final_url):
            if lp.url not in {p.url for p in result.products}:
                result.products.append(lp)

        found = extract_product_links(res.html, res.final_url, prefer_token=token)
        fresh = [u for u in found if u not in seen_products]
        for u in fresh:
            seen_products.add(u)
            result.urls.append(u)

        if result.pages_fetched == 1 and not result.products:
            # Judge on the brand-scoped links, not the navigation we filtered in.
            scoped = len([u for u in fresh if not token or
                          token in re.sub(r"[^a-z0-9]", "", urlparse(u).path.lower())])
            if looks_client_rendered(res.html, scoped):
                result.js_rendered = True

        if on_page is not None:
            await on_page(result.pages_fetched, len(result.urls), url)

        if len(result.urls) >= max_urls:
            result.stopped_because = f"hit max_urls={max_urls}"
            break

        # A page that adds nothing new means pagination is not advancing --
        # some sites ignore an unknown page parameter and re-serve page 1.
        if result.pages_fetched > 1 and not fresh:
            result.stopped_because = "page added no new products"
            break

        url = find_next_page(res.html, res.final_url)
        if not url:
            result.stopped_because = "no rel=next"

    if not result.stopped_because and result.pages_fetched >= max_pages:
        result.stopped_because = f"page limit ({max_pages}) reached"
    return result
