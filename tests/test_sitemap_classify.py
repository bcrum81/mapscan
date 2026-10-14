#!/usr/bin/env python3
"""Sitemap classification, against the REAL BigDealer taxonomy."""
from __future__ import annotations
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from worker.discovery import classify_sitemap, prioritise_sitemaps  # noqa: E402

fails = []
def chk(name, got, want):
    ok = got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {name}: {got!r}" + ("" if ok else f" want {want!r}"))
    if not ok: fails.append(name)

B = "https://www.bigdealer.example/"
REAL = [B + n for n in [
    "sitemap.product.entity.orderable.firearms.multivariant.index.xml",
    "sitemap.product.entity.orderable.generic.multivariant.index.xml",
    "sitemap.product.entity.unorderable.firearms.multivariant.index.xml",
    "sitemap.product.entity.unorderable.generic.multivariant.index.xml",
    "sitemap.brand.product-list.flag.new.index.xml",
    "sitemap.category.product-list.flag.index.xml",
    "sitemap.family_category.product-list.flag.index.xml",
    "sitemap.department.product-list.flag.index.xml",
    "sitemap.review.list.product.index.xml",
    "sitemap.qna.list.product.index.xml",
    "sitemap.dynamic_landing.index.xml",
    "sitemap.info.flag.demo.index.xml",
]]

print("=== classification of real BigDealer sitemap names (2=expand 1=neutral 0=skip) ===")
for u in REAL:
    print(f"  {classify_sitemap(u)}  {u.rsplit('/', 1)[-1]}")

kept, st = prioritise_sitemaps(REAL)
print(f"\n  stats: {st}")
print(f"  kept:  {[u.rsplit('/', 1)[-1] for u in kept]}")
chk("only the 2 orderable-product sitemaps kept", len(kept), 2)
chk("no unorderable survives", any("unorderable" in u for u in kept), False)
chk("no listing sitemap survives", any("product-list" in u for u in kept), False)
chk("no review/qna survives", any(("review" in u or "qna" in u) for u in kept), False)
chk("skipped count", st["skipped"], 10)

print("\n=== small shops must not be filtered into nothing ===")
k, _ = prioritise_sitemaps(["https://dealer.example/sitemap.xml"])
chk("generic /sitemap.xml kept", len(k), 1)
k, _ = prioritise_sitemaps(["https://d.example/sitemap_products_1.xml",
                            "https://d.example/sitemap_pages_1.xml"])
chk("shopify products sitemap ranked first", "sitemap_products_1.xml" in k[0], True)
k, st2 = prioritise_sitemaps(["https://d.example/sitemap.review.xml",
                              "https://d.example/blog-sitemap.xml"])
chk("all-skip falls back to all rather than none", len(k), 2)
chk("fallback is explained", bool(st2.get("note")), True)
k, _ = prioritise_sitemaps(["https://d.example/wp-sitemap-posts-product-1.xml",
                            "https://d.example/wp-sitemap-posts-post-1.xml"])
chk("woocommerce: post sitemap skipped", len(k), 1)

print()
if fails:
    print(f"FAILED: {fails}")
    raise SystemExit(1)
print("ALL SITEMAP CLASSIFICATION CHECKS PASSED")
