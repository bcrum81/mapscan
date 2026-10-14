-- ===========================================================================
-- mapscan  —  MAP violation tracking
-- Target: PostgreSQL 15+ (developed on 17).
--
-- Bootstrap (as postgres):
--   CREATE ROLE mapscan LOGIN PASSWORD '...';
--   CREATE DATABASE mapscan OWNER mapscan;
--   \c mapscan
--   CREATE EXTENSION IF NOT EXISTS pg_trgm;
--   \i schema.sql
-- ===========================================================================

CREATE EXTENSION IF NOT EXISTS pg_trgm;

-- --------------------------------------------------------------------------
-- products: your catalog, loaded from the spreadsheet. ~200-300 rows.
-- --------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS products (
    id           bigserial PRIMARY KEY,
    sku          text        NOT NULL UNIQUE,
    title        text        NOT NULL,
    upc          text,                          -- normalized to 13 digits (GTIN-13)
    msrp         numeric(10,2),
    map_price    numeric(10,2) NOT NULL,
    description  text,
    active       boolean     NOT NULL DEFAULT true,
    created_at   timestamptz NOT NULL DEFAULT now(),
    updated_at   timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT map_price_positive CHECK (map_price > 0),
    -- A MAP above MSRP is nearly always a spreadsheet transposition. Catching
    -- it here beats generating phantom violations forever.
    CONSTRAINT map_not_above_msrp CHECK (msrp IS NULL OR map_price <= msrp)
);
CREATE INDEX IF NOT EXISTS products_upc_idx        ON products (upc) WHERE upc IS NOT NULL;
CREATE INDEX IF NOT EXISTS products_title_trgm_idx ON products USING gin (title gin_trgm_ops);
CREATE INDEX IF NOT EXISTS products_active_idx     ON products (active) WHERE active;

-- --------------------------------------------------------------------------
-- sellers: one row per domain we have ever seen. `fetch_tier` is learned, not
-- configured -- the fetcher promotes a domain to tier 2 after it blocks us,
-- so we never pay a vendor for a request tier 1 could have served.
-- --------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS sellers (
    id                  bigserial PRIMARY KEY,
    domain              text        NOT NULL UNIQUE,   -- registrable host, lowercased
    name                text,
    is_authorized       boolean     NOT NULL DEFAULT false,
    fetch_tier          smallint    NOT NULL DEFAULT 1, -- 1=direct httpx, 2=vendor
    tier1_failures      integer     NOT NULL DEFAULT 0,
    robots_crawl_delay  numeric(6,2),
    robots_checked_at   timestamptz,
    robots_disallowed   boolean     NOT NULL DEFAULT false,
    platform            text,                           -- shopify|woocommerce|bigcommerce|unknown
    notes               text,
    created_at          timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT fetch_tier_valid CHECK (fetch_tier IN (1, 2))
);

-- --------------------------------------------------------------------------
-- listings: a (product, seller, url) candidate. Lives through a review
-- lifecycle: candidate -> confirmed | rejected. Only `confirmed` rows are
-- price-checked nightly, so review quality directly controls crawl cost.
-- --------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS listings (
    id             bigserial PRIMARY KEY,
    product_id     bigint      NOT NULL REFERENCES products (id) ON DELETE CASCADE,
    seller_id      bigint      NOT NULL REFERENCES sellers  (id) ON DELETE CASCADE,
    url            text        NOT NULL,
    status         text        NOT NULL DEFAULT 'candidate',
    match_score    numeric(5,2),
    match_signals  jsonb       NOT NULL DEFAULT '{}'::jsonb,
    title_seen     text,
    sku_seen       text,
    upc_seen       text,
    discovered_via text,                        -- brave|sitemap|manual
    first_seen     timestamptz NOT NULL DEFAULT now(),
    last_checked   timestamptz,
    last_error     text,
    consecutive_failures integer NOT NULL DEFAULT 0,
    reviewed_at    timestamptz,
    reviewed_note  text,
    UNIQUE (product_id, url),
    CONSTRAINT listing_status_valid CHECK (status IN ('candidate','confirmed','rejected'))
);
CREATE INDEX IF NOT EXISTS listings_status_idx    ON listings (status);
CREATE INDEX IF NOT EXISTS listings_recheck_idx   ON listings (last_checked NULLS FIRST) WHERE status = 'confirmed';
CREATE INDEX IF NOT EXISTS listings_seller_idx    ON listings (seller_id);
CREATE INDEX IF NOT EXISTS listings_product_idx   ON listings (product_id);

