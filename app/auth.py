"""Authentication: password hashing, sessions, invites, rate limiting.

Design notes worth knowing before changing anything here:

* **bcrypt, not argon2.** Argon2 is the stronger default in general, but it is
  deliberately memory-hard -- 64 MiB per hash at library defaults. This API unit
  runs under `MemoryMax=256M` on a swapless 1.9 GB box, so a couple of
  concurrent logins could push it into the OOM killer. bcrypt at cost 12 uses a
  few KB and ~250 ms of CPU, which is the right trade here.

* **Passwords are SHA-256 pre-hashed before bcrypt** (the `bcrypt_sha256`
  scheme Django uses). bcrypt hard-caps input at 72 *bytes* -- and bcrypt 4.x
  raises `ValueError` rather than truncating, so a long passphrase would 500 the
  request. Since the UI actively recommends passphrases, that is a live path,
  not a theoretical one. Pre-hashing to a fixed 44-byte digest removes the cap
  entirely and also sidesteps bcrypt's historical null-byte truncation.
  Changing this scheme invalidates every stored hash.

* **Hashing is CPU-bound and runs in a thread.** A 250 ms blocking call on the
  event loop would stall every other request on this single-vCPU box.

* **Tokens are stored hashed.** The raw invite/reset token is shown or emailed
  exactly once. A database dump therefore does not hand over live accounts.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import logging
import re
import secrets
from datetime import datetime, timedelta, timezone
from typing import Optional

import bcrypt
from fastapi import HTTPException, Request
from fastapi.responses import RedirectResponse

from app import db
from app.config import settings

log = logging.getLogger(__name__)

BCRYPT_ROUNDS = 12
TOKEN_TTL_HOURS = 72
MIN_PASSWORD_LENGTH = 12

# Rate limits on the login form. Internet-facing, so both are enforced.
MAX_FAILS_PER_EMAIL = 8
MAX_FAILS_PER_IP = 25
FAIL_WINDOW_MINUTES = 15

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

# Rejected outright regardless of length. Not a substitute for a real breach
# corpus, just a guard against the handful people actually try first.
_OBVIOUS_WEAK = {
    "password", "passw0rd", "password123", "letmein", "changeme",
    "mapscan", "qwertyuiop", "1234567890",
    "administrator", "welcome1234",
}


# ---------------------------------------------------------------------------
# passwords
# ---------------------------------------------------------------------------
def _prehash(password: str) -> bytes:
    """SHA-256 then base64: a fixed 44 bytes, always inside bcrypt's 72-byte cap.

    Without this, bcrypt 4.x raises ValueError on any password over 72 bytes --
    which UTF-8 reaches at well under 72 *characters* for accented or non-Latin
    text.
    """
    return base64.b64encode(hashlib.sha256(password.encode("utf-8")).digest())


def _hash_sync(password: str) -> str:
    return bcrypt.hashpw(_prehash(password), bcrypt.gensalt(BCRYPT_ROUNDS)).decode()


def _verify_sync(password: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(_prehash(password), hashed.encode("utf-8"))
    except (ValueError, TypeError):
        return False


async def hash_password(password: str) -> str:
    return await asyncio.to_thread(_hash_sync, password)


async def verify_password(password: str, hashed: Optional[str]) -> bool:
    if not hashed:
        return False
    return await asyncio.to_thread(_verify_sync, password, hashed)


def validate_email(email: str) -> str:
    e = (email or "").strip()
    if not _EMAIL_RE.match(e):
        raise HTTPException(400, "That does not look like a valid email address.")
    if len(e) > 254:
        raise HTTPException(400, "Email address is too long.")
    return e


def validate_password(password: str, email: str = "") -> None:
    """Length-first policy, no composition rules (per current NIST guidance)."""
    p = password or ""
    if len(p) < MIN_PASSWORD_LENGTH:
        raise HTTPException(
            400, f"Password must be at least {MIN_PASSWORD_LENGTH} characters."
        )
    if len(p) > 200:
        raise HTTPException(400, "Password must be under 200 characters.")
    squashed = re.sub(r"[^a-z0-9]", "", p.lower())
    if squashed in _OBVIOUS_WEAK or any(w in squashed for w in ("password", "mapscan")):
        raise HTTPException(400, "That password is too easily guessed. Pick another.")
    local = (email or "").split("@")[0].lower()
    if local and len(local) >= 4 and local in p.lower():
        raise HTTPException(400, "Password must not contain your email address.")


def generate_password(words: int = 4) -> str:
    """Readable-but-strong initial password, e.g. 'Tf7x-Qm2p-Rk9v-Zd4h'."""
    alphabet = "abcdefghjkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    return "-".join(
        "".join(secrets.choice(alphabet) for _ in range(4)) for _ in range(words)
    )


# ---------------------------------------------------------------------------
# tokens
# ---------------------------------------------------------------------------
def _token_hash(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


async def issue_token(user_id: int, purpose: str,
                      created_by: Optional[int] = None) -> str:
    """Create a single-use token. Returns the RAW token -- store it nowhere."""
    raw = secrets.token_urlsafe(32)
    expires = datetime.now(timezone.utc) + timedelta(hours=TOKEN_TTL_HOURS)
    # Supersede any outstanding token of the same purpose, so a re-invite
    # invalidates the previous link rather than leaving two live.
    await db.execute(
        "UPDATE auth_tokens SET used_at = now() "
        "WHERE user_id = $1 AND purpose = $2 AND used_at IS NULL",
        user_id, purpose,
    )
    await db.execute(
        "INSERT INTO auth_tokens (user_id, token_hash, purpose, expires_at, created_by) "
        "VALUES ($1,$2,$3,$4,$5)",
        user_id, _token_hash(raw), purpose, expires, created_by,
    )
    return raw


async def consume_token(raw: str, purpose: str) -> Optional[dict]:
    """Validate and burn a token atomically. Returns the user row, or None."""
    row = await db.fetchrow(
        """
        UPDATE auth_tokens
           SET used_at = now()
         WHERE token_hash = $1
           AND purpose = $2
           AND used_at IS NULL
           AND expires_at > now()
        RETURNING user_id
        """,
        _token_hash(raw), purpose,
    )
    if row is None:
        return None
    user = await db.fetchrow(
        "SELECT id, email, email_lower, is_admin, is_active FROM users WHERE id = $1",
        row["user_id"],
    )
    if user is None or not user["is_active"]:
        return None
    return dict(user)


def invite_url(raw_token: str) -> str:
    return f"{settings.public_base_url.rstrip('/')}/invite/{raw_token}"


# ---------------------------------------------------------------------------
# rate limiting
# ---------------------------------------------------------------------------
async def login_blocked(email_lower: str, ip: Optional[str]) -> Optional[str]:
    since = datetime.now(timezone.utc) - timedelta(minutes=FAIL_WINDOW_MINUTES)
    by_email = await db.fetchval(
        "SELECT count(*) FROM auth_events "
        "WHERE event='login_fail' AND email_lower=$1 AND created_at > $2",
        email_lower, since,
    ) or 0
    if by_email >= MAX_FAILS_PER_EMAIL:
        return (f"Too many failed attempts for this account. "
                f"Try again in {FAIL_WINDOW_MINUTES} minutes.")
    if ip:
        by_ip = await db.fetchval(
            "SELECT count(*) FROM auth_events "
            "WHERE event='login_fail' AND ip=$1::inet AND created_at > $2",
            ip, since,
        ) or 0
        if by_ip >= MAX_FAILS_PER_IP:
            return (f"Too many failed attempts from this address. "
                    f"Try again in {FAIL_WINDOW_MINUTES} minutes.")
    return None


async def record_event(event: str, email_lower: Optional[str] = None,
                       user_id: Optional[int] = None, ip: Optional[str] = None,
                       detail: Optional[str] = None) -> None:
    await db.execute(
        "INSERT INTO auth_events (email_lower, user_id, event, ip, detail) "
        "VALUES ($1,$2,$3,$4::inet,$5)",
        email_lower, user_id, event, ip, detail,
    )


def client_ip(request: Request) -> Optional[str]:
    """Trust X-Forwarded-For only because nginx on this box sets it."""
    xff = request.headers.get("x-forwarded-for")
    if xff:
        return xff.split(",")[0].strip() or None
    return request.client.host if request.client else None


# ---------------------------------------------------------------------------
# session
# ---------------------------------------------------------------------------
SESSION_USER_KEY = "uid"
SESSION_CSRF_KEY = "csrf"


async def load_user(request: Request) -> Optional[dict]:
    uid = request.session.get(SESSION_USER_KEY)
    if not uid:
        return None
    row = await db.fetchrow(
        "SELECT id, email, is_admin, is_active, must_change_password "
        "FROM users WHERE id = $1",
        uid,
    )
    if row is None or not row["is_active"]:
        request.session.clear()
        return None
    return dict(row)


def start_session(request: Request, user_id: int) -> None:
    request.session.clear()
    request.session[SESSION_USER_KEY] = user_id
    request.session[SESSION_CSRF_KEY] = secrets.token_urlsafe(24)


def csrf_token(request: Request) -> str:
    tok = request.session.get(SESSION_CSRF_KEY)
    if not tok:
        tok = secrets.token_urlsafe(24)
        request.session[SESSION_CSRF_KEY] = tok
    return tok


def check_csrf(request: Request, submitted: Optional[str]) -> None:
    expected = request.session.get(SESSION_CSRF_KEY)
    if not expected or not submitted or not secrets.compare_digest(submitted, expected):
        raise HTTPException(400, "Session expired or invalid form token. Reload and retry.")


class LoginRedirect(Exception):
    """Raised when the request has no valid session: bounce to /login."""

    def __init__(self, next_url: str = "/") -> None:
        self.next_url = next_url


class PasswordChangeRequired(Exception):
    """Raised when the session IS valid but the password must be rotated first.

    Deliberately distinct from LoginRedirect. Routing this through /login would
    loop forever: /login sees the valid session, redirects to /, which raises
    this again. The user is authenticated -- they just cannot go anywhere else
    until they set their own password.
    """


async def require_user(request: Request) -> dict:
    user = await load_user(request)
    if user is None:
        raise LoginRedirect(request.url.path)
    # Force rotation before anything else is reachable.
    if user["must_change_password"] and request.url.path not in (
        "/change-password", "/logout"
    ):
        raise PasswordChangeRequired()
    return user


async def require_admin(request: Request) -> dict:
    user = await require_user(request)
    if not user["is_admin"]:
        raise HTTPException(403, "Administrator access required.")
    return user


def redirect_to_login(next_url: str = "/") -> RedirectResponse:
    if next_url and next_url not in ("/", "/login"):
        return RedirectResponse(f"/login?next={next_url}", status_code=303)
    return RedirectResponse("/login", status_code=303)
