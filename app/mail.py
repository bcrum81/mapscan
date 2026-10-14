"""Outbound email for invites and resets.

Deliberately pluggable, with a default that needs no provider. The dashboard was
specified as no-push-alerts, so there is no mail account wired up -- but invites
need delivery. Rather than block user creation on procuring a provider, the
default transport is `manual`: the invite link is returned to the admin in the
UI to send however they like. Configure EMAIL_PROVIDER later and the same code
path emails it instead.

Transports: manual (default) | gmail_api | resend | smtp

**SMTP is often unusable on cloud hosts.** Many VPS providers (DigitalOcean
among them) block outbound 25/465/587 at the network level. The `smtp`
transport is retained for hosts where those ports are open. Everything else here talks HTTPS on 443.

`gmail_api` sends through the account's own Google Workspace mailbox over HTTPS,
using a service account with domain-wide delegation. That is the only way to
send as a Workspace address from here: Workspace disabled app passwords for SMTP
on 2025-05-01 (OAuth mandatory), and the SMTP ports are blocked regardless.
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import smtplib
import time
from dataclasses import dataclass
from email.message import EmailMessage
from typing import Optional

import httpx

from app import settings_store
from app.config import settings

log = logging.getLogger(__name__)


@dataclass
class SendResult:
    delivered: bool
    transport: str
    detail: str = ""

    @property
    def needs_manual_delivery(self) -> bool:
        return not self.delivered


def _invite_body(url: str, inviter: Optional[str], is_reset: bool) -> tuple[str, str]:
    action = "reset your password" if is_reset else "set up your account"
    subject = ("MAP Scan — password reset" if is_reset
               else "You have been invited to MAP Scan")
    who = f" by {inviter}" if inviter else ""
    text = f"""You have been invited{who} to {action} for MAP Scan, the Henry
Holsters MAP compliance dashboard.

Open this link to choose your password:

  {url}

