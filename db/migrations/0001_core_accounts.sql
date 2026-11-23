-- Original web tables (web/app.py init_db CREATE).
CREATE TABLE IF NOT EXISTS users(
  email TEXT PRIMARY KEY,
  created_at TEXT NOT NULL
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
  PRIMARY KEY(email, city_key)
);
CREATE TABLE IF NOT EXISTS magic_nonces(
  nonce TEXT PRIMARY KEY,
  email TEXT NOT NULL,
  created_at REAL NOT NULL,
  used_at REAL
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
