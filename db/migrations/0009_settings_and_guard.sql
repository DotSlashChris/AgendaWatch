-- settings (web/app.py and aw_guard) plus aw_guard usage/trip/lock tables.
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
