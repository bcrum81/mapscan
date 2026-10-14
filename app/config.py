"""Configuration, loaded from the environment (see .env.example)."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()


def _bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _int(name: str, default: int) -> int:
    raw = os.getenv(name)
    try:
        return int(raw) if raw not in (None, "") else default
    except ValueError:
        return default


def _float(name: str, default: float) -> float:
    raw = os.getenv(name)
    try:
        return float(raw) if raw not in (None, "") else default
    except ValueError:
        return default


@dataclass(frozen=True)
class Settings:
    database_url: str = os.getenv(
        "DATABASE_URL", "postgresql://mapscan@localhost:5432/mapscan"
    )
    redis_url: str = os.getenv("REDIS_URL", "redis://localhost:6379/0")

    # Used to build invite links, so it must match how users reach the app.
    public_base_url: str = os.getenv(
        "PUBLIC_BASE_URL", "http://localhost:8013"
    )
    # Signs the session cookie. Rotating it logs everyone out.
    secret_key: str = os.getenv("SECRET_KEY", "")
    session_max_age: int = _int("SESSION_MAX_AGE_SECONDS", 60 * 60 * 12)
    cookie_secure: bool = _bool("COOKIE_SECURE", True)

    # email transport for invites / resets: manual | smtp | resend
    email_provider: str = os.getenv("EMAIL_PROVIDER", "manual").strip().lower()
    email_from: str = os.getenv("EMAIL_FROM", "")
    resend_key: str = os.getenv("RESEND_KEY", "")
    smtp_host: str = os.getenv("SMTP_HOST", "")
    smtp_port: int = _int("SMTP_PORT", 587)
    smtp_user: str = os.getenv("SMTP_USER", "")
    smtp_password: str = os.getenv("SMTP_PASSWORD", "")

    # Whose products are being protected. Shown in the UI and on /about, and
    # BRAND_NAME's first word is the default slug brand token (see BRAND_TOKENS).
    brand_name: str = os.getenv("BRAND_NAME", "Your Brand")
    contact_email: str = os.getenv("CONTACT_EMAIL", "")
    # Comma-separated domains that are never flagged (your own storefronts).
    own_domains: tuple = tuple(
        d.strip().lower() for d in os.getenv("OWN_DOMAINS", "").split(",") if d.strip()
    )

    bind_host: str = os.getenv("BIND_HOST", "127.0.0.1")
    bind_port: int = _int("BIND_PORT", 8013)
    evidence_dir: Path = field(
        default_factory=lambda: Path(os.getenv("EVIDENCE_DIR", "./evidence")).resolve()
    )
    log_level: str = os.getenv("LOG_LEVEL", "INFO").upper()

    # discovery -- Brave Search is the web-search provider
    brave_search_key: str = os.getenv("BRAVE_SEARCH_KEY", "")
    brave_monthly_budget: int = _int("BRAVE_MONTHLY_BUDGET", 2000)
    # Queries per sweep run. Keep sweeps under the monthly cap: 300 SKUs x 2
    # queries = 600 per full pass, so a 2000/mo tier affords ~3 full sweeps.
    discovery_queries_per_run: int = _int("DISCOVERY_QUERIES_PER_RUN", 150)

    # fetching
    user_agent: str = os.getenv(
        "USER_AGENT",
        "MAPScan/1.0 (+https://mapscan.example.com/about)",
    )
    per_domain_delay: float = _float("PER_DOMAIN_DELAY_SECONDS", 3.0)
    max_concurrent_fetches: int = _int("MAX_CONCURRENT_FETCHES", 8)
    fetch_timeout: int = _int("FETCH_TIMEOUT_SECONDS", 30)
    respect_robots: bool = _bool("RESPECT_ROBOTS", True)

    fetch_vendor: str = os.getenv("FETCH_VENDOR", "").strip().lower()
    scrapingbee_key: str = os.getenv("SCRAPINGBEE_KEY", "")
    zyte_key: str = os.getenv("ZYTE_KEY", "")

    # LLM extraction fallback (OpenAI). The key and model are resolved through
    # settings_store at call time -- see worker/extract.py:_llm_config -- so
    # these are env-only fallbacks, not the source of truth.
    openai_api_key: str = os.getenv("OPENAI_API_KEY", "")
    extraction_model: str = os.getenv("EXTRACTION_MODEL", "gpt-4o-mini")
    llm_extraction_enabled: bool = _bool("LLM_EXTRACTION_ENABLED", True)
    llm_max_input_chars: int = _int("LLM_MAX_INPUT_CHARS", 24_000)

    # matching
    match_auto_confirm: float = _float("MATCH_AUTO_CONFIRM", 85.0)
    match_auto_reject: float = _float("MATCH_AUTO_REJECT", 45.0)

    # violations
    violation_consecutive_checks: int = _int("VIOLATION_CONSECUTIVE_CHECKS", 2)
    violation_min_delta_pct: float = _float("VIOLATION_MIN_DELTA_PCT", 0.5)

    @property
    def vendor_enabled(self) -> bool:
        if self.fetch_vendor == "scrapingbee":
            return bool(self.scrapingbee_key)
        if self.fetch_vendor == "zyte":
            return bool(self.zyte_key)
        return False

    @property
    def llm_enabled(self) -> bool:
        """Env-only view. Prefer `await extract.llm_enabled()`, which also sees
        a key entered through the Settings page."""
        return self.llm_extraction_enabled and bool(self.openai_api_key)

    @property
    def email_enabled(self) -> bool:
        if self.email_provider == "resend":
            return bool(self.resend_key and self.email_from)
        if self.email_provider == "smtp":
            return bool(self.smtp_host and self.email_from)
        return False


settings = Settings()
