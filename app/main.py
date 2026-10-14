"""mapscan -- FastAPI + Jinja2 dashboard.

Auth is enforced BY THIS APP -- session cookies, invite links, forced password
rotation -- not by nginx. The vhost deliberately sets no auth_basic, because a
second password prompt would sit in front of the login page and would break
emailed invite links. The app binds 127.0.0.1 and is reached only through the
nginx TLS vhost, but the login form is directly internet-facing, so it is
rate-limited per-email and per-IP in app/auth.py.

Every route requires a session except the eBay Marketplace Account Deletion
endpoint, which eBay's servers must be able to call; see app/ebay_notify.py.
"""
from __future__ import annotations

import hashlib
import json
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from arq.connections import ArqRedis, RedisSettings, create_pool
from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import (HTMLResponse, JSONResponse, PlainTextResponse,
                               RedirectResponse, Response)
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from app import auth, db, ebay_notify, mail, settings_store
from app.auth import (LoginRedirect, PasswordChangeRequired, require_admin,
                      require_user)
from app.config import settings

logging.basicConfig(
    level=settings.log_level,
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
)
log = logging.getLogger("mapscan.api")

BASE_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))


def _money(value) -> str:
    """Render a price, or an em dash when there isn't one.

    Prohibited-channel violations open with no price at all when the listing is
    an auction or best-offer -- migration 007 dropped NOT NULL on these columns
    for exactly that case -- so every price cell has to tolerate NULL. Before
    this filter, one such eBay listing 500'd the whole violations page.
    """
    if value is None:
        return "\u2014"
    return f"${value:,.2f}"


def _pct(value) -> str:
    """Percentage under MAP. Blank for unauthorized listings, which have no MAP
    comparison to make -- '0.0%' would read as 'exactly at MAP', which is wrong."""
    if value is None:
        return "\u2014"
    return f"{value:.1f}%"


templates.env.filters["money"] = _money
templates.env.filters["pct"] = _pct


def _asset_version() -> str:
    """Cache-buster for /static, derived from the newest asset mtime.

    StaticFiles sends ETag and Last-Modified but no Cache-Control, so browsers
    fall back to heuristic caching and can serve a stale stylesheet for hours
    after a deploy -- which looks exactly like the CSS change not working. The
    query string changes whenever an asset does, so the old copy is never
    reused.
    """
    newest = 0.0
    static_dir = BASE_DIR / "static"
    for f in static_dir.glob("*"):
        try:
            newest = max(newest, f.stat().st_mtime)
        except OSError:
            continue
    return str(int(newest))


templates.env.globals["asset_v"] = _asset_version()
templates.env.globals["brand_name"] = settings.brand_name
templates.env.globals["contact_email"] = settings.contact_email

_arq: Optional[ArqRedis] = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _arq
    await db.init_pool()
    _arq = await create_pool(RedisSettings.from_dsn(settings.redis_url))
    settings.evidence_dir.mkdir(parents=True, exist_ok=True)
    log.info("api up on %s:%s", settings.bind_host, settings.bind_port)
    yield
    if _arq is not None:
        await _arq.aclose()
    await db.close_pool()


app = FastAPI(title="MAP Scan", lifespan=lifespan, docs_url=None, redoc_url=None)

if not settings.secret_key:
    # Refuse to start rather than silently signing sessions with a default: a
    # predictable key means forgeable session cookies on an internet-facing app.
    raise RuntimeError(
        "SECRET_KEY is not set. Generate one with:\n"
        '  python3 -c "import secrets; print(secrets.token_urlsafe(48))"'
    )

app.add_middleware(
    SessionMiddleware,
    secret_key=settings.secret_key,
    session_cookie="mapscan_session",
    max_age=settings.session_max_age,
    same_site="lax",
    https_only=settings.cookie_secure,
)
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")


@app.exception_handler(LoginRedirect)
async def _login_redirect(request: Request, exc: LoginRedirect):
    return auth.redirect_to_login(exc.next_url)


@app.exception_handler(PasswordChangeRequired)
async def _password_change_required(request: Request, exc: PasswordChangeRequired):
    # Straight to the form -- never via /login, which would loop.
    return RedirectResponse("/change-password", status_code=303)