The link can be used once and expires in 72 hours. If you were not expecting
this, you can ignore it -- no change is made until the link is opened.
"""
    return subject, text


async def send_invite(to_email: str, url: str, inviter: Optional[str] = None,
                      is_reset: bool = False) -> SendResult:
    subject, text = _invite_body(url, inviter, is_reset)
    provider = (await settings_store.get("email_provider", "manual") or "manual").lower()

    if provider == "gmail_api":
        return await _send_gmail_api(to_email, subject, text)
    if provider == "resend":
        return await _send_resend(to_email, subject, text)
    if provider == "smtp":
        return await _send_smtp(to_email, subject, text)

    log.info("invite for %s not emailed (provider=manual); link returned to admin",
             to_email)
    return SendResult(False, "manual", "No email provider configured.")


# ---------------------------------------------------------------------------
# Gmail API over HTTPS, service account + domain-wide delegation
# ---------------------------------------------------------------------------
_TOKEN_URL = "https://oauth2.googleapis.com/token"
_SEND_URL = "https://gmail.googleapis.com/gmail/v1/users/me/messages/send"
_SCOPE = "https://www.googleapis.com/auth/gmail.send"

# Access tokens last an hour; cache so a burst of invites signs one JWT.
_token_cache: dict[str, tuple[str, float]] = {}


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def normalise_pem(raw: str) -> str:
    """Accept a private key however it was copied out of the JSON key file.

    Google's service-account JSON stores the key as a single JSON string, so
    `private_key` reads as `-----BEGIN PRIVATE KEY-----\nMIIE...` with LITERAL
    backslash-n, not newlines. Pasting that verbatim -- the obvious thing to do
    -- fails deep inside OpenSSL with "Could not deserialize key data", which
    says nothing about the real problem. Normalising here means both the raw
    JSON value and a properly unescaped PEM work.
    """
    if not raw:
        return ""
    text = raw.strip()
    # A whole JSON string may arrive with its surrounding quotes attached.
    if len(text) >= 2 and text[0] == text[-1] == '"':
        text = text[1:-1]
    text = text.replace("\\r\\n", "\n").replace("\\n", "\n").replace("\r\n", "\n")
    return text.strip() + "\n"


async def _gmail_access_token(sa_email: str, sa_key_pem: str,
                              send_as: str) -> tuple[Optional[str], str]:
    """Sign a JWT and exchange it for an access token.

    Returns (token, reason). `reason` explains WHICH step failed -- this used to
    return a bare None and the caller guessed "check domain-wide delegation",
    which sent at least one person to the Workspace admin console over a key
    that had simply been pasted with literal \\n in it.

    Hand-rolled rather than pulling in google-auth: it is ~20 lines against the
    `cryptography` dependency this app already has, and every megabyte of RSS
    matters under MemoryMax on a swapless box.
    """
    cached = _token_cache.get(send_as)
    if cached and cached[1] > time.time() + 60:
        return cached[0], ""

    pem = normalise_pem(sa_key_pem)
    if "BEGIN" not in pem or "PRIVATE KEY" not in pem:
        return None, ("The private key does not look like a PEM block. Paste the "
                      "`private_key` value from the service-account JSON, starting "
                      "at -----BEGIN PRIVATE KEY-----.")
    try:
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import padding
        key = serialization.load_pem_private_key(pem.encode("utf-8"), password=None)
    except Exception as exc:
        log.error("gmail service-account key could not be loaded: %s", exc)
        return None, (f"The private key could not be read ({type(exc).__name__}). "
                      f"Copy the `private_key` value from the service-account JSON "
                      f"exactly, including the BEGIN/END lines.")

    now = int(time.time())
    header = {"alg": "RS256", "typ": "JWT"}
    claims = {
        "iss": sa_email,
        "sub": send_as,          # the Workspace user to impersonate
        "scope": _SCOPE,
        "aud": _TOKEN_URL,
        "iat": now,
        "exp": now + 3600,
    }
    signing_input = f"{_b64url(json.dumps(header).encode())}.{_b64url(json.dumps(claims).encode())}"
    try:
        sig = key.sign(signing_input.encode("ascii"), padding.PKCS1v15(), hashes.SHA256())
    except Exception as exc:
        log.error("gmail JWT signing failed: %s", exc)
        return None, f"Signing the request failed: {type(exc).__name__}: {exc}"
    assertion = f"{signing_input}.{_b64url(sig)}"

    try:
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.post(_TOKEN_URL, data={
                "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
                "assertion": assertion,
            })
    except Exception as exc:
        log.error("gmail token request failed: %s", exc)
        return None, f"Could not reach Google's token endpoint: {type(exc).__name__}"

    if resp.status_code >= 300:
        log.error("gmail token HTTP %s: %s", resp.status_code, resp.text[:300])
        try:
            err = (resp.json() or {}).get("error", "")
        except Exception:
            err = ""
        # Google's own error code distinguishes the two failures that actually
        # happen here, and they need opposite fixes -- so name the right one
        # instead of always blaming delegation.
        if err == "unauthorized_client":
            reason = (f"Google rejected the request as unauthorized_client. Grant "
                      f"domain-wide delegation for this service account's numeric "
                      f"client ID with scope {_SCOPE}, and check that the send-as "
                      f"address ({send_as}) is a real mailbox in that Workspace.")
        elif err == "invalid_grant":
            reason = (f"Google rejected the request as invalid_grant. Usually the "
                      f"send-as address ({send_as}) is not a user in this Workspace, "
                      f"or the server clock has drifted.")
        else:
            reason = f"Google returned HTTP {resp.status_code}: {resp.text[:160]}"
        return None, reason

    data = resp.json()
    token = data.get("access_token")
    if not token:
        return None, "Google's response contained no access_token."
    _token_cache[send_as] = (token, time.time() + int(data.get("expires_in", 3600)))
    return token, ""


async def _send_gmail_api(to_email: str, subject: str, text: str) -> SendResult:
    sa_email = await settings_store.get("gmail_sa_email")
    sa_key = await settings_store.get("gmail_sa_key")
    send_as = await settings_store.get("gmail_send_as") or await settings_store.get("email_from")
    if not (sa_email and sa_key and send_as):
        return SendResult(False, "gmail_api",
                          "Service account email, private key, and send-as address "
                          "are all required.")

    token, reason = await _gmail_access_token(sa_email, sa_key, send_as)
    if not token:
        return SendResult(False, "gmail_api", reason or "Could not obtain an access token.")

    msg = EmailMessage()
    msg["From"] = send_as
    msg["To"] = to_email
    msg["Subject"] = subject
    msg.set_content(text)

    try:
        async with httpx.AsyncClient(timeout=25) as client:
            resp = await client.post(
                _SEND_URL,
                headers={"Authorization": f"Bearer {token}",
                         "Content-Type": "application/json"},
                json={"raw": _b64url(msg.as_bytes())},
            )
    except Exception as exc:
        log.error("gmail send failed: %s", exc)
        return SendResult(False, "gmail_api", f"{type(exc).__name__}: {exc}")

    if resp.status_code >= 300:
        log.error("gmail send HTTP %s: %s", resp.status_code, resp.text[:300])
        return SendResult(False, "gmail_api", f"HTTP {resp.status_code}: {resp.text[:200]}")
    return SendResult(True, "gmail_api")


async def _send_resend(to_email: str, subject: str, text: str) -> SendResult:
    key = await settings_store.get("resend_key")
    sender = await settings_store.get("email_from")
    if not (key and sender):
        return SendResult(False, "resend", "Resend API key or from-address missing.")
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.post(
                "https://api.resend.com/emails",
                headers={"Authorization": f"Bearer {key}"},
                json={"from": sender, "to": [to_email],
                      "subject": subject, "text": text},
            )
    except Exception as exc:
        log.error("resend send failed: %s", exc)
        return SendResult(False, "resend", f"{type(exc).__name__}: {exc}")
    if resp.status_code >= 300:
        log.error("resend HTTP %s: %s", resp.status_code, resp.text[:300])
        return SendResult(False, "resend", f"HTTP {resp.status_code}: {resp.text[:200]}")
    return SendResult(True, "resend")


async def _send_smtp(to_email: str, subject: str, text: str) -> SendResult:
    """Plain SMTP, for hosts that allow outbound 25/465/587."""
    host = await settings_store.get("smtp_host")
    sender = await settings_store.get("email_from")
    port = await settings_store.get_int("smtp_port", 587)
    user = await settings_store.get("smtp_user")
    password = await settings_store.get("smtp_password")
    if not (host and sender):
        return SendResult(False, "smtp", "SMTP host or from-address missing.")
    msg = EmailMessage()
    msg["From"] = sender
    msg["To"] = to_email
    msg["Subject"] = subject
    msg.set_content(text)
    def _blocking_send() -> None:
        if port == 465:
            with smtplib.SMTP_SSL(host, 465, timeout=20) as srv:
                if user:
                    srv.login(user, password or "")
                srv.send_message(msg)
        else:
            with smtplib.SMTP(host, port, timeout=20) as srv:
                srv.starttls()
                if user:
                    srv.login(user, password or "")
                srv.send_message(msg)

    try:
        # smtplib is blocking; keep it off the event loop.
        await asyncio.to_thread(_blocking_send)
    except Exception as exc:
        log.error("smtp send failed: %s", exc)
        return SendResult(
            False, "smtp",
            f"{type(exc).__name__}: {exc} -- note that DigitalOcean blocks "
            f"outbound SMTP (25/465/587) on their VPSes.",
        )
    return SendResult(True, "smtp")
