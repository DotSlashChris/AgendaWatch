"""Run log + notice archive (SQLite), shared by agenda_watch.py (writer) and web/app.py (admin reader).

Tables
  fetch_runs  one row per daily run-owner
  notices     one row per PMN notice seen (deduped by notice_url; re-seen notices are updated)
Retention: purge() deletes notices / fetch_runs older than RETENTION_DAYS. It never touches
users, watches, watch_cities, alerts_sent or sent history.
"""
import datetime as dt
import json
import sqlite3
from zoneinfo import ZoneInfo

TZ = ZoneInfo("America/Denver")
RETENTION_DAYS = 365
TEXT_LIMIT = 8000          # stored searchable text per notice (agenda + PDF text excerpt)
SCHEDULED_HHMM = (8, 21)   # daily run time, America/Denver (for late_minutes)
# PMN PDF HTTP 404: remember the dead URL, log the first miss, then once as permanently
# missing after this many distinct America/Denver dates, and stop retrying daily.
# A later weekly recheck still picks the file up if PMN starts serving the same ID.
PDF_404_PERMANENT_AFTER = 2
PDF_404_RECHECK_DAYS = 7
PDF_404_SKIP_MSG = "PMN PDF missing (skipped): {url} HTTP 404"
PDF_404_PERM_MSG = "PMN PDF permanently missing (will not retry daily): {url}"

SCHEMA = """
CREATE TABLE IF NOT EXISTS fetch_runs(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  started_at TEXT NOT NULL,
  finished_at TEXT,
  outcome TEXT NOT NULL,              -- silence | hit | partial | failure | alerts_sent
  exit_code INTEGER,
  total_notices INTEGER NOT NULL DEFAULT 0,
  per_city_json TEXT NOT NULL DEFAULT '{}',
  errors TEXT NOT NULL DEFAULT '',
  late_minutes INTEGER NOT NULL DEFAULT 0,
  purged_notices INTEGER NOT NULL DEFAULT 0,
  purged_runs INTEGER NOT NULL DEFAULT 0);
CREATE INDEX IF NOT EXISTS fetch_runs_started ON fetch_runs(started_at);
CREATE TABLE IF NOT EXISTS notices(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  notice_url TEXT NOT NULL UNIQUE,
  city_key TEXT NOT NULL,
  metro TEXT NOT NULL DEFAULT '',
  meeting_date TEXT,                  -- ISO datetime of the meeting (America/Denver)
  body TEXT NOT NULL DEFAULT '',
  title TEXT NOT NULL DEFAULT '',
  pdf_urls TEXT NOT NULL DEFAULT '[]',
  fetched_at TEXT NOT NULL,           -- last time the notice was seen
  matched INTEGER NOT NULL DEFAULT 0,
  matched_terms TEXT NOT NULL DEFAULT '',
  text TEXT NOT NULL DEFAULT '');
CREATE INDEX IF NOT EXISTS notices_city_date ON notices(city_key, meeting_date);
CREATE INDEX IF NOT EXISTS notices_fetched ON notices(fetched_at);
CREATE TABLE IF NOT EXISTS documents(
  url TEXT PRIMARY KEY,               -- PMN attachment URL (/pmn/files/<id>.pdf)
  notice_url TEXT NOT NULL DEFAULT '',
  etag TEXT, last_modified TEXT, length INTEGER,
  text_hash TEXT NOT NULL,
  text TEXT NOT NULL,                 -- full pdftotext output (the PDF itself is deleted)
  fetched_at TEXT NOT NULL,           -- last full download + extract
  checked_at TEXT NOT NULL);          -- last time a run used/validated it (purge key)
CREATE INDEX IF NOT EXISTS documents_checked ON documents(checked_at);
CREATE TABLE IF NOT EXISTS missing_pmn_pdfs(
  url TEXT PRIMARY KEY,               -- official /pmn/files/<id>.pdf that returned HTTP 404
  notice_url TEXT NOT NULL DEFAULT '',
  first_404_at TEXT NOT NULL,
  last_404_at TEXT NOT NULL,
  last_checked_at TEXT NOT NULL,      -- last GET of this URL (not a quiet skip)
  consecutive_dates INTEGER NOT NULL DEFAULT 1,
  status TEXT NOT NULL DEFAULT 'watching',   -- watching | permanent
  permanent_logged INTEGER NOT NULL DEFAULT 0);
"""