def _token_sha(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def ctx(request: Request, user: Optional[dict] = None, **extra) -> dict:
    """Base template context: current user, CSRF token, nav counts."""
    base = {"user": user, "csrf": auth.csrf_token(request)}
    base.update(extra)
    return base


# ---------------------------------------------------------------------------
# auth routes
# ---------------------------------------------------------------------------
@app.get("/login", response_class=HTMLResponse)
async def login_form(request: Request, next: str = "/", error: str = ""):
    signed_in = await auth.load_user(request)
    if signed_in:
        # Respect a pending rotation, otherwise an already-signed-in user with
        # must_change_password bounces between here and "/".
        if signed_in["must_change_password"]:
            return RedirectResponse("/change-password", status_code=303)
        return RedirectResponse("/", status_code=303)
    return templates.TemplateResponse(
        request, "login.html", ctx(request, None, next=next, error=error)
    )


@app.post("/login")
async def login_submit(request: Request, email: str = Form(...),
                       password: str = Form(...), next: str = Form("/")):
    ip = auth.client_ip(request)
    email_lower = (email or "").strip().lower()

    blocked = await auth.login_blocked(email_lower, ip)
    if blocked:
        await auth.record_event("login_fail", email_lower, None, ip, "rate limited")
        return templates.TemplateResponse(
            request, "login.html",
            ctx(request, None, next=next, error=blocked), status_code=429,
        )

    row = await db.fetchrow(
        "SELECT id, email, password_hash, is_active, must_change_password "
        "FROM users WHERE email_lower = $1",
        email_lower,
    )
    ok = bool(row) and row["is_active"] and await auth.verify_password(
        password, row["password_hash"]
    )
    if not ok:
        await auth.record_event("login_fail", email_lower,
                                row["id"] if row else None, ip)
        # Deliberately identical message for unknown-user and wrong-password, so
        # the form cannot be used to enumerate who has an account.
        return templates.TemplateResponse(
            request, "login.html",
            ctx(request, None, next=next, error="Email or password is incorrect."),
            status_code=401,
        )

    auth.start_session(request, row["id"])
    await db.execute("UPDATE users SET last_login_at = now() WHERE id = $1", row["id"])
    await auth.record_event("login_ok", email_lower, row["id"], ip)

    if row["must_change_password"]:
        return RedirectResponse("/change-password", status_code=303)
    # Only ever redirect to a local path, never to an attacker-supplied host.
    dest = next if next.startswith("/") and not next.startswith("//") else "/"
    return RedirectResponse(dest, status_code=303)


@app.get("/logout")
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


@app.get("/change-password", response_class=HTMLResponse)
async def change_password_form(request: Request, error: str = ""):
    user = await auth.load_user(request)
    if user is None:
        return auth.redirect_to_login("/change-password")
    return templates.TemplateResponse(
        request, "change_password.html",
        ctx(request, user, error=error, forced=user["must_change_password"]),
    )


@app.post("/change-password")
async def change_password_submit(request: Request, current: str = Form(...),
                                 new_password: str = Form(...),
                                 confirm: str = Form(...),
                                 csrf_token: str = Form(...)):
    user = await auth.load_user(request)
    if user is None:
        return auth.redirect_to_login("/change-password")
    auth.check_csrf(request, csrf_token)

    row = await db.fetchrow("SELECT password_hash, email FROM users WHERE id=$1", user["id"])
    if not await auth.verify_password(current, row["password_hash"]):
        return templates.TemplateResponse(
            request, "change_password.html",
            ctx(request, user, error="Current password is incorrect.",
                forced=user["must_change_password"]), status_code=400,
        )
    if new_password != confirm:
        return templates.TemplateResponse(
            request, "change_password.html",
            ctx(request, user, error="New passwords do not match.",
                forced=user["must_change_password"]), status_code=400,
        )
    if await auth.verify_password(new_password, row["password_hash"]):
        return templates.TemplateResponse(
            request, "change_password.html",
            ctx(request, user, error="New password must differ from the current one.",
                forced=user["must_change_password"]), status_code=400,
        )
    try:
        auth.validate_password(new_password, row["email"])
    except HTTPException as exc:
        return templates.TemplateResponse(
            request, "change_password.html",
            ctx(request, user, error=exc.detail,
                forced=user["must_change_password"]), status_code=400,
        )

    await db.execute(
        "UPDATE users SET password_hash=$2, must_change_password=false, "
        "password_changed_at=now() WHERE id=$1",
        user["id"], await auth.hash_password(new_password),
    )
    await auth.record_event("password_changed", row["email"].lower(), user["id"],
                            auth.client_ip(request))
    return RedirectResponse("/?changed=1", status_code=303)


# ---------------------------------------------------------------------------
# invite acceptance (unauthenticated -- the token IS the credential)
# ---------------------------------------------------------------------------
@app.get("/invite/{token}", response_class=HTMLResponse)
async def invite_form(request: Request, token: str, error: str = ""):
    # Do not consume the token on GET: a mail scanner prefetching the link would
    # burn it before the human ever sees the form. Hashing happens in Python so
    # there is no pgcrypto dependency.
    row = await db.fetchrow(
        """
        SELECT u.email, t.purpose FROM auth_tokens t
          JOIN users u ON u.id = t.user_id
         WHERE t.token_hash = $1
           AND t.used_at IS NULL AND t.expires_at > now() AND u.is_active
        """,
        _token_sha(token),
    )
    if row is None:
        return templates.TemplateResponse(
            request, "invite.html",
            ctx(request, None, invalid=True, email=None, token=token), status_code=400,
        )
    return templates.TemplateResponse(
        request, "invite.html",
        ctx(request, None, invalid=False, email=row["email"], token=token,
            purpose=row["purpose"], error=error),
    )


@app.post("/invite/{token}")
async def invite_submit(request: Request, token: str,
                        new_password: str = Form(...), confirm: str = Form(...)):
    purpose_row = await db.fetchrow(
        "SELECT purpose FROM auth_tokens WHERE token_hash = $1", 
        _token_sha(token),
    )
    purpose = purpose_row["purpose"] if purpose_row else "invite"

    if new_password != confirm:
        return RedirectResponse(f"/invite/{token}?error=Passwords+do+not+match",
                                status_code=303)

    # Validate BEFORE consuming, so a weak password does not burn the token.
    user_peek = await db.fetchrow(
        """
        SELECT u.email FROM auth_tokens t JOIN users u ON u.id = t.user_id
         WHERE t.token_hash = $1 AND t.used_at IS NULL AND t.expires_at > now()
        """,
        _token_sha(token),
    )
    if user_peek is None:
        return RedirectResponse(f"/invite/{token}", status_code=303)
    try:
        auth.validate_password(new_password, user_peek["email"])
    except HTTPException as exc:
        from urllib.parse import quote
        return RedirectResponse(f"/invite/{token}?error={quote(exc.detail)}",
                                status_code=303)

    user = await auth.consume_token(token, purpose)
    if user is None:
        return RedirectResponse(f"/invite/{token}", status_code=303)

    await db.execute(
        "UPDATE users SET password_hash=$2, must_change_password=false, "
        "password_changed_at=now() WHERE id=$1",
        user["id"], await auth.hash_password(new_password),
    )
    await auth.record_event("invite_accepted", user["email_lower"], user["id"],
                            auth.client_ip(request))
    auth.start_session(request, user["id"])
    return RedirectResponse("/", status_code=303)


# ---------------------------------------------------------------------------
# settings (admin only) -- credentials entered here, never in a file
# ---------------------------------------------------------------------------
@app.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request, notice: str = "", error: str = ""):
    user = await require_admin(request)
    st = await settings_store.status()
    usage = await db.fetch(
        "SELECT provider, period, used FROM search_budget ORDER BY period DESC LIMIT 3"
    )
    spend = await db.fetchrow(
        """
        SELECT count(*)                                    AS queries_30d,
               coalesce(sum(new_listings), 0)              AS listings_30d,
               coalesce(sum(new_domains), 0)               AS domains_30d,
               count(*) FILTER (WHERE result_count = 0)    AS zero_result_30d
          FROM search_query_log
         WHERE ran_at > now() - interval '30 days'
        """
    )
    return templates.TemplateResponse(
        request, "settings.html",
        ctx(request, user, st=st, usage=usage, spend=spend, notice=notice,
            error=error, encryption_ok=settings_store.encryption_available(),
            ebay_default_endpoint=settings.public_base_url.rstrip("/") + ebay_notify.PATH,
            ebay_endpoint=await ebay_notify.endpoint_url(),
            # Shown in full, unlike every other secret here. This one is a
            # shared secret with eBay by construction -- it has to be typed
            # into their portal -- so hiding it from the admin who has to
            # transcribe it protects nothing and just sends them to the CLI.
            ebay_token_value=await settings_store.get("ebay_verification_token", ""),
            default_extraction_model=_extract_mod().DEFAULT_EXTRACTION_MODEL),
    )


