"""Screenshot labelling and eBay auth-failure detection.

Both guard against the same class of bug: an artifact or a job result that
claims something the underlying data does not support.

  * _image_suffix -- Zyte returns JPEG for full-page captures (it rejects PNG
    for them outright), ScrapingBee returns PNG. The extension used to be
    hardcoded ".png", so half the evidence files contradicted their own bytes.
  * _is_auth_failure -- a bad keyset fails identically for all 246 products
    while re-minting a token against a 1,000/day cap. It must halt the sweep,
    not be retried per product.

No network, no credentials, no database.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from worker.evidence import _image_suffix
from worker.tasks import _is_auth_failure
from worker.ebay import EbayError

FAILS = []


def check(label, got, want):
    ok = got == want
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}: got {got!r}, want {want!r}")
    if not ok:
        FAILS.append(label)


print("1. Screenshot extension follows the bytes, not the vendor")
# Real headers. The JPEG one is what Zyte actually returned in production.
check("zyte jpeg (ff d8 ff e0 JFIF)",
      _image_suffix(bytes.fromhex("ffd8ffe000104a464946")), ".jpg")
check("jpeg without JFIF segment",
      _image_suffix(bytes.fromhex("ffd8ffdb00430008")), ".jpg")
check("scrapingbee png", _image_suffix(b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"), ".png")
check("gif87a", _image_suffix(b"GIF87a\x01\x00"), ".gif")
check("gif89a", _image_suffix(b"GIF89a\x01\x00"), ".gif")
check("webp", _image_suffix(b"RIFF\x24\x00\x00\x00WEBPVP8 "), ".webp")

print("\n2. Unrecognised bytes are stored honestly rather than mislabelled")
# The point is that it must NOT claim .png. A vendor returning an error page or
# a format we have not seen should not produce a file that lies about itself.
check("html error page is not called an image",
      _image_suffix(b"<!DOCTYPE html><html>"), ".bin")
check("empty response", _image_suffix(b""), ".bin")

print("\n3. Auth failures halt the sweep")
check("invalid_client (the live failure)",
      _is_auth_failure(EbayError(
          'token HTTP 401: {"error":"invalid_client",'
          '"error_description":"client authentication failed"}')), True)
check("invalid_grant", _is_auth_failure(EbayError('token HTTP 400: invalid_grant')), True)
check("invalid_scope", _is_auth_failure(EbayError('token HTTP 400: invalid_scope')), True)
check("unauthorized_client",
      _is_auth_failure(EbayError('token HTTP 400: unauthorized_client')), True)
check("token HTTP 403", _is_auth_failure(EbayError("token HTTP 403: forbidden")), True)

print("\n4. Transient / per-product failures do NOT halt the sweep")
# These can succeed on the next product, so halting would lose real coverage.
check("search 500", _is_auth_failure(EbayError("search HTTP 500: server error")), False)
check("timeout",
      _is_auth_failure(EbayError("search failed: ReadTimeout: timed out")), False)
check("rate limit is handled by its own branch",
      _is_auth_failure(EbayError("eBay rate limit reached (HTTP 429)")), False)
check("no access_token in body",
      _is_auth_failure(EbayError("token response contained no access_token")), False)
# A 401 on SEARCH is not a keyset problem -- ebay._search already refreshes the
# token and asks for a retry, so it must not be treated as fatal.
check("search 401 is not fatal (token refresh path)",
      _is_auth_failure(EbayError(
          "search returned 401; token refreshed, retry the sweep")), False)

print()
if FAILS:
    print(f"FAILED: {len(FAILS)} check(s): {', '.join(FAILS)}")
    sys.exit(1)
print("ALL EVIDENCE/AUTH CHECKS PASSED")