def ensure_schema(conn):
    conn.executescript(SCHEMA)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(notices)")}
    if "fingerprint" not in cols:   # "Last Edited On" + sorted attachment URLs -> notice-level skip
        conn.execute("ALTER TABLE notices ADD COLUMN fingerprint TEXT NOT NULL DEFAULT ''")
    rcols = {r[1] for r in conn.execute("PRAGMA table_info(fetch_runs)")}
    for col in ("alert_emails", "alert_items", "alert_hits"):     # per-user alert emails sent by this run
        if col not in rcols:
            conn.execute(f"ALTER TABLE fetch_runs ADD COLUMN {col} INTEGER NOT NULL DEFAULT 0")


def load_cache(conn):
    """{notice_url: fingerprint}, {doc_url: row dict} for the fetcher (read once, before threads start)."""
    conn.row_factory = sqlite3.Row
    fps = {r["notice_url"]: r["fingerprint"] for r in conn.execute("SELECT notice_url, fingerprint FROM notices")}
    docs = {r["url"]: dict(r) for r in conn.execute("SELECT * FROM documents")}
    conn.row_factory = None
    return fps, docs


def save_documents(conn, rows, checked_urls, now=None):
    now = now or now_iso()
    for d in rows:
        conn.execute(
            """INSERT INTO documents(url,notice_url,etag,last_modified,length,text_hash,text,fetched_at,checked_at)
               VALUES(?,?,?,?,?,?,?,?,?)
               ON CONFLICT(url) DO UPDATE SET notice_url=excluded.notice_url, etag=excluded.etag,
                 last_modified=excluded.last_modified, length=excluded.length, text_hash=excluded.text_hash,
                 text=excluded.text, fetched_at=excluded.fetched_at, checked_at=excluded.checked_at""",
            (d["url"], d.get("notice_url") or "", d.get("etag"), d.get("last_modified"), d.get("length"),
             d["text_hash"], d["text"], d.get("fetched_at") or now, now))
    conn.executemany("UPDATE documents SET checked_at=? WHERE url=?", [(now, u) for u in checked_urls])


def now_iso():
    return dt.datetime.now(TZ).isoformat(timespec="seconds")