-- --------------------------------------------------------------------------
-- price_observations: the time-series, and the table that actually grows.
-- We insert only on CHANGE (or once daily as a heartbeat), not on every
-- crawl -- see worker/tasks.py. Retention job prunes below.
-- --------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS price_observations (
    id              bigserial PRIMARY KEY,
    listing_id      bigint      NOT NULL REFERENCES listings (id) ON DELETE CASCADE,
    price           numeric(10,2),
    currency        text        NOT NULL DEFAULT 'USD',
    in_stock        boolean,
    extraction_tier text        NOT NULL,       -- jsonld|platform_json|selector|llm|og
    fetch_tier      smallint,
    below_map       boolean,
    observed_at     timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS price_obs_listing_time_idx ON price_observations (listing_id, observed_at DESC);
CREATE INDEX IF NOT EXISTS price_obs_time_idx         ON price_observations (observed_at);

-- --------------------------------------------------------------------------
-- violations: open/resolve state machine. One open violation per listing.
-- --------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS violations (
    id              bigserial PRIMARY KEY,
    listing_id      bigint      NOT NULL REFERENCES listings (id) ON DELETE CASCADE,
    status          text        NOT NULL DEFAULT 'open',
    map_at_open     numeric(10,2) NOT NULL,
    opening_price   numeric(10,2) NOT NULL,
    lowest_price    numeric(10,2) NOT NULL,
    worst_delta_pct numeric(6,2),
    opened_at       timestamptz NOT NULL DEFAULT now(),
    resolved_at     timestamptz,
    resolved_price  numeric(10,2),
    acknowledged_at timestamptz,
    note            text,
    CONSTRAINT violation_status_valid CHECK (status IN ('open','resolved'))
);
-- At most one OPEN violation per listing; resolved history is unconstrained.
CREATE UNIQUE INDEX IF NOT EXISTS violations_one_open_per_listing
    ON violations (listing_id) WHERE status = 'open';
CREATE INDEX IF NOT EXISTS violations_status_idx ON violations (status, opened_at DESC);

-- --------------------------------------------------------------------------
-- evidence: captured when a violation OPENS, not on every check. That is what
-- keeps screenshot cost negligible. Files live on disk; rows point at them.
-- --------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS evidence (
    id              bigserial PRIMARY KEY,
    violation_id    bigint      REFERENCES violations (id) ON DELETE SET NULL,
    listing_id      bigint      NOT NULL REFERENCES listings (id) ON DELETE CASCADE,
    url             text        NOT NULL,
    observed_price  numeric(10,2),
    map_price       numeric(10,2),
    screenshot_path text,                       -- relative to EVIDENCE_DIR
    html_path       text,                       -- gzipped
    sha256_html     text,
    sha256_screenshot text,
    source_tier     text,
    captured_at     timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS evidence_violation_idx ON evidence (violation_id);
CREATE INDEX IF NOT EXISTS evidence_listing_idx   ON evidence (listing_id, captured_at DESC);

-- --------------------------------------------------------------------------
-- crawl_jobs: durable source of truth for the dashboard. arq is only the
-- execution queue -- if Redis is flushed, history survives here.
-- --------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS crawl_jobs (
    id           bigserial PRIMARY KEY,
    kind         text        NOT NULL,          -- domain_scan|discovery_sweep|recheck
    target       text,                          -- domain or descriptor
    status       text        NOT NULL DEFAULT 'queued',
    progress     jsonb       NOT NULL DEFAULT '{}'::jsonb,
    arq_job_id   text,
    created_at   timestamptz NOT NULL DEFAULT now(),
    started_at   timestamptz,
    finished_at  timestamptz,
    error        text,
    CONSTRAINT job_kind_valid   CHECK (kind IN ('domain_scan','discovery_sweep','recheck','ebay_sweep')),
    -- 'inconclusive': the job completed but established nothing, e.g. an eBay
    -- sweep run against the sandbox, which holds no real listings.
    CONSTRAINT job_status_valid CHECK (status IN ('queued','running','done','failed','cancelled','inconclusive'))
);
CREATE INDEX IF NOT EXISTS crawl_jobs_recent_idx ON crawl_jobs (created_at DESC);
CREATE INDEX IF NOT EXISTS crawl_jobs_status_idx ON crawl_jobs (status);

-- --------------------------------------------------------------------------
-- ebay_deletion_requests: proof that an eBay account-deletion notice was
-- received and acted on. Holds no username by design -- see migration 011.
-- --------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS ebay_deletion_requests (
    id                  bigserial PRIMARY KEY,
    notification_id     text UNIQUE,
    username_sha256     text,
    listings_anonymized integer     NOT NULL DEFAULT 0,
    received_at         timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ebay_deletion_received_idx
    ON ebay_deletion_requests (received_at DESC);

-- --------------------------------------------------------------------------
-- extraction_rules: per-domain CSS selectors, stored as DATA not code, so a
-- broken dealer site is a dashboard edit rather than a deploy.
-- --------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS extraction_rules (
    id             bigserial PRIMARY KEY,
    domain         text        NOT NULL UNIQUE,
    price_selector text,
    stock_selector text,
    currency       text        NOT NULL DEFAULT 'USD',
    notes          text,
    created_at     timestamptz NOT NULL DEFAULT now(),
    updated_at     timestamptz NOT NULL DEFAULT now()
);

-- --------------------------------------------------------------------------
-- search_budget: enforces the Brave Search monthly cap. The staggered sweep
-- reads this before every query. Kept provider-keyed so a second search
-- provider can be added without a migration.
-- --------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS search_budget (
    provider   text        NOT NULL,            -- brave
    period     date        NOT NULL,            -- month-start (Brave bills monthly)
    used       integer     NOT NULL DEFAULT 0,
    PRIMARY KEY (provider, period)
);

-- --------------------------------------------------------------------------
-- discovery_cursor: round-robin pointer so the sweep covers the whole catalog
-- across days instead of always re-querying the first N products.
-- --------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS discovery_cursor (
    product_id   bigint      PRIMARY KEY REFERENCES products (id) ON DELETE CASCADE,
    last_upc_at  timestamptz,
    last_title_at timestamptz
);

-- ===========================================================================
-- Authentication
--
-- Auth lives in the application, not in nginx basic-auth, because we need
-- forced password rotation, self-service invites, and per-user attribution.
-- Consequence: this app is directly internet-facing, so the login path is
-- rate-limited (auth_events) and every token is stored hashed.
-- ===========================================================================

CREATE TABLE IF NOT EXISTS users (
    id                   bigserial PRIMARY KEY,
    email                text        NOT NULL,
    email_lower          text        NOT NULL UNIQUE,   -- case-insensitive identity
    password_hash        text,                          -- NULL until an invite is accepted
    is_admin             boolean     NOT NULL DEFAULT false,
    is_active            boolean     NOT NULL DEFAULT true,
    -- Set on the bootstrap admin and on any admin-forced reset. Every request
    -- redirects to the change-password page while this is true.
    must_change_password boolean     NOT NULL DEFAULT false,
    last_login_at        timestamptz,
    password_changed_at  timestamptz,
    created_by           bigint      REFERENCES users (id) ON DELETE SET NULL,
    created_at           timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT email_has_at CHECK (position('@' in email) > 1)
);
CREATE INDEX IF NOT EXISTS users_active_idx ON users (is_active) WHERE is_active;

-- Invite + password-reset tokens. The raw token is shown/emailed exactly once
-- and never stored: only its SHA-256 lands here, so a database dump does not
-- hand over live account access.
CREATE TABLE IF NOT EXISTS auth_tokens (
    id          bigserial PRIMARY KEY,
    user_id     bigint      NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    token_hash  text        NOT NULL UNIQUE,
    purpose     text        NOT NULL,          -- invite | reset
    expires_at  timestamptz NOT NULL,
    used_at     timestamptz,
    created_by  bigint      REFERENCES users (id) ON DELETE SET NULL,
    created_at  timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT token_purpose_valid CHECK (purpose IN ('invite','reset'))
);
CREATE INDEX IF NOT EXISTS auth_tokens_user_idx ON auth_tokens (user_id);
CREATE INDEX IF NOT EXISTS auth_tokens_live_idx ON auth_tokens (expires_at)
    WHERE used_at IS NULL;

-- Login attempt log. Doubles as the rate-limit source and the audit trail;
-- an internet-facing login form without this is an open credential-stuffing
-- target.
CREATE TABLE IF NOT EXISTS auth_events (
    id          bigserial PRIMARY KEY,
    email_lower text,
    user_id     bigint      REFERENCES users (id) ON DELETE SET NULL,
    event       text        NOT NULL,   -- login_ok|login_fail|invite_sent|
                                        -- invite_accepted|password_changed|
                                        -- user_deactivated|user_reactivated
    ip          inet,
    detail      text,
    created_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS auth_events_rate_idx ON auth_events (email_lower, created_at DESC)
    WHERE event = 'login_fail';
CREATE INDEX IF NOT EXISTS auth_events_ip_idx   ON auth_events (ip, created_at DESC)
    WHERE event = 'login_fail';
CREATE INDEX IF NOT EXISTS auth_events_recent_idx ON auth_events (created_at DESC);

-- --------------------------------------------------------------------------
-- Convenience view: current price per confirmed listing.
-- --------------------------------------------------------------------------
CREATE OR REPLACE VIEW listing_current AS
SELECT DISTINCT ON (l.id)
       l.id            AS listing_id,
       l.product_id,
       l.seller_id,
       l.url,
       l.status,
       p.sku,
       p.title         AS product_title,
       p.map_price,
       s.domain,
       s.is_authorized,
       o.price         AS current_price,
       o.in_stock,
       o.observed_at   AS price_observed_at,
       o.extraction_tier,
       (o.price IS NOT NULL AND o.price < p.map_price) AS below_map
FROM listings l
JOIN products p ON p.id = l.product_id
JOIN sellers  s ON s.id = l.seller_id
LEFT JOIN price_observations o ON o.listing_id = l.id
ORDER BY l.id, o.observed_at DESC NULLS LAST;