@app.post("/settings")
async def settings_save(request: Request, csrf_token: str = Form(...)):
    user = await require_admin(request)
    auth.check_csrf(request, csrf_token)
    form = await request.form()

    if not settings_store.encryption_available():
        from urllib.parse import quote
        return RedirectResponse(
            "/settings?error=" + quote(
                "SETTINGS_KEY is not configured on the server, so secrets cannot "
                "be stored. Set it in .env and restart."),
            status_code=303,
        )

    saved, cleared = [], []
    for key in settings_store.KNOWN:
        if key not in form:
            continue
        raw = (form.get(key) or "").strip()
        # A blank secret field means "leave as is", not "delete" -- the field is
        # never pre-filled with the stored value, so blank is the normal state.
        is_secret = settings_store.KNOWN[key][1]
        if is_secret and raw == "":
            continue
        if raw == "__CLEAR__":
            await settings_store.set_value(key, None, user["id"])
            cleared.append(key)
            continue
        await settings_store.set_value(key, raw, user["id"])
        saved.append(key)

    await auth.record_event(
        "settings_changed", user["email"].lower(), user["id"],
        auth.client_ip(request),
        f"saved={','.join(saved)} cleared={','.join(cleared)}",
    )
    from urllib.parse import quote
    msg = f"Saved {len(saved)} setting(s)." + (f" Cleared {len(cleared)}." if cleared else "")
    return RedirectResponse(f"/settings?notice={quote(msg)}", status_code=303)


@app.post("/settings/test-email")
async def settings_test_email(request: Request, csrf_token: str = Form(...)):
    """Send a real invite-shaped email to the admin, to prove the transport works."""
    user = await require_admin(request)
    auth.check_csrf(request, csrf_token)
    result = await mail.send_invite(
        user["email"], f"{settings.public_base_url}/login",
        inviter="MAP Scan settings test",
    )
    from urllib.parse import quote
    if result.delivered:
        msg = f"Test email sent to {user['email']} via {result.transport}."
        return RedirectResponse(f"/settings?notice={quote(msg)}", status_code=303)
    return RedirectResponse(
        f"/settings?error={quote(f'{result.transport}: {result.detail}')}",
        status_code=303,
    )


def _extract_mod():
    """Lazy import of worker.extract -- see the note in the dashboard route."""
    from worker import extract
    return extract


@app.post("/settings/test-extraction")
async def settings_test_extraction(request: Request, csrf_token: str = Form(...)):
    """Prove the OpenAI key works AND that the prompt still behaves.

    Deliberately more than an auth check: it runs a fixture page carrying both a
    list price and a sale price, and asserts the sale price comes back. A key
    that authenticates but a model that returns the wrong one of two prices is
    the failure that would otherwise show up as silent MAP noise.
    """
    user = await require_admin(request)
    auth.check_csrf(request, csrf_token)
    from urllib.parse import quote

    try:
        ok, detail = await _extract_mod().verify_credentials()
    except Exception as exc:
        log.exception("extraction credential test failed")
        ok, detail = False, f"{type(exc).__name__}: {exc}"
    key = "notice" if ok else "error"
    return RedirectResponse(f"/settings?{key}={quote(detail)}", status_code=303)


@app.post("/settings/test-vendor")
async def settings_test_vendor(request: Request, csrf_token: str = Form(...)):
    """Fetch a known-good URL through the configured vendor and report back.

    The vendor request shapes were written from documentation and never
    exercised against a live API, so this is the difference between "the key is
    saved" and "the key works".
    """
    user = await require_admin(request)
    auth.check_csrf(request, csrf_token)

    from worker.fetch import Fetcher
    from urllib.parse import quote

    vendor = await settings_store.get("fetch_vendor", "") or ""
    if not vendor:
        return RedirectResponse(
            "/settings?error=" + quote("No fetch vendor selected."), status_code=303)

    probe = "https://example.com/"
    try:
        async with Fetcher() as f:
            if not f.vendor_enabled:
                return RedirectResponse(
                    "/settings?error=" + quote(
                        f"Vendor {vendor} is selected but its API key is not set."),
                    status_code=303)
            res = await f._tier2(probe)
    except Exception as exc:
        log.exception("vendor test failed")
        return RedirectResponse(
            "/settings?error=" + quote(f"{vendor}: {type(exc).__name__}: {exc}"),
            status_code=303)

    if res.ok:
        msg = (f"{vendor} works — fetched {len(res.html or '')} bytes through it. "
               f"Blocked domains and screenshots are now available.")
        return RedirectResponse(f"/settings?notice={quote(msg)}", status_code=303)
    return RedirectResponse(
        "/settings?error=" + quote(f"{vendor} returned: {res.error or res.status}"),
        status_code=303)


# ---------------------------------------------------------------------------
# user management (admin only)
# ---------------------------------------------------------------------------
@app.get("/users", response_class=HTMLResponse)
async def users_page(request: Request, invited: str = "", link: str = "",
                     notice: str = ""):
    user = await require_admin(request)
    rows = await db.fetch(
        """
        SELECT u.id, u.email, u.is_admin, u.is_active, u.must_change_password,
               u.last_login_at, u.password_changed_at, u.created_at,
               c.email AS created_by_email,
               (SELECT max(t.expires_at) FROM auth_tokens t
                 WHERE t.user_id = u.id AND t.used_at IS NULL
                   AND t.expires_at > now()) AS pending_invite_expires
          FROM users u
          LEFT JOIN users c ON c.id = u.created_by
         ORDER BY u.created_at
        """
    )
    events = await db.fetch(
        "SELECT event, email_lower, ip, detail, created_at FROM auth_events "
        "ORDER BY created_at DESC LIMIT 25"
    )
    return templates.TemplateResponse(
        request, "users.html",
        ctx(request, user, rows=rows, events=events, invited=invited,
            link=link, notice=notice, email_enabled=settings.email_enabled,
            email_provider=settings.email_provider),
    )


