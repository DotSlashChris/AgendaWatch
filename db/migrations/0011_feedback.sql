-- Current feedback table after feedback_handling_20261112 (widened status + reply columns).
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
