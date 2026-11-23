"""Cost and safety guardrails (shared by the web app, the daily job and tests). Stdlib only.

Settings live in the app's `settings` table (key 'guard_<name>'); daily usage in `guard_usage`,
cap trips in `guard_trips`, the run lock in `guard_lock`. Days are America/Denver dates.

Defaults (2026-10-21) are about 2x the measured normal:
  * PMN requests/day 3000   - one 15-city daily run is <= ~1,400 GETs (1 entity list + 15 body lists +
                              77 notice lists + ~380 notice pages + ~900 conditional document GETs).
  * Alert emails/day 50     - at most one email per user per daily run; ~3 users today.
  * Resend sends/day 100    - sign-in links + alerts (a few per day today; Resend's free tier is 100/day).
  * Snapshot uploads/day 60 - 6-27/day measured with the old 20 s loop.
  * Container wakes/day 60  - 6-28 boots/day measured (each restore = one wake) before the bot filter.
Owner budget notices are exempt from the Resend cap (at most one per cap per day).
"""
import datetime as dt
import sqlite3
from zoneinfo import ZoneInfo

TZ = ZoneInfo("America/Denver")
PREFIX = "guard_"
LOCK_TTL_S = 30 * 60

# name: (default, min, max, label)
FIELDS = {
    "fetch_enabled":        (1, 0, 1, "Daily fetch (kill switch)"),
    "snapshot_interval_s":  (600, 60, 3600, "Snapshot check interval (seconds)"),
    "cap_pmn_requests":     (3000, 100, 20000, "PMN fetch requests per day"),
    "cap_alert_emails":     (50, 1, 5000, "Alert emails per day"),
    "cap_resend_sends":     (100, 10, 5000, "Resend sends per day (sign-in links + alerts)"),
    "cap_snapshot_uploads": (60, 24, 2000, "Snapshot uploads per day"),
    "cap_container_wakes":  (60, 4, 500, "Container wakes per day"),
}
# usage counter key for each cap
USAGE_FOR_CAP = {"cap_pmn_requests": "pmn_requests", "cap_alert_emails": "alert_emails",
                 "cap_resend_sends": "resend_sends", "cap_snapshot_uploads": "snapshot_uploads",
                 "cap_container_wakes": "container_wakes"}
WORKER_FIELDS = ("fetch_enabled", "cap_snapshot_uploads", "cap_container_wakes")  # mirrored to the Worker


def today(now=None):
    return (now or dt.datetime.now(TZ)).astimezone(TZ).date().isoformat()


def ensure_schema(con):
    con.execute("CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT)")
    con.execute("CREATE TABLE IF NOT EXISTS guard_usage(day TEXT NOT NULL, key TEXT NOT NULL, "
                "count INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(day, key))")
    con.execute("CREATE TABLE IF NOT EXISTS guard_trips(day TEXT NOT NULL, key TEXT NOT NULL, at TEXT NOT NULL, "
                "detail TEXT NOT NULL DEFAULT '', notified INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(day, key))")
    con.execute("CREATE TABLE IF NOT EXISTS guard_lock(name TEXT PRIMARY KEY, holder TEXT NOT NULL, "
                "acquired_at REAL NOT NULL)")


def parse_value(name, raw):
    """-> (value, error). Integers only, inside the field's range."""
    default, lo, hi, label = FIELDS[name]
    s = str(raw).strip().lower()
    if name == "fetch_enabled":
        if s in ("1", "on", "true", "yes"):
            return 1, None
        if s in ("0", "off", "false", "no"):
            return 0, None
        return None, f"{label}: must be on or off."
    try:
        v = int(s)
    except ValueError:
        return None, f"{label}: enter a whole number."
    if not lo <= v <= hi:
        return None, f"{label}: must be between {lo} and {hi}."
    return v, None


def get_all(con):
    """Every guard setting, stored value or default (invalid stored values fall back to default)."""
    ensure_schema(con)
    stored = dict(con.execute("SELECT key, value FROM settings WHERE key LIKE 'guard_%'").fetchall())
    out = {}
    for name, (default, *_r) in FIELDS.items():
        raw = stored.get(PREFIX + name)
        v, err = parse_value(name, raw) if raw is not None else (default, None)
        out[name] = default if err else v
    return out


