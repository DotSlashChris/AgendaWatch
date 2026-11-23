-- aw_store.SCHEMA original tables (before later ALTERs).
CREATE TABLE IF NOT EXISTS fetch_runs(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  started_at TEXT NOT NULL,
  finished_at TEXT,
  outcome TEXT NOT NULL,
  exit_code INTEGER,
  total_notices INTEGER NOT NULL DEFAULT 0,
  per_city_json TEXT NOT NULL DEFAULT '{}',
  errors TEXT NOT NULL DEFAULT '',
  late_minutes INTEGER NOT NULL DEFAULT 0,
  purged_notices INTEGER NOT NULL DEFAULT 0,
  purged_runs INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS fetch_runs_started ON fetch_runs(started_at);
CREATE TABLE IF NOT EXISTS notices(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  notice_url TEXT NOT NULL UNIQUE,
  city_key TEXT NOT NULL,
  metro TEXT NOT NULL DEFAULT '',
  meeting_date TEXT,
  body TEXT NOT NULL DEFAULT '',
  title TEXT NOT NULL DEFAULT '',
  pdf_urls TEXT NOT NULL DEFAULT '[]',
  fetched_at TEXT NOT NULL,
  matched INTEGER NOT NULL DEFAULT 0,
  matched_terms TEXT NOT NULL DEFAULT '',
  text TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS notices_city_date ON notices(city_key, meeting_date);
CREATE INDEX IF NOT EXISTS notices_fetched ON notices(fetched_at);
CREATE TABLE IF NOT EXISTS documents(
  url TEXT PRIMARY KEY,
  notice_url TEXT NOT NULL DEFAULT '',
  etag TEXT,
  last_modified TEXT,
  length INTEGER,
  text_hash TEXT NOT NULL,
  text TEXT NOT NULL,
  fetched_at TEXT NOT NULL,
  checked_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS documents_checked ON documents(checked_at);
