-- Agenda Watch — Durable Object SQLite (Cloudflare Worker), not data/agenda_watch.db.
-- Created in cloudflare/src/index.ts on the AgendaWatchContainer instance.
-- There is no D1 database, no Workers KV namespace, and no R2 bucket binding
-- in the current cloudflare/wrangler.jsonc. Container snapshots and the daily-run
-- lock live in these Durable Object tables (the "kv" table is a small key/value
-- store inside the DO, not Cloudflare Workers KV).

CREATE TABLE IF NOT EXISTS snap (
  name TEXT,
  idx INTEGER,
  data BLOB,
  PRIMARY KEY(name, idx)
);

CREATE TABLE IF NOT EXISTS daily_run (
  date TEXT PRIMARY KEY,
  source TEXT,
  started_at TEXT,
  finished_at TEXT,
  status TEXT,
  http INTEGER,
  result TEXT
);

CREATE TABLE IF NOT EXISTS kv (
  k TEXT PRIMARY KEY,
  v TEXT
);

CREATE TABLE IF NOT EXISTS usage (
  day TEXT,
  k TEXT,
  n INTEGER,
  PRIMARY KEY(day, k)
);
