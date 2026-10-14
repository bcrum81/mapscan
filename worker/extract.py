"""Price extraction, cheapest tier first.

  1. platform JSON  -- Shopify/Woo `.json` endpoints. Free, exact, no parsing.
  2. JSON-LD        -- schema.org Product/Offer. Very high coverage, since most
                       platforms emit it for Google Shopping.
  3. microdata/OG    -- older sites.
  4. selector rules  -- per-domain CSS, stored in the DB as data.
  5. LLM             -- last resort on trimmed HTML, via OpenAI. Survives a
                       dealer redesign that breaks a CSS selector.

Tier 5 costs money, so tiers 1-4 exist to make it rare. Trimming before the LLM
call matters enormously: a raw product page is often 100k+ tokens, while the
region around a currency symbol is a few thousand.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Optional

from selectolax.parser import HTMLParser

from app import settings_store
from app.config import settings

log = logging.getLogger(__name__)

CURRENCY_CHARS = "$£€¥"
_PRICE_RE = re.compile(r"[-+]?\d{1,3}(?:[,\s]\d{3})*(?:\.\d{1,2})?|\d+\.\d{1,2}")

# Tags whose contents never contain the price and always contain noise.
# NOTE: <form> is deliberately NOT stripped. On many e-commerce templates the
# price sits inside the add-to-cart form, so removing it deletes the one value
# we came for. <nav>/<footer>/<header> are safe because they carry menu prices
# at most, and the currency-proximity filter would pick those up as noise.
_STRIP_TAGS = (
    "script", "style", "noscript", "svg", "iframe", "nav", "footer",
    "header", "template",
)


@dataclass
class Extracted:
    price: Optional[Decimal] = None
    currency: str = "USD"
    in_stock: Optional[bool] = None
    title: Optional[str] = None
    sku: Optional[str] = None
    upc: Optional[str] = None
    tier: str = "none"
    raw: dict[str, Any] | None = None

    @property
    def ok(self) -> bool:
        return self.price is not None and self.price > 0


def parse_price(value: Any) -> Optional[Decimal]:
    """Coerce a price out of anything a dealer site might hand us."""
    if value is None:
        return None
    if isinstance(value, (int, float, Decimal)):
        try:
            d = Decimal(str(value))
            return d if d > 0 else None
        except InvalidOperation:
            return None
    text = str(value).strip()
    if not text:
        return None
    # Drop currency symbols and codes before number matching.
    text = re.sub(r"(?i)\b(usd|cad|eur|gbp)\b", "", text)
    text = "".join(ch for ch in text if ch not in CURRENCY_CHARS)
    m = _PRICE_RE.search(text)
    if not m:
        return None
    cleaned = m.group(0).replace(",", "").replace(" ", "")
    try:
        d = Decimal(cleaned)
    except InvalidOperation:
        return None
    return d if d > 0 else None


def _availability_to_bool(value: Any) -> Optional[bool]:
    if value is None:
        return None
    text = str(value).lower()
    if "instock" in text.replace(" ", "") or "in_stock" in text or "available" in text:
        return True
    if any(k in text.replace(" ", "") for k in ("outofstock", "soldout", "discontinued", "backorder")):
        return False
    return None


# ---------------------------------------------------------------------------
# Tier 1 -- platform JSON endpoints
# ---------------------------------------------------------------------------
def shopify_json_url(url: str) -> Optional[str]:
    """Shopify exposes /products/<handle>.json with the price in cents."""
    m = re.match(r"^(https?://[^/]+)(/.*)?/products/([^/?#]+)", url)
    if not m:
        return None
    base, _mid, handle = m.groups()
    prefix = url[: url.index("/products/")]
    return f"{prefix}/products/{handle}.json"


def from_shopify_json(payload: dict) -> Extracted:
    product = payload.get("product") or payload
    variants = product.get("variants") or []
    price = None
    in_stock = None
    sku = None
    for v in variants:
        p = parse_price(v.get("price"))
        if p is not None and (price is None or p < price):
            price = p
            sku = v.get("sku") or sku
            av = v.get("available")
            in_stock = bool(av) if av is not None else in_stock
    return Extracted(
        price=price,
        in_stock=in_stock,
        title=product.get("title"),
        sku=sku,
        tier="platform_json",
        raw={"vendor": product.get("vendor"), "handle": product.get("handle")},
    )


# ---------------------------------------------------------------------------
# Tier 2/3 -- structured data (JSON-LD, microdata, RDFa, OpenGraph)
# ---------------------------------------------------------------------------
def _walk_for_product(node: Any):
    """Yield every dict in the tree that looks like a schema.org Product."""
    if isinstance(node, dict):
        types = node.get("@type") or node.get("type") or ""
        types = [types] if isinstance(types, str) else list(types)
        if any("product" in str(t).lower() for t in types):
            yield node
        for v in node.values():
            yield from _walk_for_product(v)
    elif isinstance(node, list):
        for item in node:
            yield from _walk_for_product(item)


def _offer_price(product: dict) -> tuple[Optional[Decimal], str, Optional[bool]]:
    offers = product.get("offers")
    candidates: list[dict] = []
    if isinstance(offers, dict):
        candidates = [offers]
    elif isinstance(offers, list):
        candidates = [o for o in offers if isinstance(o, dict)]

    best: Optional[Decimal] = None
    currency = "USD"
    in_stock: Optional[bool] = None
    for off in candidates:
        # AggregateOffer uses lowPrice; Offer uses price.
        for key in ("price", "lowPrice", "highPrice"):
            p = parse_price(off.get(key))
            if p is not None and (best is None or p < best):
                best = p
        currency = off.get("priceCurrency") or off.get("priceCurrency") or currency
        av = _availability_to_bool(off.get("availability"))
        if av is not None:
            in_stock = av
        spec = off.get("priceSpecification")
        if isinstance(spec, dict):
            p = parse_price(spec.get("price"))
            if p is not None and (best is None or p < best):
                best = p
    return best, (currency or "USD"), in_stock


def from_structured_data(html: str, url: str) -> Extracted:
    try:
        import extruct
    except ImportError:  # pragma: no cover
        return Extracted(tier="none")

    try:
        data = extruct.extract(
            html, base_url=url, syntaxes=["json-ld", "microdata", "rdfa", "opengraph"],
            uniform=True,
        )
    except Exception as exc:
        log.debug("extruct failed on %s: %s", url, exc)
        return Extracted(tier="none")

    for syntax, tier in (("json-ld", "jsonld"), ("microdata", "microdata"), ("rdfa", "rdfa")):
        for product in _walk_for_product(data.get(syntax) or []):
            price, currency, in_stock = _offer_price(product)
            if price is None:
                continue
            gtin = (
                product.get("gtin13") or product.get("gtin12")
                or product.get("gtin") or product.get("gtin14")
            )
            return Extracted(
                price=price, currency=currency, in_stock=in_stock,
                title=product.get("name"),
                sku=product.get("sku") or product.get("mpn"),
                upc=str(gtin) if gtin else None,
                tier=tier,
            )

    # OpenGraph product tags as a last structured attempt.
    for og in data.get("opengraph") or []:
        props = og if isinstance(og, dict) else {}
        price = parse_price(
            props.get("product:price:amount") or props.get("og:price:amount")
        )
        if price is not None:
            return Extracted(
                price=price,
                currency=props.get("product:price:currency") or "USD",
                title=props.get("og:title"),
                tier="og",
            )
    return Extracted(tier="none")


# ---------------------------------------------------------------------------
# Tier 4 -- per-domain CSS selectors from the DB
# ---------------------------------------------------------------------------
def from_selector(html: str, price_selector: str,
                  stock_selector: Optional[str] = None,
                  currency: str = "USD") -> Extracted:
    tree = HTMLParser(html)
    node = tree.css_first(price_selector)
    if node is None:
        return Extracted(tier="none")
    # Prefer a content/value attribute if present -- it is usually unformatted.
    raw = node.attributes.get("content") or node.attributes.get("value") or node.text()
    price = parse_price(raw)
    in_stock = None
    if stock_selector:
        s = tree.css_first(stock_selector)
        if s is not None:
            in_stock = _availability_to_bool(s.text())
    return Extracted(price=price, currency=currency, in_stock=in_stock, tier="selector")


# ---------------------------------------------------------------------------
# Tier 5 -- LLM on trimmed HTML
# ---------------------------------------------------------------------------
def trim_html_for_llm(html: str, max_chars: int) -> str:
    """Reduce a product page to the regions plausibly containing the price.

    This is the difference between a ~3k-token call and a ~150k-token one, so it
    is the single most important cost control in the whole pipeline.
    """
    tree = HTMLParser(html)
    for tag in _STRIP_TAGS:
        for node in tree.css(tag):
            node.decompose()

    body = tree.body or tree.root
    if body is None:
        return html[:max_chars]

    text = body.text(separator="\n", strip=True) or ""
    lines = [ln.strip() for ln in text.split("\n") if ln.strip()]

    # Keep lines with a currency symbol or price-ish number, plus context.
    keep_idx: set[int] = set()
    for i, line in enumerate(lines):
        if any(c in line for c in CURRENCY_CHARS) or re.search(r"\d+\.\d{2}\b", line):
            keep_idx.update(range(max(0, i - 2), min(len(lines), i + 3)))

    # Always keep the head of the document -- title, breadcrumbs, SKU block.
    keep_idx.update(range(0, min(len(lines), 40)))

    selected = [lines[i] for i in sorted(keep_idx)]
    out = "\n".join(selected)
    if len(out) > max_chars:
        out = out[:max_chars]
    return out


_LLM_SYSTEM = """You extract product pricing from the text of a single retail product page.

