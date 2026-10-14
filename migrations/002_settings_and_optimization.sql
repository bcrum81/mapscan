-- ===========================================================================
-- 002: encrypted settings store + search/crawl usage optimization
--
-- Applied on top of schema.sql. Idempotent.
-- ===========================================================================

-- --------------------------------------------------------------------------
-- app_settings: runtime configuration set through the admin UI rather than a
-- file. Secret values are Fernet-encrypted with SETTINGS_KEY, so an API key
-- entered in the browser never lands in .env, in the repo, or in a chat
-- transcript. Both the API and the worker read from here.
-- --------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS app_settings (
    key         text        PRIMARY KEY,
    value_enc   bytea,                              -- Fernet token, or NULL
    is_secret   boolean     NOT NULL DEFAULT true,
    updated_by  bigint      REFERENCES users (id) ON DELETE SET NULL,
    updated_at  timestamptz NOT NULL DEFAULT now()
);

-- Bumped on every write so the worker notices a change without a restart.
CREATE TABLE IF NOT EXISTS settings_version (
    id      smallint PRIMARY KEY DEFAULT 1,
    version bigint   NOT NULL DEFAULT 1,
    CONSTRAINT settings_version_singleton CHECK (id = 1)
);
INSERT INTO settings_version (id, version) VALUES (1, 1) ON CONFLICT DO NOTHING;

-- --------------------------------------------------------------------------
-- search_query_log: dedupe + audit for paid queries.
--
-- Brave is metered per query, and a query returning zero results still costs
-- one. Logging every query lets the planner skip a repeat inside the cache
-- window instead of paying for the same answer twice.
-- --------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS search_query_log (
    id           bigserial PRIMARY KEY,
    query_hash   text        NOT NULL,
    query        text        NOT NULL,
    provider     text        NOT NULL DEFAULT 'brave',
    product_id   bigint      REFERENCES products (id) ON DELETE SET NULL,
    result_count integer     NOT NULL DEFAULT 0,
    new_listings integer     NOT NULL DEFAULT 0,
    new_domains  integer     NOT NULL DEFAULT 0,
    ran_at       timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS search_query_log_hash_idx ON search_query_log (query_hash, ran_at DESC);
CREATE INDEX IF NOT EXISTS search_query_log_time_idx ON search_query_log (ran_at DESC);

-- --------------------------------------------------------------------------
-- discovery_cursor: per-product search scheduling with exponential backoff.
--
-- A product that has returned nothing new for several sweeps is very unlikely
-- to next sweep either, so it earns a longer interval. This is the main lever
-- that keeps a 300-SKU catalog inside a metered query budget.
-- --------------------------------------------------------------------------
ALTER TABLE discovery_cursor ADD COLUMN IF NOT EXISTS last_searched_at     timestamptz;
ALTER TABLE discovery_cursor ADD COLUMN IF NOT EXISTS next_eligible_at     timestamptz;
ALTER TABLE discovery_cursor ADD COLUMN IF NOT EXISTS consecutive_empty    integer NOT NULL DEFAULT 0;
ALTER TABLE discovery_cursor ADD COLUMN IF NOT EXISTS last_new_listing_at  timestamptz;
ALTER TABLE discovery_cursor ADD COLUMN IF NOT EXISTS total_queries        integer NOT NULL DEFAULT 0;
CREATE INDEX IF NOT EXISTS discovery_cursor_eligible_idx
    ON discovery_cursor (next_eligible_at NULLS FIRST);

-- --------------------------------------------------------------------------
-- sellers: sitemap capability.
--
-- The core economy of this system: a paid search query finds a DOMAIN, and a
-- free sitemap scan then enumerates every one of our products on it. Once a
-- domain is sitemap-capable we never need to pay to search it again.
-- --------------------------------------------------------------------------
ALTER TABLE sellers ADD COLUMN IF NOT EXISTS sitemap_url          text;
ALTER TABLE sellers ADD COLUMN IF NOT EXISTS sitemap_ok           boolean NOT NULL DEFAULT false;
ALTER TABLE sellers ADD COLUMN IF NOT EXISTS last_sitemap_scan_at timestamptz;
ALTER TABLE sellers ADD COLUMN IF NOT EXISTS sitemap_url_count    integer NOT NULL DEFAULT 0;
ALTER TABLE sellers ADD COLUMN IF NOT EXISTS first_seen_via       text;
CREATE INDEX IF NOT EXISTS sellers_sitemap_idx ON sellers (sitemap_ok, last_sitemap_scan_at NULLS FIRST);

-- --------------------------------------------------------------------------
-- listings: conditional-request caching + adaptive recheck cadence.
--
-- ETag / Last-Modified turn most nightly rechecks into a 304 with no body,
-- which saves bandwidth, saves a paid tier-2 fetch where one is in use, and is
-- markedly politer to the dealer's server.
-- --------------------------------------------------------------------------
ALTER TABLE listings ADD COLUMN IF NOT EXISTS etag                 text;
ALTER TABLE listings ADD COLUMN IF NOT EXISTS last_modified        text;
ALTER TABLE listings ADD COLUMN IF NOT EXISTS content_hash         text;
ALTER TABLE listings ADD COLUMN IF NOT EXISTS next_check_at        timestamptz;
ALTER TABLE listings ADD COLUMN IF NOT EXISTS check_interval_hours integer NOT NULL DEFAULT 24;
ALTER TABLE listings ADD COLUMN IF NOT EXISTS unchanged_streak     integer NOT NULL DEFAULT 0;
ALTER TABLE listings ADD COLUMN IF NOT EXISTS retired_at           timestamptz;
CREATE INDEX IF NOT EXISTS listings_due_idx
    ON listings (next_check_at NULLS FIRST)
    WHERE status = 'confirmed' AND retired_at IS NULL;

-- Existing confirmed rows become due immediately.
UPDATE listings SET next_check_at = now()
 WHERE status = 'confirmed' AND next_check_at IS NULL;