@app.post("/users")
async def users_create(request: Request, email: str = Form(...),
                       is_admin: str = Form(""), csrf_token: str = Form(...)):
    admin = await require_admin(request)
    auth.check_csrf(request, csrf_token)
    clean = auth.validate_email(email)
    lower = clean.lower()

    existing = await db.fetchval("SELECT id FROM users WHERE email_lower=$1", lower)
    if existing:
        from urllib.parse import quote
        return RedirectResponse(
            f"/users?notice={quote('That email already has an account. Use Re-invite to send a fresh link.')}",
            status_code=303,
        )

    user_id = await db.fetchval(
        "INSERT INTO users (email, email_lower, is_admin, must_change_password, created_by) "
        "VALUES ($1,$2,$3,false,$4) RETURNING id",
        clean, lower, bool(is_admin), admin["id"],
    )
    raw = await auth.issue_token(user_id, "invite", admin["id"])
    url = auth.invite_url(raw)
    result = await mail.send_invite(clean, url, admin["email"])
    await auth.record_event("invite_sent", lower, user_id, auth.client_ip(request),
                            f"transport={result.transport} delivered={result.delivered}")

    from urllib.parse import quote
    if result.delivered:
        return RedirectResponse(
            f"/users?invited={quote(clean)}&notice={quote('Invite emailed.')}",
            status_code=303,
        )
    # Not delivered: hand the link to the admin rather than losing it.
    return RedirectResponse(
        f"/users?invited={quote(clean)}&link={quote(url)}"
        f"&notice={quote('No email provider configured — copy this link to the user:')}",
        status_code=303,
    )


@app.post("/users/{user_id}/reinvite")
async def users_reinvite(request: Request, user_id: int, csrf_token: str = Form(...)):
    admin = await require_admin(request)
    auth.check_csrf(request, csrf_token)
    row = await db.fetchrow("SELECT email, email_lower FROM users WHERE id=$1", user_id)
    if row is None:
        raise HTTPException(404, "user not found")
    raw = await auth.issue_token(user_id, "reset", admin["id"])
    url = auth.invite_url(raw)
    result = await mail.send_invite(row["email"], url, admin["email"], is_reset=True)
    await auth.record_event("invite_sent", row["email_lower"], user_id,
                            auth.client_ip(request),
                            f"reinvite transport={result.transport}")
    from urllib.parse import quote
    if result.delivered:
        return RedirectResponse(f"/users?notice={quote('Reset link emailed.')}",
                                status_code=303)
    return RedirectResponse(
        f"/users?invited={quote(row['email'])}&link={quote(url)}"
        f"&notice={quote('No email provider configured — copy this reset link to the user:')}",
        status_code=303,
    )


@app.post("/users/{user_id}/toggle")
async def users_toggle(request: Request, user_id: int, csrf_token: str = Form(...)):
    admin = await require_admin(request)
    auth.check_csrf(request, csrf_token)
    if user_id == admin["id"]:
        raise HTTPException(400, "You cannot deactivate your own account.")
    row = await db.fetchrow("SELECT email_lower, is_active FROM users WHERE id=$1", user_id)
    if row is None:
        raise HTTPException(404, "user not found")
    # Never leave the instance with no way in.
    if row["is_active"]:
        remaining = await db.fetchval(
            "SELECT count(*) FROM users WHERE is_admin AND is_active AND id <> $1",
            user_id,
        )
        if remaining == 0:
            raise HTTPException(400, "That is the last active administrator.")
    new_state = not row["is_active"]
    await db.execute("UPDATE users SET is_active=$2 WHERE id=$1", user_id, new_state)
    await auth.record_event(
        "user_reactivated" if new_state else "user_deactivated",
        row["email_lower"], user_id, auth.client_ip(request),
    )
    return RedirectResponse("/users", status_code=303)


def arq() -> ArqRedis:
    if _arq is None:
        raise HTTPException(503, "queue unavailable")
    return _arq


# ---------------------------------------------------------------------------
# dashboard
# ---------------------------------------------------------------------------
@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request, user: dict = Depends(require_user)):
    stats = await db.fetchrow(
        """
        SELECT
          (SELECT count(*) FROM products WHERE active)                        AS products,
          (SELECT count(*) FROM listings WHERE status='confirmed')            AS confirmed,
          (SELECT count(*) FROM listings WHERE status='candidate')            AS review,
          (SELECT count(*) FROM sellers)                                     AS sellers,
          (SELECT count(*) FROM violations WHERE status='open')               AS open_violations,
          (SELECT count(*) FROM violations
            WHERE status='resolved' AND resolved_at > now()-interval '30 days') AS resolved_30d,
          (SELECT max(finished_at) FROM crawl_jobs
            WHERE kind='recheck' AND status='done')                          AS last_recheck
        """
    )
    open_violations = await db.fetch(
        """
        SELECT v.id, v.opened_at, v.opening_price, v.lowest_price,
               v.worst_delta_pct, v.map_at_open, v.acknowledged_at, v.kind,
               p.sku, p.title, s.domain, l.url, l.id AS listing_id,
               (SELECT count(*) FROM evidence e WHERE e.violation_id = v.id) AS evidence_count
          FROM violations v
          JOIN listings l ON l.id = v.listing_id
          JOIN products p ON p.id = l.product_id
          JOIN sellers  s ON s.id = l.seller_id
         WHERE v.status = 'open'
         -- unauthorized listings have no delta, so surface them first rather
         -- than letting NULLS LAST bury them under every MAP violation.
         ORDER BY (v.kind = 'unauthorized') DESC,
                  v.worst_delta_pct DESC NULLS LAST, v.opened_at DESC
        """
    )
    recent_jobs = await db.fetch(
        "SELECT id, kind, target, status, progress, created_at, finished_at "
        "FROM crawl_jobs ORDER BY created_at DESC LIMIT 8"
    )
    budget = await db.fetch(
        "SELECT provider, period, used FROM search_budget "
        "ORDER BY period DESC LIMIT 4"
    )
    # Imported here rather than at module scope, matching the worker.ebay
    # pattern below: it drags selectolax/extruct in, which the API otherwise
    # has no use for.
    from worker import extract

    vendor = await settings_store.get("fetch_vendor", "") or ""
    vendor_key = await settings_store.get(f"{vendor}_key", "") if vendor else ""
    extraction_model = await settings_store.get(
        "extraction_model", extract.DEFAULT_EXTRACTION_MODEL)
    llm_on = await extract.llm_enabled()
    return templates.TemplateResponse(
        request, "dashboard.html",
        ctx(request, user, stats=stats, violations=open_violations,
            jobs=recent_jobs, budget=budget, settings=settings,
            vendor=vendor if vendor_key else "",
            extraction_model=extraction_model, llm_on=llm_on),
    )