def late_minutes(started: dt.datetime) -> int:
    s = started.astimezone(TZ)
    sched = s.replace(hour=SCHEDULED_HHMM[0], minute=SCHEDULED_HHMM[1], second=0, microsecond=0)
    return max(0, int((s - sched).total_seconds() // 60))


def outcome_for(code: int) -> str:
    return {0: "silence", 4: "hit", 3: "partial"}.get(code, "failure")


ALERTS_SENT = "alerts_sent"
ALERT_WINDOW_MIN = 30   # user alerts go out right after run-owner writes the run row


def mark_alerts(conn, run_id, emails, items, hits=None):
    """Record the per-user alert emails a run sent. A run that sent any alert email is not
    'silence': its outcome becomes 'alerts_sent' (hit / partial / failure are left as they are)."""
    if not run_id:
        return None
    conn.execute("UPDATE fetch_runs SET alert_emails=?, alert_items=?, alert_hits=? WHERE id=?",
                 (int(emails or 0), int(items or 0), int(hits if hits is not None else items or 0), run_id))
    if emails:
        conn.execute("UPDATE fetch_runs SET outcome=? WHERE id=? AND outcome='silence'", (ALERTS_SENT, run_id))
    r = conn.execute("SELECT outcome FROM fetch_runs WHERE id=?", (run_id,)).fetchone()
    return r[0] if r else None


def reconcile_alert_outcomes(conn):
    """Idempotent repair for runs recorded before mark_alerts existed (e.g. 2026-10-27 run 15:
    1 alert email with 4 items, recorded as 'silence'). A 'silence' run with no alert counts gets
    the user_alerts rows sent between its start and finished_at + ALERT_WINDOW_MIN (and before the
    next run starts): emails = distinct Resend ids, items = rows. Returns the run ids fixed."""
    ensure_schema(conn)
    has = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='user_alerts'").fetchone()
    if not has:
        return []
    runs = conn.execute("SELECT id, started_at, finished_at, outcome, alert_emails FROM fetch_runs ORDER BY id").fetchall()
    sent = [(dt.datetime.fromisoformat(a), rid) for a, rid in
            conn.execute("SELECT first_at, resend_id FROM user_alerts WHERE status='sent' AND resend_id IS NOT NULL")]
    fixed = []
    for i, (rid, st, fin, outcome, emails) in enumerate(runs):
        if outcome != "silence" or emails or not fin:
            continue
        lo = dt.datetime.fromisoformat(st)
        hi = dt.datetime.fromisoformat(fin) + dt.timedelta(minutes=ALERT_WINDOW_MIN)
        if i + 1 < len(runs):
            hi = min(hi, dt.datetime.fromisoformat(runs[i + 1][1]))
        mine = [r for at, r in sent if lo <= at <= hi]
        if mine:
            mark_alerts(conn, rid, len(set(mine)), len(mine), len(mine))
            fixed.append(rid)
    return fixed


def outcome_label(run):
    """'alerts sent (1 email, 4 items)' for the admin page / reports; other outcomes as stored."""
    o = run.get("outcome")
    e, n = run.get("alert_emails") or 0, run.get("alert_items") or 0
    if o == ALERTS_SENT:
        return f"alerts sent ({e} email{'s' if e != 1 else ''}, {n} item{'s' if n != 1 else ''})"
    if e:
        return f"{o} + alerts sent ({e} email{'s' if e != 1 else ''}, {n} item{'s' if n != 1 else ''})"
    return o


def upsert_notices(conn, notices, fetched_at=None):
    fetched_at = fetched_at or now_iso()
    n = 0
    for x in notices:
        terms = sorted(set(x.get("matched_terms") or []))
        conn.execute(
            """INSERT INTO notices(notice_url,city_key,metro,meeting_date,body,title,pdf_urls,fetched_at,
                                   matched,matched_terms,text,fingerprint)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(notice_url) DO UPDATE SET
                 city_key=excluded.city_key, metro=excluded.metro, meeting_date=excluded.meeting_date,
                 body=excluded.body, title=excluded.title, pdf_urls=excluded.pdf_urls,
                 fetched_at=excluded.fetched_at, matched=excluded.matched,
                 matched_terms=excluded.matched_terms, text=excluded.text,
                 fingerprint=excluded.fingerprint""",
            (x["notice_url"], x.get("city_key") or "", x.get("metro") or "", x.get("meeting_date"),
             x.get("body") or "", x.get("title") or "", json.dumps(x.get("pdf_urls") or []), fetched_at,
             int(bool(terms)), ", ".join(terms), (x.get("text") or "")[:TEXT_LIMIT], x.get("fingerprint") or ""),
        )
        n += 1
    return n


def purge(conn, now=None, days=RETENTION_DAYS):
    """Delete notices (by fetched_at) and fetch_runs (by started_at) older than `days`."""
    cutoff = ((now or dt.datetime.now(TZ)) - dt.timedelta(days=days)).isoformat(timespec="seconds")
    pn = conn.execute("DELETE FROM notices WHERE fetched_at < ?", (cutoff,)).rowcount
    pr = conn.execute("DELETE FROM fetch_runs WHERE started_at < ?", (cutoff,)).rowcount
    conn.execute("DELETE FROM documents WHERE checked_at < ?", (cutoff,))
    return pn, pr


def record_run(conn, *, started, code, total_notices=0, per_city=None, errors="", purged=(0, 0)):
    cur = conn.execute(
        """INSERT INTO fetch_runs(started_at,finished_at,outcome,exit_code,total_notices,per_city_json,
                                  errors,late_minutes,purged_notices,purged_runs)
           VALUES(?,?,?,?,?,?,?,?,?,?)""",
        (started.astimezone(TZ).isoformat(timespec="seconds"), now_iso(), outcome_for(code), code,
         total_notices, json.dumps(per_city or {}), errors or "", late_minutes(started), purged[0], purged[1]),
    )
    return cur.lastrowid


def latest_run(conn):
    conn.row_factory = sqlite3.Row
    r = conn.execute("SELECT * FROM fetch_runs ORDER BY id DESC LIMIT 1").fetchone()
    if not r:
        return None
    d = dict(r)
    d["per_city"] = json.loads(d.pop("per_city_json") or "{}")
    d["outcome_label"] = outcome_label(d)
    return d


def _parse_iso(s, default_tz=TZ):
    try:
        t = dt.datetime.fromisoformat(s)
        return t if t.tzinfo else t.replace(tzinfo=default_tz)
    except (TypeError, ValueError):
        return None


def _day(value, tz=TZ):
    if isinstance(value, dt.datetime):
        t = value if value.tzinfo else value.replace(tzinfo=tz)
        return t.astimezone(tz).date()
    t = _parse_iso(value, tz)
    return t.astimezone(tz).date() if t else None


def load_missing_pmn_pdfs(conn):
    """{url: row dict} of PMN PDFs that have 404'd on a previous run."""
    conn.row_factory = sqlite3.Row
    try:
        rows = {r["url"]: dict(r) for r in conn.execute("SELECT * FROM missing_pmn_pdfs")}
    except sqlite3.Error:
        rows = {}
    finally:
        conn.row_factory = None
    return rows


def save_missing_pmn_pdfs(conn, rows, recovered=None):
    """Persist in-memory missing-PDF state. recovered URLs are deleted (file is back)."""
    for url in recovered or []:
        conn.execute("DELETE FROM missing_pmn_pdfs WHERE url=?", (url,))
    for r in (rows or {}).values():
        conn.execute(
            """INSERT INTO missing_pmn_pdfs(
                   url, notice_url, first_404_at, last_404_at, last_checked_at,
                   consecutive_dates, status, permanent_logged)
               VALUES(?,?,?,?,?,?,?,?)
               ON CONFLICT(url) DO UPDATE SET
                 notice_url=excluded.notice_url,
                 first_404_at=excluded.first_404_at,
                 last_404_at=excluded.last_404_at,
                 last_checked_at=excluded.last_checked_at,
                 consecutive_dates=excluded.consecutive_dates,
                 status=excluded.status,
                 permanent_logged=excluded.permanent_logged""",
            (r["url"], r.get("notice_url") or "", r["first_404_at"], r["last_404_at"],
             r["last_checked_at"], int(r.get("consecutive_dates") or 1),
             r.get("status") or "watching", int(bool(r.get("permanent_logged")))),
        )


def should_skip_permanent_pdf(row, now):
    """True when this URL is permanently missing and not yet due for a weekly recheck."""
    if not row or (row.get("status") or "") != "permanent":
        return False
    last = _parse_iso(row.get("last_checked_at"))
    if not last:
        return False
    if last.tzinfo is None:
        last = last.replace(tzinfo=TZ)
    return (now - last) < dt.timedelta(days=PDF_404_RECHECK_DAYS)


def record_pdf_404(rows, url, notice_url, now):
    """Update in-memory missing-PDF state after a GET that returned HTTP 404.

    Returns the run-log line for this observation, or None to stay quiet (already
    reported as permanently missing). Consecutive dates are America/Denver calendar
    days; a second scan on the same date does not advance the count.
    """
    stamp = now.astimezone(TZ).isoformat(timespec="seconds")
    today = now.astimezone(TZ).date()
    row = rows.get(url)
    if not row:
        rows[url] = {
            "url": url,
            "notice_url": notice_url or "",
            "first_404_at": stamp,
            "last_404_at": stamp,
            "last_checked_at": stamp,
            "consecutive_dates": 1,
            "status": "watching",
            "permanent_logged": 0,
        }
        return PDF_404_SKIP_MSG.format(url=url)
    last_day = _day(row.get("last_404_at"))
    if last_day and last_day < today:
        row["consecutive_dates"] = int(row.get("consecutive_dates") or 1) + 1
    row["last_404_at"] = stamp
    row["last_checked_at"] = stamp
    if notice_url:
        row["notice_url"] = notice_url
    if int(row.get("consecutive_dates") or 1) >= PDF_404_PERMANENT_AFTER:
        row["status"] = "permanent"
        if not int(row.get("permanent_logged") or 0):
            row["permanent_logged"] = 1
            return PDF_404_PERM_MSG.format(url=url)
        return None
    return PDF_404_SKIP_MSG.format(url=url)

