-- Created on first feedback submit (web/app.py _ensure_feedback_tables).
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