# ---------------------------------------------------------------------------
# review queue
# ---------------------------------------------------------------------------
@app.get("/review", response_class=HTMLResponse)
async def review_queue(request: Request, page: int = 0,
                       user: dict = Depends(require_user)):
    per_page = 50
    rows = await db.fetch(
        """
        SELECT l.id, l.url, l.match_score, l.match_signals, l.title_seen,
               l.sku_seen, l.upc_seen, l.discovered_via, l.first_seen,
               p.sku, p.title, p.upc, p.map_price, s.domain, s.is_authorized
          FROM listings l
          JOIN products p ON p.id = l.product_id
          JOIN sellers  s ON s.id = l.seller_id
         WHERE l.status = 'candidate'
         ORDER BY l.match_score DESC NULLS LAST, l.first_seen DESC
         LIMIT $1 OFFSET $2
        """,
        per_page, page * per_page,
    )
    total = await db.fetchval("SELECT count(*) FROM listings WHERE status='candidate'")
    return templates.TemplateResponse(
        request, "review.html",
        ctx(request, user, rows=rows, total=total, page=page, per_page=per_page),
    )


@app.post("/review/{listing_id}")
async def review_decide(request: Request, listing_id: int,
                        decision: str = Form(...), note: str = Form(""),
                        user: dict = Depends(require_user)):
    if decision not in {"confirmed", "rejected"}:
        raise HTTPException(400, "decision must be 'confirmed' or 'rejected'")
    updated = await db.fetchval(
        """
        UPDATE listings
           SET status = $2, reviewed_at = now(), reviewed_note = NULLIF($3,'')
         WHERE id = $1 RETURNING id
        """,
        listing_id, decision, note,
    )
    if updated is None:
        raise HTTPException(404, "listing not found")
    return JSONResponse({"ok": True, "listing_id": listing_id, "status": decision})


# ---------------------------------------------------------------------------
# violations
# ---------------------------------------------------------------------------
@app.get("/violations", response_class=HTMLResponse)
async def violations_page(request: Request, status: str = "open",
                          user: dict = Depends(require_user)):
    if status not in {"open", "resolved", "all"}:
        status = "open"
    where = "" if status == "all" else "WHERE v.status = $1"
    args = () if status == "all" else (status,)
    rows = await db.fetch(
        f"""
        SELECT v.*, p.sku, p.title, s.domain, l.url, l.id AS listing_id,
               -- Marketplace username, for channels where the domain alone does
               -- not identify who is selling: every eBay listing shows
               -- "ebay.com", and the accusation is against the seller, not eBay.
               -- NULL once an account-deletion notice has been honoured.
               l.marketplace_seller, l.discovered_via,
               (SELECT count(*) FROM evidence e WHERE e.violation_id = v.id) AS evidence_count,
               -- Newest screenshot to preview on hover. Prefer one captured for
               -- THIS violation; fall back to the listing's most recent, since a
               -- listing can carry several violations over time and an older
               -- capture is still a useful preview of the page.
               COALESCE(
                 (SELECT e.id FROM evidence e
                   WHERE e.violation_id = v.id AND e.screenshot_path IS NOT NULL
                   ORDER BY e.captured_at DESC LIMIT 1),
                 (SELECT e.id FROM evidence e
                   WHERE e.listing_id = l.id AND e.screenshot_path IS NOT NULL
                   ORDER BY e.captured_at DESC LIMIT 1)
               ) AS shot_evidence_id
          FROM violations v
          JOIN listings l ON l.id = v.listing_id
          JOIN products p ON p.id = l.product_id
          JOIN sellers  s ON s.id = l.seller_id
        {where}
         ORDER BY v.opened_at DESC
         LIMIT 300
        """,
        *args,
    )
    return templates.TemplateResponse(
        request, "violations.html", ctx(request, user, rows=rows, status=status)
    )


@app.post("/violations/{violation_id}/ack")
async def ack_violation(violation_id: int, note: str = Form(""),
                        user: dict = Depends(require_user)):
    await db.execute(
        "UPDATE violations SET acknowledged_at = now(), note = NULLIF($2,'') WHERE id = $1",
        violation_id, note,
    )
    return JSONResponse({"ok": True})


@app.get("/listings/{listing_id}", response_class=HTMLResponse)
async def listing_detail(request: Request, listing_id: int,
                         user: dict = Depends(require_user)):
    listing = await db.fetchrow(
        """
        SELECT l.*, p.sku, p.title AS product_title, p.upc AS product_upc,
               p.map_price, p.msrp, s.domain, s.is_authorized, s.fetch_tier
          FROM listings l
          JOIN products p ON p.id = l.product_id
          JOIN sellers  s ON s.id = l.seller_id
         WHERE l.id = $1
        """,
        listing_id,
    )
    if listing is None:
        raise HTTPException(404, "listing not found")
    history = await db.fetch(
        "SELECT price, currency, in_stock, extraction_tier, fetch_tier, below_map, observed_at "
        "FROM price_observations WHERE listing_id=$1 ORDER BY observed_at DESC LIMIT 200",
        listing_id,
    )
    viols = await db.fetch(
        "SELECT * FROM violations WHERE listing_id=$1 ORDER BY opened_at DESC", listing_id
    )
    ev = await db.fetch(
        "SELECT * FROM evidence WHERE listing_id=$1 ORDER BY captured_at DESC LIMIT 50",
        listing_id,
    )
    return templates.TemplateResponse(
        request, "listing.html",
        ctx(request, user, listing=listing, history=history, violations=viols,
            evidence=ev),
    )


