"""eBay Marketplace Account Deletion challenge + notice handling.

The challenge digest is the gate on the whole production keyset, and it fails
silently: a wrong digest looks identical to a right one from here, and eBay
just refuses to enable the app. So the exact concatenation order and the exact
endpoint string are pinned by test.

No network, no credentials, no database.
"""
import hashlib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.ebay_notify import PATH, challenge_response, token_problem, _extract

FAILS = []


def check(label, got, want):
    ok = got == want
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}: got {got!r}, want {want!r}")
    if not ok:
        FAILS.append(label)


print("1. Digest is sha256(challenge + token + endpoint), in that order")
code, token = "abc123", "x" * 40
endpoint = "https://mapscan.example.com" + PATH
expected = hashlib.sha256((code + token + endpoint).encode()).hexdigest()
check("matches the documented construction",
      challenge_response(code, token, endpoint), expected)
check("is lowercase hex, 64 chars",
      len(challenge_response(code, token, endpoint)) == 64
      and challenge_response(code, token, endpoint).islower(), True)

print("\n2. Order matters -- a transposition must NOT collide")
check("token+challenge differs from challenge+token",
      challenge_response(code, token, endpoint)
      == challenge_response(token, code, endpoint), False)

print("\n3. The endpoint string is part of the digest")
# This is the usual cause of a rejected challenge: a trailing slash or http://
# in the portal changes the answer while looking identical to a human.
base = challenge_response(code, token, endpoint)
check("trailing slash changes it",
      challenge_response(code, token, endpoint + "/") == base, False)
check("http vs https changes it",
      challenge_response(code, token, endpoint.replace("https://", "http://")) == base,
      False)

print("\n4. Verification token validation mirrors eBay's rules")
check("40 chars alnum is fine", token_problem("a" * 40), None)
check("underscore and hyphen allowed", token_problem("a" * 30 + "_-" + "b" * 8), None)
check("32 chars is the floor", token_problem("a" * 32), None)
check("80 chars is the ceiling", token_problem("a" * 80), None)
check("empty rejected", token_problem("") is None, False)
check("31 chars rejected", token_problem("a" * 31) is None, False)
check("81 chars rejected", token_problem("a" * 81) is None, False)
check("punctuation rejected", token_problem("a" * 39 + "!") is None, False)

print("\n5. Payload parsing survives a malformed body")
# This runs on a public endpoint; a KeyError here becomes a 500, which eBay
# reads as a dead endpoint and eventually disables the keyset over.
full = {"notification": {"notificationId": "n-1",
                         "data": {"username": "seller_bob", "userId": "u-9"}}}
check("extracts all three", _extract(full), ("n-1", "seller_bob", "u-9"))
check("empty dict", _extract({}), (None, None, None))
check("notification present but null", _extract({"notification": None}),
      (None, None, None))
check("data present but null",
      _extract({"notification": {"notificationId": "n-2", "data": None}}),
      ("n-2", None, None))
check("unexpected shape", _extract({"notification": {"data": {}}}),
      (None, None, None))

print()
if FAILS:
    print(f"FAILED: {len(FAILS)} check(s): {', '.join(FAILS)}")
    sys.exit(1)
print("ALL EBAY-NOTIFY CHECKS PASSED")
