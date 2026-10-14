-- 006: the ability to remove things.
--
-- Until now everything the scanner found was permanent: a wrongly-matched
-- listing, a dealer scanned by mistake, or a domain that should never be
-- tracked could all be added but never taken away.

-- Exclude a seller from all future scanning and matching. Kept as a flag rather
-- than a delete so the exclusion survives -- deleting the row would just let the
-- next scan re-create it.
ALTER TABLE sellers ADD COLUMN IF NOT EXISTS excluded        boolean NOT NULL DEFAULT false;
ALTER TABLE sellers ADD COLUMN IF NOT EXISTS excluded_at     timestamptz;
ALTER TABLE sellers ADD COLUMN IF NOT EXISTS excluded_reason text;
CREATE INDEX IF NOT EXISTS sellers_excluded_idx ON sellers (excluded) WHERE excluded;

-- Merge www.<domain> duplicates created before host normalisation was fixed.
-- The fallback used to store the host it actually fetched, so a site reached via
-- the www variant became a second seller with its own listings.
DO $$
DECLARE dup RECORD;
BEGIN
    FOR dup IN
        SELECT w.id AS www_id, b.id AS base_id, w.domain AS www_domain
          FROM sellers w
          JOIN sellers b ON b.domain = regexp_replace(w.domain, '^www\.', '')
         WHERE w.domain LIKE 'www.%'
    LOOP
        UPDATE listings SET seller_id = dup.base_id WHERE seller_id = dup.www_id;
        DELETE FROM sellers WHERE id = dup.www_id;
        RAISE NOTICE 'merged % into its non-www seller', dup.www_domain;
    END LOOP;
END $$;

-- Any remaining www.* sellers with no duplicate simply get renamed.
UPDATE sellers SET domain = regexp_replace(domain, '^www\.', '')
 WHERE domain LIKE 'www.%'
   AND NOT EXISTS (
       SELECT 1 FROM sellers s2
        WHERE s2.domain = regexp_replace(sellers.domain, '^www\.', ''));
