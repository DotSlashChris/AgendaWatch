-- aw_city_sources.ensure() ALTERs, 2026-11-21 fetch-errors (known-blocked listing pages).
ALTER TABLE city_sources ADD COLUMN block_status TEXT;
ALTER TABLE city_sources ADD COLUMN block_count INTEGER NOT NULL DEFAULT 0;
ALTER TABLE city_sources ADD COLUMN block_http TEXT;
ALTER TABLE city_sources ADD COLUMN block_first_at TEXT;
ALTER TABLE city_sources ADD COLUMN block_last_at TEXT;
ALTER TABLE city_sources ADD COLUMN known_blocked_logged INTEGER NOT NULL DEFAULT 0;
