-- enabled_at, then city-lock columns (locked_at, blocked_at).
ALTER TABLE watch_cities ADD COLUMN enabled_at TEXT;
ALTER TABLE watch_cities ADD COLUMN locked_at TEXT;
ALTER TABLE watch_cities ADD COLUMN blocked_at TEXT;
