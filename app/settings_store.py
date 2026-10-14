"""Runtime settings held in the database, secrets encrypted at rest.

Why this exists: an API key typed into the admin UI must never reach a file in
the repo, a `.env` on disk, or a chat transcript. Values live in `app_settings`,
secrets Fernet-encrypted under `SETTINGS_KEY`.

Resolution order for any key: **database wins, then environment, then default.**
That way `.env` can stay empty for every credential while still allowing an
env-var override for local work.

Cross-process invalidation: the API writes and the worker reads, in separate
processes. `settings_version` is bumped on every write and both sides re-check
it on a short interval, so a key entered in the browser reaches the worker
without a restart.
"""
from __future__ import annotations

import base64
import hashlib
import logging
import os
import time
from typing import Optional

from cryptography.fernet import Fernet, InvalidToken

from app import db
from app.config import settings as env_settings

log = logging.getLogger(__name__)

CACHE_TTL_SECONDS = 30

# key -> (env var fallback, is_secret)
KNOWN: dict[str, tuple[str, bool]] = {
    "brave_search_key":   ("BRAVE_SEARCH_KEY", True),
    "brave_monthly_cap":  ("BRAVE_MONTHLY_BUDGET", False),
    "discovery_queries_per_run": ("DISCOVERY_QUERIES_PER_RUN", False),
    "brand_tokens":       ("BRAND_TOKENS", False),
    # eBay Browse API (unauthorised-channel monitoring)
    "ebay_app_id":        ("EBAY_APP_ID", True),
    "ebay_cert_id":       ("EBAY_CERT_ID", True),
    "ebay_sandbox":       ("EBAY_SANDBOX", False),
    "ebay_products_per_run": ("EBAY_PRODUCTS_PER_RUN", False),
    # Marketplace Account Deletion endpoint. eBay will not activate a
    # production keyset until its challenge is answered with a digest over
    # this token, so it is a prerequisite for eBay working at all.
    "ebay_verification_token": ("EBAY_VERIFICATION_TOKEN", True),
    # Only needed if the URL registered with eBay differs from
    # PUBLIC_BASE_URL + the endpoint path; the digest is an exact string match.
    "ebay_deletion_endpoint": ("EBAY_DELETION_ENDPOINT", False),
    "openai_api_key":     ("OPENAI_API_KEY", True),
    "extraction_model":   ("EXTRACTION_MODEL", False),
    "llm_max_input_chars": ("LLM_MAX_INPUT_CHARS", False),
    "fetch_vendor":       ("FETCH_VENDOR", False),
    "scrapingbee_key":    ("SCRAPINGBEE_KEY", True),
    "zyte_key":           ("ZYTE_KEY", True),
    # email
    "email_provider":     ("EMAIL_PROVIDER", False),
    "email_from":         ("EMAIL_FROM", False),
    "resend_key":         ("RESEND_KEY", True),
    "gmail_sa_email":     ("GMAIL_SA_EMAIL", False),
    "gmail_sa_key":       ("GMAIL_SA_KEY", True),      # PEM private key
    "gmail_send_as":      ("GMAIL_SEND_AS", False),
    "smtp_host":          ("SMTP_HOST", False),
    "smtp_port":          ("SMTP_PORT", False),
    "smtp_user":          ("SMTP_USER", False),
    "smtp_password":      ("SMTP_PASSWORD", True),
}

_cache: dict[str, Optional[str]] = {}
_cache_at: float = 0.0
_cache_version: int = -1


# ---------------------------------------------------------------------------
# encryption
# ---------------------------------------------------------------------------
def _fernet() -> Optional[Fernet]:
    raw = os.getenv("SETTINGS_KEY", "").strip()
    if not raw:
        return None
    # Accept either a real Fernet key or any passphrase, which is stretched to
    # one. Stretching keeps the deploy step simple without weakening a
    # properly-generated key.
    try:
        return Fernet(raw.encode())
    except (ValueError, TypeError):
        derived = base64.urlsafe_b64encode(hashlib.sha256(raw.encode()).digest())
        return Fernet(derived)


