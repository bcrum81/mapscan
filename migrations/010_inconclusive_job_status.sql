-- 010: a job may finish without having established anything.
--
-- 'done' and 'failed' could not express the case this exists for: an eBay
-- sweep pointed at the sandbox runs cleanly to completion and matches nothing,
-- because the sandbox holds no real listings. Recording that as 'done' put a
-- green pill on a run that proves nothing about the live marketplace -- on the
-- one channel where every listing found is a violation. 'failed' would be
-- equally untrue: nothing went wrong.
--
-- Also repairs drift: 008 added 'ebay_sweep' to job_kind_valid by migration,
-- but schema.sql was never updated to match, so a fresh install built from
-- schema.sql would reject every eBay sweep insert. Restated here so both
-- paths agree.
ALTER TABLE crawl_jobs DROP CONSTRAINT IF EXISTS job_status_valid;
ALTER TABLE crawl_jobs ADD CONSTRAINT job_status_valid
    CHECK (status IN ('queued','running','done','failed','cancelled','inconclusive'));

ALTER TABLE crawl_jobs DROP CONSTRAINT IF EXISTS job_kind_valid;
ALTER TABLE crawl_jobs ADD CONSTRAINT job_kind_valid
    CHECK (kind IN ('domain_scan','discovery_sweep','recheck','ebay_sweep'));