# ---------------------------------------------------------------------------
# removal
#
# Everything the scanner finds used to be permanent. These give a way back:
# untrack a single listing, purge a dealer, or exclude a domain outright.
# ---------------------------------------------------------------------------
async def _delete_evidence_files(listing_id: int) -> int:
    """Remove evidence artifacts from disk before their rows cascade away."""
    rows = await db.fetch(
        "SELECT html_path, screenshot_path FROM evidence WHERE listing_id = $1",
        listing_id,
    )
    removed = 0
    root = settings.evidence_dir.resolve()
    for r in rows:
        for rel in (r["html_path"], r["screenshot_path"]):
            if not rel:
                continue
            target = (settings.evidence_dir / rel).resolve()
            # Paths come from the database, but a traversal here would delete
            # arbitrary files -- confine to the evidence root.
            if not str(target).startswith(str(root)):
                log.warning("refusing to delete outside evidence root: %s", rel)
                continue
            try:
                target.unlink(missing_ok=True)
                removed += 1
            except OSError as exc:
                log.error("could not delete evidence file %s: %s", rel, exc)
    return removed


@app.post("/listings/{listing_id}/delete")
async def listing_delete(request: Request, listing_id: int,
                         csrf_token: str = Form(...)):
    """Stop tracking one URL and remove everything derived from it."""
    user = await require_user(request)
    auth.check_csrf(request, csrf_token)
    row = await db.fetchrow(
        "SELECT l.url, p.sku FROM listings l JOIN products p ON p.id = l.product_id "
        "WHERE l.id = $1", listing_id)
    if row is None:
        raise HTTPException(404, "listing not found")

    files = await _delete_evidence_files(listing_id)
    # price_observations, violations and evidence all cascade from listings.
    await db.execute("DELETE FROM listings WHERE id = $1", listing_id)
    await auth.record_event(
        "listing_deleted", user["email"].lower(), user["id"],
        auth.client_ip(request), f"{row['sku']} {row['url']} ({files} file(s))")
    from urllib.parse import quote
    msg = f"Removed the {row['sku']} listing and its price history."
    return RedirectResponse(f"/?notice={quote(msg)}", status_code=303)


@app.post("/listings/{listing_id}/untrack")
async def listing_untrack(request: Request, listing_id: int,
                          csrf_token: str = Form(...)):
    """Keep the row and its history, but stop checking it.

    Preferred over deletion when a listing was real: the price history and any
    resolved violations stay available as a record.
    """
    user = await require_user(request)
    auth.check_csrf(request, csrf_token)
    updated = await db.fetchval(
        "UPDATE listings SET status = 'rejected', retired_at = now(), "
        "reviewed_at = now(), reviewed_note = 'untracked by ' || $2 "
        "WHERE id = $1 RETURNING id",
        listing_id, user["email"])
    if updated is None:
        raise HTTPException(404, "listing not found")
    # An open violation on something we no longer track would sit there forever.
    await db.execute(
        "UPDATE violations SET status = 'resolved', resolved_at = now(), "
        "note = coalesce(note,'') || ' [listing untracked]' "
        "WHERE listing_id = $1 AND status = 'open'", listing_id)
    await auth.record_event("listing_untracked", user["email"].lower(), user["id"],
                            auth.client_ip(request), str(listing_id))
    return RedirectResponse(f"/listings/{listing_id}", status_code=303)


@app.post("/listings/{listing_id}/track")
async def listing_track(request: Request, listing_id: int,
                        csrf_token: str = Form(...)):
    """Resume tracking a listing that was untracked.

    The missing inverse of /untrack. Untracking deliberately survives a rescan --
    a scan re-finding a URL must not silently undo a decision to stop watching it
    -- which means without this there is no way back short of editing the
    database.
    """
    user = await require_user(request)
    auth.check_csrf(request, csrf_token)
    updated = await db.fetchval(
        """
        UPDATE listings
           SET status = 'confirmed', retired_at = NULL,
               consecutive_failures = 0, below_map_streak = 0,
               next_check_at = now(),
               reviewed_at = now(),
               reviewed_note = 'tracking resumed by ' || $2
         WHERE id = $1
        RETURNING id
        """,
        listing_id, user["email"])
    if updated is None:
        raise HTTPException(404, "listing not found")
    await auth.record_event("listing_tracked", user["email"].lower(), user["id"],
                            auth.client_ip(request), str(listing_id))
    return RedirectResponse(f"/listings/{listing_id}", status_code=303)


@app.get("/sellers", response_class=HTMLResponse)
async def sellers_page(request: Request, notice: str = ""):
    user = await require_user(request)
    rows = await db.fetch(
        """
        SELECT s.*,
               count(l.id)                                            AS listings,
               count(l.id) FILTER (WHERE l.status = 'confirmed')      AS confirmed,
               count(v.id) FILTER (WHERE v.status = 'open')           AS open_violations,
               max(l.last_checked)                                    AS last_checked
          FROM sellers s
          LEFT JOIN listings l   ON l.seller_id = s.id
          LEFT JOIN violations v ON v.listing_id = l.id
         GROUP BY s.id
         ORDER BY count(v.id) FILTER (WHERE v.status = 'open') DESC, s.domain
        """
    )
    return templates.TemplateResponse(
        request, "sellers.html", ctx(request, user, rows=rows, notice=notice))


@app.post("/sellers/{seller_id}/exclude")
async def seller_exclude(request: Request, seller_id: int,
                         csrf_token: str = Form(...), reason: str = Form(""),
                         purge: str = Form("")):
    """Never scan or match this domain again.

    A flag rather than a delete: deleting the seller row would simply let the
    next scan re-create it.
    """
    user = await require_admin(request)
    auth.check_csrf(request, csrf_token)
    row = await db.fetchrow("SELECT domain FROM sellers WHERE id = $1", seller_id)
    if row is None:
        raise HTTPException(404, "seller not found")

    purged = 0
    if purge:
        ids = await db.fetch("SELECT id FROM listings WHERE seller_id = $1", seller_id)
        for r in ids:
            await _delete_evidence_files(r["id"])
        purged = len(ids)
        await db.execute("DELETE FROM listings WHERE seller_id = $1", seller_id)

    await db.execute(
        "UPDATE sellers SET excluded = true, excluded_at = now(), "
        "excluded_reason = NULLIF($2,'') WHERE id = $1",
        seller_id, reason)
    await auth.record_event("seller_excluded", user["email"].lower(), user["id"],
                            auth.client_ip(request),
                            f"{row['domain']} purged={purged}")
    from urllib.parse import quote
    msg = f"{row['domain']} excluded from future scans."
    if purged:
        msg += f" {purged} listing(s) removed."
    return RedirectResponse(f"/sellers?notice={quote(msg)}", status_code=303)