Return the price a customer would pay right now for the product the page is about:
- If both a list price and a sale price are shown, return the SALE price.
- Ignore prices for accessories, bundles, related products, and shipping.
- If the page shows a price range, return the lowest price for the base product.
- If no price is present, or the page is not a product page, set price to null.

Report only what the page states. Do not infer or estimate a price."""

_LLM_SCHEMA = {
    "type": "object",
    "properties": {
        "price": {"type": ["number", "null"]},
        "currency": {"type": "string"},
        "in_stock": {"type": ["boolean", "null"]},
        "title": {"type": ["string", "null"]},
        "sku": {"type": ["string", "null"]},
        "upc": {"type": ["string", "null"]},
    },
    # OpenAI strict mode requires every property to be listed in `required`
    # and `additionalProperties: false`; nullable fields are expressed as a
    # ["type", "null"] union rather than by omission.
    "required": ["price", "currency", "in_stock", "title", "sku", "upc"],
    "additionalProperties": False,
}


DEFAULT_EXTRACTION_MODEL = "gpt-4o-mini"


async def _llm_config() -> tuple[str, str, int]:
    """(api key, model, max input chars), database first then environment.

    Read through settings_store, NOT app.config. Every credential in this app is
    entered through the admin Settings page and lives encrypted in the database;
    the `.env` is empty for all of them. Reading os.environ here meant a key
    pasted into Settings was stored, displayed as active on the dashboard, and
    then silently ignored -- extraction just quietly stopped at tier 4.
    """
    key = await settings_store.get("openai_api_key", "") or ""
    model = await settings_store.get("extraction_model", "") or DEFAULT_EXTRACTION_MODEL
    max_chars = await settings_store.get_int(
        "llm_max_input_chars", settings.llm_max_input_chars)
    return key.strip(), model.strip(), max_chars


async def llm_enabled() -> bool:
    if not settings.llm_extraction_enabled:
        return False
    key, _, _ = await _llm_config()
    return bool(key)


async def from_llm(html: str, url: str) -> Extracted:
    """Tier 5: ask OpenAI for the price, constrained to a strict JSON schema."""
    api_key, model, max_chars = await _llm_config()
    if not (settings.llm_extraction_enabled and api_key):
        return Extracted(tier="none")

    trimmed = trim_html_for_llm(html, max_chars)
    if not trimmed.strip():
        return Extracted(tier="none")

    try:
        from openai import AsyncOpenAI
    except ImportError:  # pragma: no cover
        log.warning("openai SDK not installed; LLM tier disabled")
        return Extracted(tier="none")

    client = AsyncOpenAI(api_key=api_key, timeout=45.0, max_retries=2)
    try:
        resp = await client.chat.completions.create(
            model=model,
            max_completion_tokens=512,
            messages=[
                {"role": "system", "content": _LLM_SYSTEM},
                {"role": "user",
                 "content": f"URL: {url}\n\nPage text:\n{trimmed}"},
            ],
            # strict json_schema, so the reply cannot come back as prose or as
            # JSON wrapped in a code fence -- both of which we would have to
            # parse defensively and would sometimes get wrong.
            response_format={
                "type": "json_schema",
                "json_schema": {
                    "name": "product_price",
                    "strict": True,
                    "schema": _LLM_SCHEMA,
                },
            },
        )
    except Exception as exc:
        log.warning("LLM extraction failed for %s: %s", url, exc)
        return Extracted(tier="none")
    finally:
        await client.close()

    choice = (resp.choices or [None])[0]
    if choice is None:
        return Extracted(tier="none")
    # A refused or length-truncated reply yields unusable/partial JSON, so bail
    # rather than parse half an object into a price.
    if choice.finish_reason not in (None, "stop"):
        log.warning("LLM extraction for %s ended with finish_reason=%s",
                    url, choice.finish_reason)
        return Extracted(tier="none")
    if getattr(choice.message, "refusal", None):
        log.warning("LLM extraction refused for %s", url)
        return Extracted(tier="none")

    text = choice.message.content
    if not text:
        return Extracted(tier="none")
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        log.warning("LLM returned non-JSON for %s", url)
        return Extracted(tier="none")

    return Extracted(
        price=parse_price(data.get("price")),
        currency=data.get("currency") or "USD",
        in_stock=data.get("in_stock"),
        title=data.get("title"),
        sku=data.get("sku"),
        upc=data.get("upc"),
        tier="llm",
        raw={"model": model},
    )


async def verify_credentials() -> tuple[bool, str]:
    """Prove the key works, for the Settings test button."""
    api_key, model, _ = await _llm_config()
    if not api_key:
        return False, "No OpenAI API key is set."
    try:
        from openai import AsyncOpenAI
    except ImportError:
        return False, "The openai package is not installed on the server."

    client = AsyncOpenAI(api_key=api_key, timeout=30.0, max_retries=1)
    sample = ("Onyx Compact IWB Holster\nRegular price $97.20\n"
              "Sale price $86.98\nIn stock\nSKU AB-0004-02")
    try:
        resp = await client.chat.completions.create(
            model=model,
            max_completion_tokens=256,
            messages=[{"role": "system", "content": _LLM_SYSTEM},
                      {"role": "user", "content": f"URL: https://example.com/p\n\nPage text:\n{sample}"}],
            response_format={"type": "json_schema",
                             "json_schema": {"name": "product_price", "strict": True,
                                             "schema": _LLM_SCHEMA}},
        )
    except Exception as exc:
        return False, f"{type(exc).__name__}: {str(exc)[:200]}"
    finally:
        await client.close()

    try:
        data = json.loads(resp.choices[0].message.content or "{}")
    except json.JSONDecodeError:
        return False, f"{model} replied, but not with valid JSON."
    got = parse_price(data.get("price"))
    # The fixture has a list price and a sale price; the prompt says take the
    # sale price. Checking the value proves the prompt is being followed, not
    # merely that the key authenticates.
    if got is not None and abs(float(got) - 86.98) < 0.01:
        return True, f"{model} works — test page extracted correctly as $86.98."
    return True, (f"{model} works (key valid), but the test page returned "
                  f"{got!r} instead of 86.98 — check the model choice.")


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------
async def extract_price(
    html: str,
    url: str,
    fetcher=None,
    rule: Optional[dict] = None,
) -> Extracted:
    """Run the tiers in order, stopping at the first that yields a price."""
    # Tier 1: Shopify JSON (needs a fetch, so only when we have a fetcher).
    if fetcher is not None:
        sj = shopify_json_url(url)
        if sj:
            body = await fetcher.get_text(sj)
            if body:
                try:
                    result = from_shopify_json(json.loads(body))
                    if result.ok:
                        return result
                except (json.JSONDecodeError, AttributeError, TypeError):
                    pass

    # Tiers 2/3: structured data.
    result = from_structured_data(html, url)
    if result.ok:
        return result

    # Tier 4: per-domain selector rule.
    if rule and rule.get("price_selector"):
        result = from_selector(
            html, rule["price_selector"], rule.get("stock_selector"),
            rule.get("currency") or "USD",
        )
        if result.ok:
            return result

    # Tier 5: LLM.
    return await from_llm(html, url)
