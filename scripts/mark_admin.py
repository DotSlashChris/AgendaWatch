"""Mark a user row is_admin=1 in a local DB.

Usage: AW_DB=/path/db python3 scripts/mark_admin.py [email]

Admin access in the running app requires the address to also be listed in the
secret AW_ADMIN_EMAILS (comma-separated). A missing secret denies everyone
(fail closed). This script only updates the database flag.

Default email is the first argument, otherwise AW_OWNER_EMAIL.
"""
import os, sqlite3, sys
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
db = Path(os.environ.get("AW_DB") or ROOT / "data" / "agenda_watch.db")
email = (sys.argv[1] if len(sys.argv) > 1 else (os.environ.get("AW_OWNER_EMAIL") or "")).strip().lower()
if not email:
    sys.exit("usage: python3 scripts/mark_admin.py EMAIL  (or set AW_OWNER_EMAIL)")
with sqlite3.connect(db) as c:
    cols = {r[1] for r in c.execute("PRAGMA table_info(users)")}
    if "is_admin" not in cols:
        c.execute("ALTER TABLE users ADD COLUMN is_admin INTEGER NOT NULL DEFAULT 0")
    n = c.execute("UPDATE users SET is_admin=1 WHERE email=?", (email,)).rowcount
print(f"{db}: is_admin=1 for {email} ({n} row)")