@app.post("/sellers/{seller_id}/include")
async def seller_include(request: Request, seller_id: int,
                         csrf_token: str = Form(...)):
    user = await require_admin(request)
    auth.check_csrf(request, csrf_token)
    await db.execute(
        "UPDATE sellers SET excluded = false, excluded_at = NULL, "
        "excluded_reason = NULL WHERE id = $1", seller_id)
    await auth.record_event("seller_included", user["email"].lower(), user["id"],
                            auth.client_ip(request), str(seller_id))
    from urllib.parse import quote
    return RedirectResponse(
        f"/sellers?notice={quote('Domain re-enabled for scanning.')}", status_code=303)


@app.post("/sellers/{seller_id}/purge")
async def seller_purge(request: Request, seller_id: int, csrf_token: str = Form(...)):
    """Drop everything found on this dealer without excluding the domain."""
    user = await require_admin(request)
    auth.check_csrf(request, csrf_token)
    row = await db.fetchrow("SELECT domain FROM sellers WHERE id = $1", seller_id)
    if row is None:
        raise HTTPException(404, "seller not found")
    ids = await db.fetch("SELECT id FROM listings WHERE seller_id = $1", seller_id)
    for r in ids:
        await _delete_evidence_files(r["id"])
    await db.execute("DELETE FROM listings WHERE seller_id = $1", seller_id)
    await auth.record_event("seller_purged", user["email"].lower(), user["id"],
                            auth.client_ip(request), f"{row['domain']} {len(ids)}")
    from urllib.parse import quote
    msg = f"Removed {len(ids)} listing(s) from {row['domain']}."
    return RedirectResponse(f"/sellers?notice={quote(msg)}", status_code=303)


@app.post("/jobs/{job_id}/delete")
async def job_delete(request: Request, job_id: int, csrf_token: str = Form(...)):
    """Remove a scan from the history list. Does not touch what it found."""
    user = await require_user(request)
    auth.check_csrf(request, csrf_token)
    await db.execute("DELETE FROM crawl_jobs WHERE id = $1 AND status <> 'running'",
                     job_id)
    return RedirectResponse("/scan", status_code=303)


# ---------------------------------------------------------------------------
# products
# ---------------------------------------------------------------------------
@app.get("/products", response_class=HTMLResponse)
async def products_page(request: Request, user: dict = Depends(require_user)):
    rows = await db.fetch(
        """
        SELECT p.*,
               (SELECT count(*) FROM listings l
                 WHERE l.product_id=p.id AND l.status='confirmed')  AS confirmed,
               (SELECT count(*) FROM listings l
                 WHERE l.product_id=p.id AND l.status='candidate')  AS review,
               (SELECT count(*) FROM violations v
                  JOIN listings l2 ON l2.id=v.listing_id
                 WHERE l2.product_id=p.id AND v.status='open')      AS open_violations
          FROM products p
         ORDER BY p.sku
        """
    )
    return templates.TemplateResponse(
        request, "products.html", ctx(request, user, rows=rows)
    )


# ---------------------------------------------------------------------------
# scan: paste a dealer URL
# ---------------------------------------------------------------------------
@app.get("/scan", response_class=HTMLResponse)
async def scan_form(request: Request, user: dict = Depends(require_user)):
    jobs = await db.fetch(
        "SELECT id, kind, target, status, progress, created_at, finished_at, error "
        "FROM crawl_jobs ORDER BY created_at DESC LIMIT 30"
    )
    product_count = await db.fetchval("SELECT count(*) FROM products WHERE active")
    return templates.TemplateResponse(
        request, "scan.html",
        ctx(request, user, jobs=jobs, product_count=product_count)
    )


@app.post("/scan")
async def scan_submit(url: str = Form(...), user: dict = Depends(require_user)):
    target = url.strip()
    if not target:
        raise HTTPException(400, "url required")
    job_row_id = await db.fetchval(
        "INSERT INTO crawl_jobs (kind, target, status) VALUES ('domain_scan',$1,'queued') "
        "RETURNING id",
        target,
    )
    job = await arq().enqueue_job("domain_scan", job_row_id, target)
    await db.execute("UPDATE crawl_jobs SET arq_job_id=$2 WHERE id=$1",
                     job_row_id, job.job_id if job else None)
    return JSONResponse({"ok": True, "job_id": job_row_id})


@app.post("/scan/recheck")
async def trigger_recheck(user: dict = Depends(require_user)):
    job_row_id = await db.fetchval(
        "INSERT INTO crawl_jobs (kind, target, status) "
        "VALUES ('recheck','manual trigger','queued') RETURNING id"
    )
    job = await arq().enqueue_job("recheck_all", job_row_id)
    await db.execute("UPDATE crawl_jobs SET arq_job_id=$2 WHERE id=$1",
                     job_row_id, job.job_id if job else None)
    return JSONResponse({"ok": True, "job_id": job_row_id})


@app.post("/scan/ebay")
async def trigger_ebay(user: dict = Depends(require_user)):
    job_row_id = await db.fetchval(
        "INSERT INTO crawl_jobs (kind, target, status) "
        "VALUES ('ebay_sweep','manual trigger','queued') RETURNING id")
    job = await arq().enqueue_job("ebay_sweep", job_row_id)
    await db.execute("UPDATE crawl_jobs SET arq_job_id=$2 WHERE id=$1",
                     job_row_id, job.job_id if job else None)
    return JSONResponse({"ok": True, "job_id": job_row_id})


@app.post("/settings/test-ebay")
async def settings_test_ebay(request: Request, csrf_token: str = Form(...)):
    """Mint a token and run one search, proving the credentials actually work."""
    await require_admin(request)
    auth.check_csrf(request, csrf_token)
    from worker import ebay
    from urllib.parse import quote
    try:
        ok, detail = await ebay.verify_credentials()
    except Exception as exc:
        log.exception("ebay credential test failed")
        ok, detail = False, f"{type(exc).__name__}: {exc}"
    key = "notice" if ok else "error"
    return RedirectResponse(f"/settings?{key}={quote(detail)}", status_code=303)


