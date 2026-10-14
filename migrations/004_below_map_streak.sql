-- 004: decouple the violation debounce from observation storage.
--
-- price_observations are written only when the price CHANGES, to stop the
-- time-series growing by one row per listing per night. The debounce counted
-- consecutive sub-MAP *observations* -- so a price sitting stably below MAP
-- produced exactly one observation and never reached the threshold. A dealer
-- persistently under MAP, the single most important case, would never open a
-- violation. The streak is now counted per CHECK on the listing itself.
ALTER TABLE listings ADD COLUMN IF NOT EXISTS below_map_streak integer NOT NULL DEFAULT 0;
