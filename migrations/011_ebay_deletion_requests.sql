-- 011: audit trail for eBay Marketplace Account Deletion notices.
--
-- eBay requires a public endpoint that accepts account-deletion notifications
-- before it will activate a production keyset. This table is the record that a
-- notice was received and acted on.
--
-- Deliberately holds NO username. The whole point of the notice is that the
-- name must go, so keeping a copy in an audit table to prove we deleted it
-- would defeat the exercise. A SHA-256 is enough to recognise a duplicate
-- notice and to demonstrate the pipeline ran, without retaining the name.
--
-- notification_id is eBay's own identifier and is not personal data; it is
-- UNIQUE so their retries are idempotent rather than double-counted.
CREATE TABLE IF NOT EXISTS ebay_deletion_requests (
    id                  bigserial PRIMARY KEY,
    notification_id     text UNIQUE,
    username_sha256     text,
    listings_anonymized integer     NOT NULL DEFAULT 0,
    received_at         timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ebay_deletion_received_idx
    ON ebay_deletion_requests (received_at DESC);
