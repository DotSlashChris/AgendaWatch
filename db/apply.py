#!/usr/bin/env python3
"""Create, migrate, or seed an Agenda Watch SQLite database.

Examples:
  python3 db/apply.py create  --db /tmp/aw.db
  python3 db/apply.py migrate --db /tmp/aw.db
  python3 db/apply.py seed    --db /tmp/aw.db
  python3 db/apply.py verify

create applies db/schema.sql (current schema in one file).
migrate applies db/migrations/*.sql in numeric order and records each file
in schema_migrations (id = the filename stem, e.g. 0001_core_accounts).
seed loads db/seed/catalog.sql (reference catalog + official city-source
seeds only — no accounts or customer data).

Version tracking: schema_migrations(id, applied_at). The running app also
writes rows such as stripe_live_v1, admin_swap_20261022, city_lock_v1, and
feedback_handling_20261112 when init_db() runs; those are app data-fix
markers, not these SQL files. Do not reuse those ids for SQL migrations.
"""
from __future__ import annotations

import argparse
import datetime as dt
import sqlite3
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
DB_DIR = Path(__file__).resolve().parent
MIGRATIONS = DB_DIR / "migrations"
SCHEMA = DB_DIR / "schema.sql"
SEED = DB_DIR / "seed" / "catalog.sql"
TZ = ZoneInfo("America/Denver")
REF_PREFIX = "ref_"
APP_TABLE_SKIP = {"sqlite_sequence"}


def now_iso():
    return dt.datetime.now(TZ).isoformat(timespec="seconds")


def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    con.execute("PRAGMA foreign_keys = ON")
    return con


def apply_sql(con: sqlite3.Connection, sql: str):
    con.executescript(sql)


def cmd_create(db: Path):
    sql = SCHEMA.read_text()
    with connect(db) as con:
        apply_sql(con, sql)
        con.execute(
            "INSERT OR IGNORE INTO schema_migrations(id, applied_at) VALUES(?, ?)",
            ("schema.sql", now_iso()),
        )
    print(f"created schema in {db}")


def migration_files():
    return sorted(p for p in MIGRATIONS.glob("*.sql") if p.name[:4].isdigit())


def cmd_migrate(db: Path):
    with connect(db) as con:
        con.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations("
            "id TEXT PRIMARY KEY, applied_at TEXT NOT NULL)"
        )
        done = {r[0] for r in con.execute("SELECT id FROM schema_migrations")}
        applied = 0
        for path in migration_files():
            mid = path.stem
            if mid in done:
                print(f"skip {mid} (already applied)")
                continue
            apply_sql(con, path.read_text())
            con.execute(
                "INSERT INTO schema_migrations(id, applied_at) VALUES(?, ?)",
                (mid, now_iso()),
            )
            print(f"applied {mid}")
            applied += 1
        con.commit()
    print(f"migrate complete on {db} ({applied} new)")


def cmd_seed(db: Path):
    sql = SEED.read_text()
    with connect(db) as con:
        # city_sources must already exist (create or migrate first)
        apply_sql(con, sql)
    print(f"loaded seed into {db}")


def _norm_default(v):
    if v is None:
        return None
    s = str(v)
    if len(s) >= 2 and s[0] == s[-1] and s[0] in ("'", '"'):
        s = s[1:-1]
    return s


def dump_app_schema(con: sqlite3.Connection) -> dict:
    """Column and index metadata for application tables (excludes ref_* seed tables)."""
    tables = [
        r[0]
        for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%' ORDER BY 1"
        )
        if not r[0].startswith(REF_PREFIX) and r[0] not in APP_TABLE_SKIP
    ]
    out = {"tables": {}, "indexes": {}}
    for t in tables:
        cols = []
        for c in con.execute(f"PRAGMA table_info({t})"):
            cols.append([c[1], (c[2] or "").upper(), int(c[3]), _norm_default(c[4]), int(c[5])])
        out["tables"][t] = cols
        idx = []
        for r in con.execute(
            "SELECT name, sql FROM sqlite_master WHERE type='index' AND tbl_name=? AND sql IS NOT NULL ORDER BY 1",
            (t,),
        ):
            sql = " ".join((r[1] or "").split()).replace("IF NOT EXISTS ", "")
            idx.append([r[0], sql])
        out["indexes"][t] = idx
    return out


def schema_diff(a: dict, b: dict) -> list[str]:
    diffs = []
    ta, tb = set(a["tables"]), set(b["tables"])
    if ta != tb:
        diffs.append(f"tables only in A: {sorted(ta - tb)}; only in B: {sorted(tb - ta)}")
    for t in sorted(ta & tb):
        if a["tables"][t] != b["tables"][t]:
            diffs.append(f"{t} columns A={a['tables'][t]} B={b['tables'][t]}")
        if a["indexes"].get(t) != b["indexes"].get(t):
            diffs.append(f"{t} indexes A={a['indexes'].get(t)} B={b['indexes'].get(t)}")
    return diffs