def get(con, name):
    return get_all(con)[name]


def update(con, form, now_iso=None):
    """Partial save: only names present in `form` change; others keep their stored values.
    All-or-nothing: any invalid field -> nothing saved. Returns (values_after, errors)."""
    ensure_schema(con)
    changes, errors = {}, []
    for name in FIELDS:
        if name in form and form[name] is not None:
            v, err = parse_value(name, form[name])
            if err:
                errors.append(err)
            else:
                changes[name] = v
    if not errors:
        ts = now_iso or dt.datetime.now(TZ).isoformat(timespec="seconds")
        for name, v in changes.items():
            con.execute("INSERT INTO settings(key,value,updated_at) VALUES(?,?,?) ON CONFLICT(key) DO UPDATE "
                        "SET value=excluded.value, updated_at=excluded.updated_at", (PREFIX + name, str(v), ts))
    return get_all(con), errors


# ---------------------------------------------------------------- usage + caps
def usage(con, day=None):
    ensure_schema(con)
    return dict(con.execute("SELECT key, count FROM guard_usage WHERE day=?", (day or today(),)).fetchall())


def add(con, key, n=1, day=None):
    ensure_schema(con)
    d = day or today()
    con.execute("INSERT INTO guard_usage(day,key,count) VALUES(?,?,?) ON CONFLICT(day,key) "
                "DO UPDATE SET count=count+excluded.count", (d, key, n))
    return con.execute("SELECT count FROM guard_usage WHERE day=? AND key=?", (d, key)).fetchone()[0]


def remaining(con, cap_name, day=None):
    cap = get(con, cap_name)
    return cap - usage(con, day).get(USAGE_FOR_CAP[cap_name], 0)


def trip(con, key, detail="", day=None):
    """Record that `key` tripped today. Returns True the first time today (caller may notify)."""
    ensure_schema(con)
    cur = con.execute("INSERT OR IGNORE INTO guard_trips(day,key,at,detail) VALUES(?,?,?,?)",
                      (day or today(), key, dt.datetime.now(TZ).isoformat(timespec="seconds"), detail[:500]))
    return cur.rowcount == 1


def trips(con, day=None):
    ensure_schema(con)
    return [dict(zip(("key", "at", "detail", "notified"), r)) for r in con.execute(
        "SELECT key, at, detail, notified FROM guard_trips WHERE day=? ORDER BY at", (day or today(),))]


def claim_notice(con, key, day=None):
    """True exactly once per tripped key per day (so the owner gets at most one notice per cap per day)."""
    cur = con.execute("UPDATE guard_trips SET notified=1 WHERE day=? AND key=? AND notified=0", (day or today(), key))
    return cur.rowcount == 1


# ---------------------------------------------------------------- run lock (stale-safe)
def acquire_lock(con, holder, name="runs", ttl=LOCK_TTL_S, now=None):
    """Atomic. A lock older than `ttl` seconds is considered dead and is taken over."""
    ensure_schema(con)
    t = now if now is not None else dt.datetime.now(TZ).timestamp()
    con.execute("DELETE FROM guard_lock WHERE name=? AND acquired_at < ?", (name, t - ttl))
    cur = con.execute("INSERT OR IGNORE INTO guard_lock(name,holder,acquired_at) VALUES(?,?,?)", (name, holder, t))
    con.commit()
    return cur.rowcount == 1


def release_lock(con, holder, name="runs"):
    ensure_schema(con)
    con.execute("DELETE FROM guard_lock WHERE name=? AND holder=?", (name, holder))
    con.commit()


def lock_info(con, name="runs"):
    ensure_schema(con)
    r = con.execute("SELECT holder, acquired_at FROM guard_lock WHERE name=?", (name,)).fetchone()
    return {"holder": r[0], "acquired_at": r[1]} if r else None


def connect(path):
    con = sqlite3.connect(path, timeout=30)
    ensure_schema(con)
    return con


def refresh_lock(con, holder, name="runs", now=None):
    """Heartbeat: a live holder keeps its lock fresh so only a dead holder's lock goes stale."""
    t = now if now is not None else dt.datetime.now(TZ).timestamp()
    con.execute("UPDATE guard_lock SET acquired_at=? WHERE name=? AND holder=?", (t, name, holder))
    con.commit()
