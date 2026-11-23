"""db/schema.sql and db/migrations produce the same application schema as init_db; seeds are catalog-only."""
import sqlite3
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
APPLY = ROOT / "db" / "apply.py"


def test_schema_migrations_and_init_db_match(tmp_path):
    work = tmp_path / "db-verify"
    proc = subprocess.run(
        [sys.executable, str(APPLY), "verify", "--work", str(work)],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stdout + "\n" + proc.stderr
    assert "produce the same application schema" in proc.stdout
    for name in ("from_schema.db", "from_migrations.db", "from_init_db.db"):
        assert (work / name).is_file()
    with sqlite3.connect(work / "from_schema.db") as con:
        assert con.execute("SELECT COUNT(*) FROM ref_cities").fetchone()[0] == 17
        assert con.execute("SELECT COUNT(*) FROM ref_keywords").fetchone()[0] == 8
        assert con.execute("SELECT COUNT(*) FROM city_sources").fetchone()[0] >= 1
        assert con.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0
        assert con.execute("SELECT COUNT(*) FROM watches").fetchone()[0] == 0
        assert con.execute("SELECT COUNT(*) FROM feedback").fetchone()[0] == 0
        assert con.execute("SELECT COUNT(*) FROM user_alerts").fetchone()[0] == 0
        url = con.execute("SELECT url FROM city_sources WHERE city_key='ch'").fetchone()[0]
        assert "cottonwoodheights.utah.gov" in url
        assert "planning-commission" in url