def schema_from_init_db(dest: Path) -> dict:
    """Run the application's init_db + feedback table ensure in a fresh interpreter-facing import."""
    import os
    import importlib

    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        dest.unlink()
    os.environ["AW_DB"] = str(dest)
    # Fresh process is safer; this function is also used in-process by tests via subprocess.
    sys.path.insert(0, str(ROOT))
    # If web.app was already imported with another AW_DB, refuse.
    if "web.app" in sys.modules or "web" in sys.modules:
        raise RuntimeError("web.app already imported; run verify in a fresh process")
    import web.app as webapp

    with sqlite3.connect(dest) as con:
        webapp._ensure_feedback_tables(con)
        con.commit()
        return dump_app_schema(con)


def cmd_verify(work: Path):
    work.mkdir(parents=True, exist_ok=True)
    from_schema = work / "from_schema.db"
    from_mig = work / "from_migrations.db"
    from_init = work / "from_init_db.db"
    for p in (from_schema, from_mig):
        if p.exists():
            p.unlink()
    cmd_create(from_schema)
    cmd_migrate(from_mig)
    cmd_seed(from_schema)
    cmd_seed(from_mig)
    with connect(from_schema) as a, connect(from_mig) as b:
        sa, sb = dump_app_schema(a), dump_app_schema(b)
        n_cities = a.execute("SELECT COUNT(*) FROM ref_cities").fetchone()[0]
        n_kw = a.execute("SELECT COUNT(*) FROM ref_keywords").fetchone()[0]
        n_src = a.execute("SELECT COUNT(*) FROM city_sources").fetchone()[0]
        n_users = a.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        n_watch = a.execute("SELECT COUNT(*) FROM watches").fetchone()[0]
    diffs_ab = schema_diff(sa, sb)
    if diffs_ab:
        print("SCHEMA MISMATCH: schema.sql vs migrations")
        for d in diffs_ab:
            print(" ", d)
        return 1
    print("schema.sql and migrations produce the same application schema")
    print(f"  tables: {len(sa['tables'])}  seed: ref_cities={n_cities} keywords={n_kw} city_sources={n_src}")
    if n_users or n_watch:
        print(f"ERROR: seed loaded customer-like rows: users={n_users} watches={n_watch}")
        return 1
    if n_cities != 17 or n_kw != 8 or n_src < 1:
        print(f"ERROR: unexpected seed counts cities={n_cities} keywords={n_kw} sources={n_src}")
        return 1

    # Compare with the application init path in a subprocess so AW_DB is set first.
    import subprocess

    helper = work / "dump_init.py"
    helper.write_text(
        "import os, sqlite3, sys\n"
        f"sys.path.insert(0, {str(ROOT)!r})\n"
        f"os.environ['AW_DB'] = {str(from_init)!r}\n"
        "import web.app as webapp\n"
        "from db.apply import dump_app_schema, connect\n"
        "con = sqlite3.connect(os.environ['AW_DB'])\n"
        "webapp._ensure_feedback_tables(con)\n"
        "import json\n"
        "print(json.dumps(dump_app_schema(con)))\n"
    )
    if from_init.exists():
        from_init.unlink()
    proc = subprocess.run(
        [sys.executable, str(helper)],
        cwd=str(ROOT),
        check=False,
        capture_output=True,
        text=True,
        env={**__import__("os").environ, "AW_DB": str(from_init)},
    )
    if proc.returncode != 0:
        print("init_db schema dump failed:")
        print(proc.stderr[-4000:])
        return 1
    import json

    si = json.loads(proc.stdout.strip().splitlines()[-1])
    diffs_ai = schema_diff(sa, si)
    if diffs_ai:
        print("SCHEMA MISMATCH: schema.sql vs application init_db")
        for d in diffs_ai:
            print(" ", d)
        return 1
    print("schema.sql, migrations, and application init_db produce the same application schema")
    return 0


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("command", choices=("create", "migrate", "seed", "verify"))
    p.add_argument("--db", type=Path, help="SQLite file (create/migrate/seed)")
    p.add_argument("--work", type=Path, default=DB_DIR / ".work", help="scratch dir for verify")
    args = p.parse_args(argv)
    if args.command == "verify":
        return cmd_verify(args.work)
    if not args.db:
        p.error("--db is required for create, migrate, and seed")
    {"create": cmd_create, "migrate": cmd_migrate, "seed": cmd_seed}[args.command](args.db)
    return 0


if __name__ == "__main__":
    sys.exit(main())
