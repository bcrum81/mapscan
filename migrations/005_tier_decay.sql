-- 005: let a blocked domain recover.
--
-- fetch_tier is promoted to 2 on repeated blocking, but bot protection is often
-- rate-triggered rather than permanent -- BigDealer blocked us after a burst
-- and served normally again later. Without a way back, one bad scan retires a
-- dealer forever. Recording when the promotion happened lets it lapse.
ALTER TABLE sellers ADD COLUMN IF NOT EXISTS tier_promoted_at timestamptz;
UPDATE sellers SET tier_promoted_at = now() WHERE fetch_tier = 2 AND tier_promoted_at IS NULL;
