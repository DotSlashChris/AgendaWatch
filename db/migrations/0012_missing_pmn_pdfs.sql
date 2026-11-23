-- aw_store.SCHEMA addition, 2026-11-21 fetch-errors (dead PMN PDF memory).
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
