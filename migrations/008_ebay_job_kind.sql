-- 008: allow the eBay sweep as a job kind.
ALTER TABLE crawl_jobs DROP CONSTRAINT IF EXISTS job_kind_valid;
ALTER TABLE crawl_jobs ADD CONSTRAINT job_kind_valid
    CHECK (kind IN ('domain_scan','discovery_sweep','recheck','ebay_sweep'));
