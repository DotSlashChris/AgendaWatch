-- aw_alerts.SCHEMA
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
