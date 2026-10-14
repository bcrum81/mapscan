"""Candidate verification: is this URL actually our product?

Weighted confidence rather than hard rules, because dealers mangle SKUs, omit
UPCs, and rewrite titles. UPC is decisive when present; everything else is
corroborating. Scores land in one of three bands:

    >= MATCH_AUTO_CONFIRM  -> confirmed, enters the nightly price check
    <= MATCH_AUTO_REJECT   -> rejected, never fetched again
    in between             -> human review queue in the dashboard

The middle band is deliberate. A false positive in a MAP program means accusing
a dealer of something they did not do, so a human gate is worth the friction.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional

from rapidfuzz import fuzz

# Weights sum to 100 when every signal is available. Missing signals are
# excluded from the denominator rather than scored zero -- a dealer who omits
# the UPC should not be penalised for it.
WEIGHTS = {
    "upc": 55.0,
    "sku": 20.0,
    "title": 15.0,
    "fitment": 5.0,
    "description": 5.0,
}


def build_sku_index(products: dict) -> dict[str, int]:
    """Normalised SKU -> product id, for finding our SKU inside free text.

    Dealers very often append the manufacturer part number to the product title
    and the URL slug -- "...IWB Holster for Sig Sauer P365 AB-0003-11" -- while
    exposing their OWN stock code in the structured sku field. Comparing only
    the structured field misses this completely, which is how eight genuine
    listings were scored 38 and rejected.
    """
    index: dict[str, int] = {}
    for pid, prod in products.items():
        norm = normalize_sku(prod.get("sku") if isinstance(prod, dict) else prod)
        # Short codes would collide by chance inside a long slug.
        if norm and len(norm) >= 6:
            index[norm] = pid
    return index


def find_sku_in_text(text: Optional[str], sku_index: dict[str, int]) -> Optional[int]:
    """Return the product id whose SKU appears verbatim in `text`.

    Comparison is on the separator-stripped form, so "ab-0003-11", "AB_0003_11"
    and "AB000311" are all the same code.
    """
    if not text or not sku_index:
        return None
    flat = re.sub(r"[^A-Za-z0-9]", "", text).upper()
    if not flat:
        return None
    # Longest first: X_ABC-0009-01 must win over any shorter code it contains.
    for norm in sorted(sku_index, key=len, reverse=True):
        if norm in flat:
            return sku_index[norm]
    return None

# Tokens that designate a model or gun fit: they contain a digit and are short.
# These are decisive in this catalog. "IWB Holster for Sig P320 Compact" and
# "IWB Holster for Glock 43x" score 86% similar by token_set_ratio despite being
# entirely different products -- no general-purpose fuzzy scorer separates them,
# because they differ in exactly the two tokens that matter and agree on
# everything else. So fitment is checked explicitly rather than left to the
# title ratio.
# Match any alphanumeric run that may contain internal separators, then keep the
# ones carrying a digit. The previous pattern used a lookahead that could not
# cross a hyphen, so "TLR-7" yielded NOTHING -- leaving TLR-1 / TLR-7 / TLR-8
# indistinguishable across 37 SKUs in the real catalogue.
_FITMENT_RE = re.compile(r"[a-z0-9]+(?:[./-][a-z0-9]+)*", re.I)

# Words that look like fitment tokens but are not.
_FITMENT_NOISE = {
    "1", "2", "3", "4", "5", "10", "100", "2nd", "3rd",
    "9mm", "45acp", "40sw", "380", "10mm",     # calibers: too coarse to fit on
    "1x", "2x", "3x", "4pk", "2pk",
}


def fitment_tokens(text: Optional[str]) -> set[str]:
    """Extract model/fit designators: p320, 43x, tlr-7, x300u, mod4s, 1911.

    Separators are stripped for comparison so a dealer writing "TLR7" still
    matches our "TLR-7" -- both catalogue spellings occur in real data. Note this
    does NOT conflate genuinely different models: X300 and X300U stay distinct.
    """
    if not text:
        return set()
    out: set[str] = set()
    for raw in _FITMENT_RE.findall(text):
        t = raw.lower().strip("./-")
        if not any(c.isdigit() for c in t):
            continue                      # a fitment token always carries a digit
        flat = t.replace("-", "").replace(".", "").replace("/", "")
        if len(flat) < 2 or flat in _FITMENT_NOISE:
            continue
        out.add(flat)
    return out


@dataclass
class MatchResult:
    score: float
    signals: dict = field(default_factory=dict)
    decision: str = "candidate"   # confirmed | rejected | candidate

    @property
    def is_confirmed(self) -> bool:
        return self.decision == "confirmed"


def normalize_upc(raw: Optional[str]) -> Optional[str]:
    """Normalize UPC-A / EAN-13 / GTIN-14 to a 13-digit GTIN for comparison."""
    if not raw:
        return None
    digits = re.sub(r"\D", "", str(raw))
    if not digits:
        return None
    if len(digits) == 12:            # UPC-A -> GTIN-13
        digits = "0" + digits
    elif len(digits) == 14:          # GTIN-14 -> drop packaging indicator
        digits = digits[1:]
    elif len(digits) == 8:           # EAN-8 kept as-is
        return digits
    if len(digits) != 13:
        return None
    return digits


def upc_check_digit_valid(gtin13: str) -> bool:
    """GS1 mod-10 for a 13-digit GTIN.

    Weighting for GTIN-13 runs 1,3,1,3,... from the LEFT: 0-indexed even
    positions get weight 1, odd positions get weight 3.

    This was previously inverted, which rejected ~80% of a real catalogue of
    valid UPC-A codes. It survived testing because the fixture used,
    0012345678905, validates identically under both weightings -- so the cases
    below deliberately include values where the two disagree.
    """
    if len(gtin13) != 13 or not gtin13.isdigit():
        return False
    body, check = gtin13[:12], int(gtin13[12])
    total = sum(int(d) * (1 if i % 2 == 0 else 3) for i, d in enumerate(body))
    return (10 - total % 10) % 10 == check


def normalize_sku(raw: Optional[str]) -> Optional[str]:
    if not raw:
        return None
    s = re.sub(r"[^A-Za-z0-9]", "", str(raw)).upper()
    return s or None


def _title_score(ours: str, theirs: Optional[str]) -> Optional[float]:
    if not theirs:
        return None
    # token_set_ratio tolerates the dealer prefixing their own brand name and
    # appending "- Free Shipping", which is the common case.
    return float(fuzz.token_set_ratio(ours.lower(), theirs.lower()))


def _description_score(ours: Optional[str], theirs: Optional[str]) -> Optional[float]:
    if not ours or not theirs:
        return None
    a, b = ours.lower()[:1500], theirs.lower()[:1500]
    return float(fuzz.token_set_ratio(a, b))


def score_match(
    product: dict,
    seen_title: Optional[str],
    seen_sku: Optional[str] = None,
    seen_upc: Optional[str] = None,
    seen_description: Optional[str] = None,
    seen_url: Optional[str] = None,
    auto_confirm: float = 85.0,
    auto_reject: float = 45.0,
) -> MatchResult:
    signals: dict = {}
    weighted_sum = 0.0
    weight_total = 0.0

    # -- OUR sku appearing verbatim in their title or URL ---------------------
    # Treated with the same authority as a UPC match: a structured part number
    # reproduced exactly is not something that happens by coincidence.
    our_sku_norm = normalize_sku(product.get("sku"))
    if our_sku_norm and len(our_sku_norm) >= 6:
        haystack = " ".join(filter(None, [seen_title, seen_url, seen_sku]))
        flat = re.sub(r"[^A-Za-z0-9]", "", haystack).upper()
        if our_sku_norm in flat:
            where = []
            for label, val in (("title", seen_title), ("url", seen_url)):
                if val and our_sku_norm in re.sub(r"[^A-Za-z0-9]", "", val).upper():
                    where.append(label)
            signals["sku_in_text"] = {"sku": our_sku_norm, "found_in": where}
            # A conflicting UPC still overrules -- that would mean the page is
            # for a different product regardless of what the text says.
            their_upc_norm = normalize_upc(seen_upc)
            our_upc_norm = normalize_upc(product.get("upc"))
            if our_upc_norm and their_upc_norm and our_upc_norm != their_upc_norm:
                signals["upc"] = {"ours": our_upc_norm, "theirs": their_upc_norm,
                                  "match": False}
                signals["veto"] = "our SKU is present but the UPC contradicts it"
                return MatchResult(0.0, signals, "rejected")
            signals["identifier_confirmed"] = True
            return MatchResult(100.0, signals, "confirmed")

    # -- UPC: decisive when both sides have one -----------------------------
    our_upc = normalize_upc(product.get("upc"))
    their_upc = normalize_upc(seen_upc)
    if our_upc and their_upc:
        hit = our_upc == their_upc
        signals["upc"] = {"ours": our_upc, "theirs": their_upc, "match": hit}
        weighted_sum += WEIGHTS["upc"] * (1.0 if hit else 0.0)
        weight_total += WEIGHTS["upc"]
        # An explicit UPC mismatch is a hard reject: it is a different product,
        # regardless of how similar the title looks.
        if not hit:
            return MatchResult(0.0, signals, "rejected")

    # -- SKU / MPN ----------------------------------------------------------
    our_sku = normalize_sku(product.get("sku"))
    their_sku = normalize_sku(seen_sku)
    if our_sku and their_sku:
        # Dealers often prefix or suffix their own stock code.
        hit = our_sku == their_sku or our_sku in their_sku or their_sku in our_sku
        signals["sku"] = {"ours": our_sku, "theirs": their_sku, "match": hit}
        weighted_sum += WEIGHTS["sku"] * (1.0 if hit else 0.0)
        weight_total += WEIGHTS["sku"]

    # -- Fitment / model tokens ---------------------------------------------
    # Checked before the title so a disjoint fit can veto a high title ratio.
    our_fit = fitment_tokens(product.get("title"))
    their_fit = fitment_tokens(seen_title) | fitment_tokens(seen_sku)
    fitment_conflict = False
    if our_fit and their_fit:
        overlap = our_fit & their_fit
        fitment_conflict = not overlap
        signals["fitment"] = {
            "ours": sorted(our_fit), "theirs": sorted(their_fit),
            "overlap": sorted(overlap), "conflict": fitment_conflict,
        }
        weighted_sum += WEIGHTS["fitment"] * (1.0 if overlap else 0.0)
        weight_total += WEIGHTS["fitment"]

    # -- Title --------------------------------------------------------------
    ts = _title_score(product.get("title", ""), seen_title)
    if ts is not None:
        signals["title"] = {"theirs": seen_title, "ratio": round(ts, 1)}
        weighted_sum += WEIGHTS["title"] * (ts / 100.0)
        weight_total += WEIGHTS["title"]

    # -- Description --------------------------------------------------------
    ds = _description_score(product.get("description"), seen_description)
    if ds is not None:
        signals["description"] = {"ratio": round(ds, 1)}
        weighted_sum += WEIGHTS["description"] * (ds / 100.0)
        weight_total += WEIGHTS["description"]

    if weight_total == 0.0:
        return MatchResult(0.0, {"note": "no comparable signals"}, "candidate")

    score = round(weighted_sum / weight_total * 100.0, 2)

    # A strong title match alone is not enough to auto-confirm -- it is exactly
    # how you end up tracking a competitor's similar-looking holster.
    identifier_confirmed = bool(
        signals.get("upc", {}).get("match") or signals.get("sku", {}).get("match")
    )

    # A disjoint fitment set means a different gun, so it vetoes both
    # auto-confirm and any benefit of the doubt -- UNLESS a UPC or SKU matched,
    # in which case the identifier outranks a noisy dealer title.
    if fitment_conflict and not identifier_confirmed:
        signals["veto"] = "fitment tokens disjoint (different model or gun fit)"
        return MatchResult(min(score, auto_reject), signals, "rejected")

    if score >= auto_confirm and identifier_confirmed:
        decision = "confirmed"
    elif score <= auto_reject:
        decision = "rejected"
    else:
        decision = "candidate"

    signals["identifier_confirmed"] = identifier_confirmed
    return MatchResult(score, signals, decision)


# Words that look like product-line names but are shared with the wider
# category, so they cannot identify our brand on a multi-brand dealer's site.
_LINE_STOPWORDS = {
    # generic descriptors
    "duty", "shield", "compact", "full", "partial", "left", "right", "small",
    "large", "medium", "holster", "holsters", "kydex", "iwb", "owb", "appendix",
    "light", "bearing", "standard", "classic", "basic", "pro", "plus", "mini",
    "spare", "hardware", "belt", "clip", "mount", "carrier",
    # firearm and accessory BRANDS -- these appear in competitors' slugs too, so
    # treating one as our model line would re-open the false-positive hole.
    "glock", "sig", "sauer", "springfield", "walther", "canik", "beretta",
    "ruger", "taurus", "kimber", "staccato", "smith", "wesson", "mossberg",
    "colt", "fnh", "heckler", "koch", "streamlight", "surefire", "olight",
    "holosun", "trijicon", "safariland", "blackhawk",
}


def derive_line_tokens(product_titles: dict[int, str]) -> set[str]:
    """Distinctive product-line names taken from the catalogue itself.

    Our titles are "Nova Glock X300U, ...", "Vertex, Sig P365, ..." -- the
    leading word is the model line. Those are brand-specific (Nova, Vertex,
    Onyx, Comet, Zenith, Talon) and are the strongest slug-level evidence that
    a URL is ours, short of the brand name itself.
    """
    out: set[str] = set()
    for title in product_titles.values():
        first = re.split(r"[\s,]+", (title or "").strip())[:1]
        if not first:
            continue
        tok = re.sub(r"[^a-z0-9]", "", first[0].lower())
        # Purely alphabetic: a leading token carrying digits is a model or
        # fitment designator (2011, G940, X300), not a product line, and those
        # are shared with the wider category.
        if len(tok) >= 4 and tok.isalpha() and tok not in _LINE_STOPWORDS:
            out.add(tok)
    return out


def slug_prefilter(product_titles: dict[int, str], url: str,
                   threshold: float = 55.0,
                   brand_tokens: Optional[set[str]] = None,
                   line_tokens: Optional[set[str]] = None,
                   require_brand: bool = True,
                   sku_index: Optional[dict[str, int]] = None
                   ) -> list[tuple[int, float]]:
    """Cheap pre-filter over a sitemap URL before spending a fetch on it.

    Fuzzy title similarity ALONE is far too loose on a multi-brand dealer. Our
    titles share their most prominent tokens -- gun platform and light model --
    with every competitor in the category, so "Sig P365" matched a cleaning
    mat, a Streamlight, and four rival holsters. On one real dealer that
    nominated 1,318 of 35,939 URLs, none of which were ours.

    So a slug must first carry a BRAND signal: our brand name, or one of our
    distinctive model-line names. Only then is the fuzzy ratio consulted. On a
    dealer who genuinely carries nothing of ours this yields zero fetches
    instead of hundreds.
    """
    slug = url.rstrip("/").rsplit("/", 1)[-1]
    slug = re.sub(r"\.(html?|php|aspx?)$", "", slug, flags=re.I)
    flat = re.sub(r"[^a-z0-9]", "", slug.lower())
    slug_words = re.sub(r"[-_+]+", " ", slug).lower()
    if not slug_words.strip():
        return []

    # Exact SKU in the slug settles it. Dealers routinely append the
    # manufacturer part number, and reading it beats guessing by fuzzy title --
    # which previously paired ".../...-ab-0003-11" with product AB-0003-07 and
    # ".../...-ab-0018-16" with AB-0023-04, both simply the wrong product.
    if sku_index:
        pid = find_sku_in_text(url, sku_index)
        if pid is not None:
            return [(pid, 100.0)]

    if require_brand:
        brand = brand_tokens or set()
        lines = line_tokens or set()
        # Brand match is checked against the separator-stripped slug so
        # "henry-holsters" and "henryholsters" behave identically.
        has_brand = any(b in flat for b in brand)
        has_line = any(re.search(rf"\b{re.escape(t)}\b", slug_words) for t in lines)
        if not (has_brand or has_line):
            return []

    hits: list[tuple[int, float]] = []
    for pid, title in product_titles.items():
        ratio = fuzz.token_set_ratio(slug_words, title.lower())
        if ratio >= threshold:
            hits.append((pid, float(ratio)))
    hits.sort(key=lambda t: t[1], reverse=True)
    return hits
