"""PEM normalisation for the Gmail service-account key.

Google's key JSON stores the private key as a JSON string, so `private_key`
reads with literal backslash-n rather than newlines. Pasting it verbatim -- the
obvious thing to do -- used to fail inside OpenSSL with "Could not deserialize
key data", and the UI then blamed domain-wide delegation, sending the user to
the Workspace admin console over a formatting problem.

No network, no credentials: the key here is generated in-process.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from app.mail import normalise_pem

FAILS = []


def check(label, got, want):
    ok = got == want
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}: got {got!r}, want {want!r}")
    if not ok:
        FAILS.append(label)


key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
REAL = key.private_bytes(
    encoding=serialization.Encoding.PEM,
    format=serialization.PrivateFormat.PKCS8,
    encryption_algorithm=serialization.NoEncryption(),
).decode()


def loads(text) -> bool:
    try:
        serialization.load_pem_private_key(
            normalise_pem(text).encode(), password=None)
        return True
    except Exception:
        return False


print("1. Every way the key gets pasted must load")
check("already-correct PEM", loads(REAL), True)
check("JSON form with literal backslash-n", loads(REAL.replace("\n", "\\n")), True)
check("JSON form still wrapped in quotes",
      loads('"' + REAL.replace("\n", "\\n") + '"'), True)
check("CRLF line endings", loads(REAL.replace("\n", "\r\n")), True)
check("literal backslash-r-backslash-n",
      loads(REAL.replace("\n", "\\r\\n")), True)
check("leading/trailing whitespace", loads("\n\n  " + REAL + "  \n\n"), True)

print("\n2. Garbage is still rejected -- normalising must not paper over a bad key")
check("empty", loads(""), False)
check("not a key at all", loads("hunter2"), False)
check("the client_email by mistake",
      loads("mapscan@proj.iam.gserviceaccount.com"), False)
check("truncated body", loads(REAL[:120]), False)

print("\n3. Output shape")
check("empty in, empty out", normalise_pem(""), "")
check("ends with exactly one newline",
      normalise_pem(REAL).endswith("-----END PRIVATE KEY-----\n"), True)
check("no literal backslash-n survives",
      "\\n" in normalise_pem(REAL.replace("\n", "\\n")), False)

print()
if FAILS:
    print(f"FAILED: {len(FAILS)} check(s): {', '.join(FAILS)}")
    sys.exit(1)
print("ALL GMAIL-PEM CHECKS PASSED")
