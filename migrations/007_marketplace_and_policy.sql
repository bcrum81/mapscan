-- ===========================================================================
-- 007: unauthorised-channel monitoring
--
-- Until now every violation meant "priced below MAP". Some channels are
-- prohibited outright -- nobody is authorised to resell on eBay -- so ANY
-- listing there is a violation whatever the price.
--
-- Modelled as a per-seller policy rather than hardcoding eBay, because the same
-- rule will apply to other marketplaces.
-- ===========================================================================

ALTER TABLE sellers ADD COLUMN IF NOT EXISTS policy text NOT NULL DEFAULT 'map';
DO $$ BEGIN
    ALTER TABLE sellers ADD CONSTRAINT seller_policy_valid
        CHECK (policy IN ('map', 'prohibited'));
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

COMMENT ON COLUMN sellers.policy IS
    'map = flag only when priced below MAP; prohibited = flag ANY listing';

-- Marketplace listings need the individual seller identified: "ebay.com" is the
-- venue, but enforcement is against the account offering the item.
ALTER TABLE listings ADD COLUMN IF NOT EXISTS marketplace_seller text;
ALTER TABLE listings ADD COLUMN IF NOT EXISTS external_id       text;
ALTER TABLE listings ADD COLUMN IF NOT EXISTS listing_condition  text;
CREATE INDEX IF NOT EXISTS listings_marketplace_seller_idx
    ON listings (marketplace_seller) WHERE marketplace_seller IS NOT NULL;
CREATE INDEX IF NOT EXISTS listings_external_id_idx
    ON listings (external_id) WHERE external_id IS NOT NULL;

-- Violations now carry a reason.
ALTER TABLE violations ADD COLUMN IF NOT EXISTS kind text NOT NULL DEFAULT 'map';
DO $$ BEGIN
    ALTER TABLE violations ADD CONSTRAINT violation_kind_valid
        CHECK (kind IN ('map', 'unauthorized'));
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

COMMENT ON COLUMN violations.kind IS
    'map = advertised below MAP; unauthorized = listed on a prohibited channel';

-- An unauthorised listing has no meaningful MAP delta, so those columns are
-- allowed to be nil for that kind.
ALTER TABLE violations ALTER COLUMN opening_price DROP NOT NULL;
ALTER TABLE violations ALTER COLUMN lowest_price  DROP NOT NULL;
ALTER TABLE violations ALTER COLUMN map_at_open   DROP NOT NULL;

CREATE INDEX IF NOT EXISTS violations_kind_idx ON violations (kind, status);

-- The eBay venue itself, prohibited by default.
INSERT INTO sellers (domain, policy, notes)
VALUES ('ebay.com', 'prohibited',
        'No reseller is authorised on eBay: any listing is a violation')
ON CONFLICT (domain) DO UPDATE SET policy = 'prohibited';