def encryption_available() -> bool:
    return _fernet() is not None


def _encrypt(value: str) -> bytes:
    f = _fernet()
    if f is None:
        raise RuntimeError(
            "SETTINGS_KEY is not set, so secrets cannot be stored. Generate one:\n"
            '  python3 -c "from cryptography.fernet import Fernet; '
            'print(Fernet.generate_key().decode())"'
        )
    return f.encrypt(value.encode("utf-8"))


def _decrypt(blob: bytes) -> Optional[str]:
    f = _fernet()
    if f is None or blob is None:
        return None
    try:
        return f.decrypt(bytes(blob)).decode("utf-8")
    except InvalidToken:
        # Almost always means SETTINGS_KEY was rotated or replaced.
        log.error("could not decrypt a stored setting -- SETTINGS_KEY may have changed")
        return None


# ---------------------------------------------------------------------------
# read
# ---------------------------------------------------------------------------
async def _db_version() -> int:
    try:
        return await db.fetchval("SELECT version FROM settings_version WHERE id = 1") or 0
    except Exception:
        return 0


async def refresh(force: bool = False) -> None:
    global _cache, _cache_at, _cache_version
    now = time.monotonic()
    if not force and (now - _cache_at) < CACHE_TTL_SECONDS:
        return
    version = await _db_version()
    if not force and version == _cache_version and _cache:
        _cache_at = now
        return
    rows = await db.fetch("SELECT key, value_enc FROM app_settings")
    _cache = {r["key"]: _decrypt(r["value_enc"]) for r in rows}
    _cache_at = now
    _cache_version = version


async def get(key: str, default: Optional[str] = None) -> Optional[str]:
    """Database value, else env var, else default."""
    await refresh()
    val = _cache.get(key)
    if val not in (None, ""):
        return val
    env_var = KNOWN.get(key, (None, True))[0]
    if env_var:
        env_val = os.getenv(env_var, "")
        if env_val != "":
            return env_val
    return default


async def get_int(key: str, default: int) -> int:
    raw = await get(key)
    try:
        return int(raw) if raw not in (None, "") else default
    except (TypeError, ValueError):
        return default


async def is_set(key: str) -> bool:
    return bool(await get(key))


async def status() -> dict[str, dict]:
    """For the admin UI: which keys are configured, and from where.

    Never returns a secret's value -- only whether one exists and its source.
    """
    await refresh(force=True)
    out: dict[str, dict] = {}
    for key, (env_var, secret) in KNOWN.items():
        db_val = _cache.get(key)
        env_val = os.getenv(env_var or "", "")
        if db_val not in (None, ""):
            source, present = "database", True
        elif env_val != "":
            source, present = "environment", True
        else:
            source, present = None, False
        out[key] = {
            "present": present,
            "source": source,
            "is_secret": secret,
            "value": None if secret else (db_val or env_val or None),
        }
    return out


# ---------------------------------------------------------------------------
# write
# ---------------------------------------------------------------------------
async def set_value(key: str, value: Optional[str], updated_by: Optional[int] = None) -> None:
    """Store (or clear, when value is empty) a setting and bump the version."""
    if key not in KNOWN:
        raise ValueError(f"unknown setting {key!r}")
    if value is None or value.strip() == "":
        await db.execute("DELETE FROM app_settings WHERE key = $1", key)
    else:
        blob = _encrypt(value.strip())
        await db.execute(
            """
            INSERT INTO app_settings (key, value_enc, is_secret, updated_by, updated_at)
            VALUES ($1,$2,$3,$4, now())
            ON CONFLICT (key) DO UPDATE
              SET value_enc = EXCLUDED.value_enc,
                  updated_by = EXCLUDED.updated_by,
                  updated_at = now()
            """,
            key, blob, KNOWN[key][1], updated_by,
        )
    await db.execute("UPDATE settings_version SET version = version + 1 WHERE id = 1")
    await refresh(force=True)
