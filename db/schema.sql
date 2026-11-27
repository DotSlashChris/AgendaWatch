-- Agenda Watch — current SQLite schema for data/agenda_watch.db
-- Built from the application's CREATE / ALTER / ensure_* logic (Nov 23, 2026).
-- The box-local copy of the live database can lag behind this; this file is the
-- schema the current code creates on a fresh database.
-- No triggers. No D1. Workers KV and R2 are not used in the current wrangler.jsonc;
-- Durable Object tables are in durable_object.sql and are not part of this file.
-- Apply: sqlite3 data/agenda_watch.db < db/schema.sql

PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS users(
  email TEXT PRIMARY KEY,
  created_at TEXT NOT NULL,
  stripe_customer_id TEXT,
  stripe_subscription_id TEXT,
  subscription_status TEXT,
  paid_updated_at TEXT,
  extra_cities INTEGER,
  billing_synced_at REAL,
  is_admin INTEGER NOT NULL DEFAULT 0,
  test_stripe_customer_id TEXT,
  test_stripe_subscription_id TEXT,
  test_subscription_status TEXT,
  test_extra_cities INTEGER,
  cancel_at REAL,
  period_end REAL,
  ref TEXT
);

CREATE TABLE IF NOT EXISTS watches(
  email TEXT PRIMARY KEY REFERENCES users(email),
  keywords TEXT NOT NULL,
  alerts_on INTEGER NOT NULL DEFAULT 0,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS watch_cities(
  email TEXT NOT NULL REFERENCES watches(email),
  city_key TEXT NOT NULL,
  enabled INTEGER NOT NULL DEFAULT 0,
  street TEXT NOT NULL DEFAULT '',
  updated_at TEXT,
  enabled_at TEXT,
  locked_at TEXT,
  blocked_at TEXT,
  PRIMARY KEY(email, city_key)
);

CREATE TABLE IF NOT EXISTS magic_nonces(
  nonce TEXT PRIMARY KEY,
  email TEXT NOT NULL,
  created_at REAL NOT NULL,
  used_at REAL,
  ref TEXT
);

CREATE TABLE IF NOT EXISTS alerts_sent(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  email TEXT NOT NULL,
  sent_date TEXT NOT NULL,
  city TEXT NOT NULL,
  item TEXT NOT NULL,
  link TEXT NOT NULL,
  meeting TEXT
);

CREATE TABLE IF NOT EXISTS schema_migrations(
  id TEXT PRIMARY KEY,
  applied_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS city_sources(
  city_key TEXT NOT NULL,
  url TEXT NOT NULL,
  added_at TEXT NOT NULL,
  added_by TEXT NOT NULL,
  last_checked_at TEXT,
  last_ok_at TEXT,
  last_error TEXT,
  last_rows INTEGER,
  block_status TEXT,
  block_count INTEGER NOT NULL DEFAULT 0,
  block_http TEXT,
  block_first_at TEXT,
  block_last_at TEXT,
  known_blocked_logged INTEGER NOT NULL DEFAULT 0,
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
  purged_runs INTEGER NOT NULL DEFAULT 0,
  alert_emails INTEGER NOT NULL DEFAULT 0,
  alert_items INTEGER NOT NULL DEFAULT 0,
  alert_hits INTEGER NOT NULL DEFAULT 0
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
  text TEXT NOT NULL DEFAULT '',
  fingerprint TEXT NOT NULL DEFAULT ''
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

CREATE TABLE IF NOT EXISTS missing_pmn_pdfs(
  url TEXT PRIMARY KEY,
  notice_url TEXT NOT NULL DEFAULT '',
  first_404_at TEXT NOT NULL,
  last_404_at TEXT NOT NULL,
  last_checked_at TEXT NOT NULL,
  consecutive_dates INTEGER NOT NULL DEFAULT 1,
  status TEXT NOT NULL DEFAULT 'watching',
  permanent_logged INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS user_alerts(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  email TEXT NOT NULL,
  fingerprint TEXT NOT NULL,
  city_key TEXT NOT NULL,
  meeting_date TEXT NOT NULL,
  item_text TEXT NOT NULL,
  notice_url TEXT NOT NULL DEFAULT '',
  link TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL,
  attempts INTEGER NOT NULL DEFAULT 0,
  resend_id TEXT,
  error TEXT,
  first_at TEXT NOT NULL,
  last_at TEXT NOT NULL,
  UNIQUE(email, fingerprint)
);
CREATE INDEX IF NOT EXISTS user_alerts_email_city_date ON user_alerts(email, city_key, meeting_date);

CREATE TABLE IF NOT EXISTS settings(
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL,
  updated_at TEXT
);

CREATE TABLE IF NOT EXISTS guard_usage(
  day TEXT NOT NULL,
  key TEXT NOT NULL,
  count INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY(day, key)
);

CREATE TABLE IF NOT EXISTS guard_trips(
  day TEXT NOT NULL,
  key TEXT NOT NULL,
  at TEXT NOT NULL,
  detail TEXT NOT NULL DEFAULT '',
  notified INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY(day, key)
);

CREATE TABLE IF NOT EXISTS guard_lock(
  name TEXT PRIMARY KEY,
  holder TEXT NOT NULL,
  acquired_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS feedback(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  created_at TEXT NOT NULL,
  user_id TEXT REFERENCES users(email),
  email TEXT NOT NULL DEFAULT '',
  category TEXT NOT NULL,
  city TEXT NOT NULL DEFAULT '',
  link TEXT NOT NULL DEFAULT '',
  message TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'new' CHECK(status IN ('new','needs-owner','replied','reviewed')),
  replied_at TEXT,
  reply_text TEXT,
  owner_note TEXT
);
CREATE INDEX IF NOT EXISTS feedback_created ON feedback(created_at);

CREATE TABLE IF NOT EXISTS feedback_hits(
  ip_hash TEXT NOT NULL,
  at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS feedback_hits_ip ON feedback_hits(ip_hash, at);

CREATE TABLE IF NOT EXISTS feedback_spam(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  at TEXT NOT NULL,
  reason TEXT NOT NULL,
  ip_hash TEXT NOT NULL DEFAULT '',
  category TEXT NOT NULL DEFAULT '',
  snippet TEXT NOT NULL DEFAULT ''
);