@app.post("/scan/discovery")
async def trigger_discovery(user: dict = Depends(require_user)):
    job_row_id = await db.fetchval(
        "INSERT INTO crawl_jobs (kind, target, status) "
        "VALUES ('discovery_sweep','manual trigger','queued') RETURNING id"
    )
    job = await arq().enqueue_job("discovery_sweep", job_row_id)
    await db.execute("UPDATE crawl_jobs SET arq_job_id=$2 WHERE id=$1",
                     job_row_id, job.job_id if job else None)
    return JSONResponse({"ok": True, "job_id": job_row_id})


@app.get("/jobs/{job_id}/progress")
async def job_progress(job_id: int, user: dict = Depends(require_user)):
    row = await db.fetchrow(
        "SELECT id, kind, target, status, progress, created_at, started_at, "
        "finished_at, error FROM crawl_jobs WHERE id=$1",
        job_id,
    )
    if row is None:
        raise HTTPException(404, "job not found")
    d = dict(row)
    for k in ("created_at", "started_at", "finished_at"):
        d[k] = d[k].isoformat() if d[k] else None
    if isinstance(d.get("progress"), str):
        d["progress"] = json.loads(d["progress"])
    return JSONResponse(d)


# ---------------------------------------------------------------------------
# eBay Marketplace Account Deletion endpoint
#
# THE ONLY UNAUTHENTICATED ROUTES IN THIS APP. eBay's servers call them, so
# they cannot sit behind the session cookie. Both are deliberately narrow:
# they read no user input beyond a challenge code and a JSON body, they never
# echo the verification token, and neither can reach anything but the two
# tables involved.
# ---------------------------------------------------------------------------
@app.get(ebay_notify.PATH)
async def ebay_deletion_challenge(challenge_code: str = ""):
    """Answer eBay's ownership challenge.

    Returns 503 rather than a wrong digest when the token is missing: a
    plausible-looking but incorrect challengeResponse is harder to diagnose
    from eBay's side than an outright failure.
    """
    if not challenge_code:
        raise HTTPException(400, "challenge_code is required")

    token = await settings_store.get("ebay_verification_token", "") or ""
    problem = ebay_notify.token_problem(token)
    if problem:
        log.error("eBay deletion challenge received but the verification token %s", problem)
        raise HTTPException(503, "verification token is not configured")

    endpoint = await ebay_notify.endpoint_url()
    digest = ebay_notify.challenge_response(challenge_code, token, endpoint)
    # The endpoint string is the usual culprit when eBay rejects the response,
    # and it is not a secret -- log it so a mismatch is one grep away.
    log.info("eBay deletion challenge answered (endpoint used: %s)", endpoint)
    return JSONResponse({"challengeResponse": digest})


@app.post(ebay_notify.PATH)
async def ebay_deletion_notify(request: Request):
    """Accept an account-deletion notice.

    Always 200 once the body parses. eBay retries non-2xx responses and
    disables a keyset whose endpoint keeps failing, so a fault on our side
    must not be reported by failing their delivery -- it is logged and dealt
    with here instead.
    """
    try:
        payload = await request.json()
    except Exception:
        log.warning("eBay deletion notice had an unparseable body")
        raise HTTPException(400, "expected a JSON body")

    if not isinstance(payload, dict):
        raise HTTPException(400, "expected a JSON object")

    try:
        result = await ebay_notify.handle_deletion(payload)
        log.info("eBay deletion notice processed: %s", result)
    except Exception:
        # Logged loudly, but still acknowledged -- see the docstring.
        log.exception("failed to process an eBay deletion notice; ACKing anyway "
                      "so eBay does not disable the keyset. Handle manually.")
    return Response(status_code=200)


# ---------------------------------------------------------------------------
# evidence
# ---------------------------------------------------------------------------
# Screenshots are not always PNG: Zyte returns JPEG for full-page captures and
# offers no PNG option for them, while ScrapingBee returns PNG. Declaring the
# wrong type left browsers to sniff their way to the right answer, which mostly
# worked and was still wrong -- evidence should not need to be guessed at.
_IMAGE_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
}


@app.get("/evidence/{evidence_id}/screenshot")
async def evidence_screenshot(evidence_id: int, user: dict = Depends(require_user)):
    row = await db.fetchrow("SELECT screenshot_path FROM evidence WHERE id=$1", evidence_id)
    if row is None or not row["screenshot_path"]:
        raise HTTPException(404, "no screenshot for this evidence row")
    target = (settings.evidence_dir / row["screenshot_path"]).resolve()
    if not str(target).startswith(str(settings.evidence_dir.resolve())) or not target.exists():
        raise HTTPException(404, "screenshot missing")
    media_type = _IMAGE_TYPES.get(target.suffix.lower(), "application/octet-stream")
    return Response(target.read_bytes(), media_type=media_type)


@app.get("/evidence/{evidence_id}/html", response_class=PlainTextResponse)
async def evidence_html(evidence_id: int, user: dict = Depends(require_user)):
    from worker import evidence as ev_mod
    row = await db.fetchrow("SELECT html_path FROM evidence WHERE id=$1", evidence_id)
    if row is None or not row["html_path"]:
        raise HTTPException(404, "no html snapshot for this evidence row")
    body = ev_mod.read_html(row["html_path"])
    if body is None:
        raise HTTPException(404, "snapshot missing")
    # Served as text/plain deliberately: this is an archived third-party page and
    # must never be rendered as live HTML in the reviewer's browser.
    return PlainTextResponse(body)


# ---------------------------------------------------------------------------
# misc
# ---------------------------------------------------------------------------
@app.get("/about", response_class=HTMLResponse)
async def about(request: Request):
    """Referenced by the crawler User-Agent so site owners can identify us."""
    return templates.TemplateResponse(
        request, "about.html", ctx(request, await auth.load_user(request),
                                   settings=settings)
    )


@app.get("/healthz", response_class=PlainTextResponse)
async def healthz():
    try:
        await db.fetchval("SELECT 1")
    except Exception as exc:
        raise HTTPException(503, f"db unavailable: {exc}")
    return "ok"
