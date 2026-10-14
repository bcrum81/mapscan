-- 003: wholesale price from the product sheet.
--
-- Not required for MAP enforcement, but the sheet supplies it and silently
-- discarding a column the user provided is worse than storing it. Useful
-- context on a violation: it shows how far below your own cost a dealer has
-- gone. Behind admin auth like everything else.
ALTER TABLE products ADD COLUMN IF NOT EXISTS wholesale_price numeric(10,2);
