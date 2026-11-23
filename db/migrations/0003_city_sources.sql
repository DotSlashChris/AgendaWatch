-- aw_city_sources.SCHEMA (before the Nov 21 known-block columns).
CREATE TABLE IF NOT EXISTS city_sources(
  city_key TEXT NOT NULL,
  url TEXT NOT NULL,
  added_at TEXT NOT NULL,
  added_by TEXT NOT NULL,
  last_checked_at TEXT,
  last_ok_at TEXT,
  last_error TEXT,
  last_rows INTEGER,
  PRIMARY KEY(city_key, url)
);
CREATE TABLE IF NOT EXISTS city_source_docs(
  city_key TEXT NOT NULL,
  source_url TEXT NOT NULL,
  doc_url TEXT NOT NULL,
  meeting_date TEXT,
  first_seen_at TEXT NOT NULL,
  baseline INTEGER NOT NULL,
  PRIMARY KEY(source_url, doc_url)
);
