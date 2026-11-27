"""Agenda Watch — step 3 web app (magic-link sign-in, watch, history, Stripe Checkout).

Build rules enforced here:
  * Email ONLY the owner (secret AW_OWNER_EMAIL). Any other address: link is stored in
    outbox/magic/ and printed to the server log; nothing is sent.
  * No SMS, no ads. Stripe: live on Cloudflare (AW_CLOUD=1), test on the box. Cities: the ten supported Utah cities in config.json
    (state -> metro -> city, stable keys; see aw_cities.py). No others.
  * Watch storage: watches (keywords, alerts) + watch_cities (email, city_key, enabled, street).
  * Free: one city + one optional street on that city (other cities disabled).
  * Paid ($5/month per extra city, Stripe, subscription quantity): 1 + quantity cities,
    capped at ten. Each city has its own optional street, matched only in that city.
"""
import json, os, re, secrets, sqlite3, smtplib, sys, time, logging
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi import FastAPI, Form, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse, PlainTextResponse
from fastapi.templating import Jinja2Templates
from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired
from starlette.middleware.sessions import SessionMiddleware

import aw_alerts
import aw_cities
import aw_guard
import aw_store
from web import stripe_billing as billing

ROOT = Path(__file__).resolve().parent.parent
CFG = aw_cities.load_config(ROOT / "config.json")
# Identity comes from secrets AW_OWNER_EMAIL and AW_ADMIN_EMAILS (see aw_cities). Missing
# admin secret => no one is admin (fail closed). Tests and operators may assign OWNER /
# ADMIN_EMAILS on this module; is_admin() always re-reads the env so a missing secret denies access.
OWNER = aw_cities.owner_email()
ADMIN_EMAILS = aw_cities.admin_emails()
CITY_LIST = aw_cities.city_list(CFG)                       # exactly the ten supported cities
CITIES = [c["name"] for c in CITY_LIST]
CITY_KEYS = [c["key"] for c in CITY_LIST]
CITY_NAME = {c["key"]: c["name"] for c in CITY_LIST}
METRO_GROUPS = aw_cities.metro_groups(CFG)
MAX_CITIES = min(aw_cities.MAX_CITIES, len(CITY_KEYS))
LEGACY_WATCH_COLUMNS = ("city_ch", "city_slc", "street_ch", "street_slc", "address", "address2")
DEFAULT_KEYWORDS = CFG["keywords"]
DB = Path(os.environ.get("AW_DB") or (ROOT / "data" / "agenda_watch.db"))  # AW_DB: tests only
OUTBOX = ROOT / "outbox" / "magic"
HISTORY_JSON = ROOT / "state" / "sent_history.json"
def base_url() -> str:
    """Resolve public base URL each call (.env first, then process env) for tunnel deploys."""
    v = None
    env_path = Path(__file__).resolve().parents[1] / ".env"
    if env_path.is_file():
        for line in env_path.read_text().splitlines():
            if line.startswith("AW_BASE_URL="):
                v = line.split("=", 1)[1].strip()
                break
    if not v:
        v = os.environ.get("AW_BASE_URL")
    return (v or "http://localhost:8080").rstrip("/")

LINK_TTL = 15 * 60
TZ = ZoneInfo("America/Denver")

log = logging.getLogger("agenda_watch")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

EXAMPLE = {
    "label": "Example from a past agenda",
    "city": "Cottonwood Heights",
    "body": "City Council work session",
    "meeting": "Tuesday, August 18, 2026, 4:00 PM MT",
    "item": "c. Short Term Rental Discussion, Jim Spung (30 min.)",
    "excerpt": "Work session agenda item: “Short Term Rental Discussion, Jim Spung (30 min.)”",
    "link": "https://www.utah.gov/pmn/files/1474715.pdf",
    "matched": "short-term rental",
}


def secret_key():
    p = ROOT / "data" / "secret.key"
    if not p.exists():
        p.write_text(secrets.token_hex(32)); p.chmod(0o600)
    return p.read_text().strip()


SECRET = secret_key()
signer = URLSafeTimedSerializer(SECRET, salt="aw-magic-link")


def db():
    c = sqlite3.connect(DB); c.row_factory = sqlite3.Row; return c


REF_COOKIE = "aw_ref"
REF_MAX_AGE = 30 * 24 * 3600
REF_MAX_LEN = 40
_REF_BOT = re.compile(r"bot\b|bot/|crawler|spider|slurp|facebookexternalhit|facebot|embedly|preview|whatsapp|"
                      r"telegram|skype|discord|slack|linkedin|pinterest|headless|python-requests|curl/|wget|"
                      r"go-http-client|okhttp|scrapy|httpclient", re.I)


def clean_ref(value):
    """Sanitize a ?ref= code: lowercase [a-z0-9-], at most 40 chars. None when nothing usable is left."""
    if not isinstance(value, str):
        return None
    v = re.sub(r"[_\s]+", "-", value.strip().lower())
    v = re.sub(r"[^a-z0-9-]", "", v).strip("-")[:REF_MAX_LEN].strip("-")
    return v or None


def ref_signup_counts(conn=None):
    """{ref: new accounts}; accounts without a ref are counted as 'direct'. Counts only, no addresses."""
    def run(c):
        out = {}
        for r in c.execute("SELECT COALESCE(NULLIF(ref, ''), 'direct') AS r, COUNT(*) AS n FROM users GROUP BY r ORDER BY n DESC, r"):
            out[r["r"]] = r["n"]
        return out
    if conn is not None:
        return run(conn)
    with db() as c:
        return run(c)


def _ensure_column(conn, table, column, ddl):
    cols = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
    if column not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {ddl}")


def _migrate_watch_cities(conn):
    """One-time: fixed watches.city_ch/city_slc/street_ch/street_slc (and the older
    address/address2) -> one watch_cities row per (email, city_key). Copies, verifies the copy,
    then drops the legacy columns. Idempotent: does nothing once the columns are gone."""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(watches)")}
    legacy = [c for c in LEGACY_WATCH_COLUMNS if c in cols]
    if not legacy:
        return
    rows = [dict(r) for r in conn.execute("SELECT * FROM watches")]
    expected = {}
    for w in rows:
        state = aw_cities.legacy_watch_cities(w)
        expected[w["email"]] = state
        if conn.execute("SELECT 1 FROM watch_cities WHERE email=? LIMIT 1", (w["email"],)).fetchone():
            continue  # already has per-city rows; never overwrite them
        for key, v in state.items():
            if v["enabled"] or v["street"]:
                conn.execute(
                    "INSERT INTO watch_cities(email,city_key,enabled,street,updated_at) VALUES(?,?,?,?,?)",
                    (w["email"], key, int(v["enabled"]), v["street"][:200], w.get("updated_at") or now_iso()),
                )
    # Verify before dropping anything.
    for email, state in expected.items():
        got = {r["city_key"]: (bool(r["enabled"]), r["street"]) for r in conn.execute(
            "SELECT city_key, enabled, street FROM watch_cities WHERE email=?", (email,))}
        for key, v in state.items():
            if v["enabled"] or v["street"]:
                if got.get(key) != (v["enabled"], v["street"][:200]):
                    raise RuntimeError(f"watch_cities migration mismatch for {email}/{key}: {got.get(key)} != {v}")
    for col in legacy:
        conn.execute(f"ALTER TABLE watches DROP COLUMN {col}")
    log.info("Migrated %d watch row(s) to watch_cities; dropped %s", len(rows), ",".join(legacy))


def migrate_stripe_live(c):
    """One-time switch to LIVE Stripe (cloud + live key only). Every stored Stripe id came from the
    TEST account and is invalid in live mode: archive them into test_* columns, clear the live
    columns, extra_cities -> 0. Cities and streets are NOT touched here (no auto-disable at switch);
    the plan limit applies on the user's next watch save (save is refused while over the limit) or
    on their next live subscription event (downgrade keeps earliest-enabled cities)."""
    if not (billing.is_live() and billing.configured()):
        return False
    if c.execute("SELECT 1 FROM schema_migrations WHERE id='stripe_live_v1'").fetchone():
        return False
    c.execute("""UPDATE users SET
        test_stripe_customer_id=stripe_customer_id, test_stripe_subscription_id=stripe_subscription_id,
        test_subscription_status=subscription_status, test_extra_cities=extra_cities,
        stripe_customer_id=NULL, stripe_subscription_id=NULL, subscription_status=NULL,
        extra_cities=0, billing_synced_at=NULL
        WHERE stripe_customer_id IS NOT NULL OR stripe_subscription_id IS NOT NULL
           OR subscription_status IS NOT NULL OR COALESCE(extra_cities,0) != 0""")
    c.execute("INSERT INTO schema_migrations(id, applied_at) VALUES('stripe_live_v1', ?)", (now_iso(),))
    log.warning("stripe_live_v1: archived test-mode Stripe ids; cities left unchanged")
    return True


def get_setting(key):
    with db() as c:
        r = c.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return r[0] if r else None


def set_setting(key, value):
    with db() as c:
        c.execute("INSERT INTO settings(key,value,updated_at) VALUES(?,?,?) "
                  "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
                  (key, value, now_iso()))


LIVE_SETTING_KEYS = {"price_id": "stripe_live_price_id", "product_id": "stripe_live_product_id",
                     "portal_config_id": "stripe_live_portal_config_id"}


def load_live_catalog_ids():
    if billing.is_live():
        for k, sk in LIVE_SETTING_KEYS.items():
            v = get_setting(sk)
            if v:
                billing.LIVE_IDS[k] = v


def ensure_live_catalog_now():
    """Cloud + live key: idempotently ensure product/price/portal in Stripe and store ids in settings."""
    if not (billing.is_live() and billing.configured()):
        return None
    ids = billing.ensure_live_catalog()
    for k, v in ids.items():
        set_setting(LIVE_SETTING_KEYS[k], v)
    log.info("Live Stripe catalog ready: %s", ids)
    return ids


def init_db():
    with db() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS users(
          email TEXT PRIMARY KEY, created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS watches(
          email TEXT PRIMARY KEY REFERENCES users(email),
          keywords TEXT NOT NULL,
          alerts_on INTEGER NOT NULL DEFAULT 0, updated_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS watch_cities(
          email TEXT NOT NULL REFERENCES watches(email),
          city_key TEXT NOT NULL,
          enabled INTEGER NOT NULL DEFAULT 0,
          street TEXT NOT NULL DEFAULT '',
          updated_at TEXT,
          PRIMARY KEY(email, city_key));
        CREATE TABLE IF NOT EXISTS magic_nonces(
          nonce TEXT PRIMARY KEY, email TEXT NOT NULL, created_at REAL NOT NULL, used_at REAL);
        CREATE TABLE IF NOT EXISTS alerts_sent(
          id INTEGER PRIMARY KEY AUTOINCREMENT, email TEXT NOT NULL, sent_date TEXT NOT NULL,
          city TEXT NOT NULL, item TEXT NOT NULL, link TEXT NOT NULL, meeting TEXT);
        """)
        _ensure_column(c, "users", "stripe_customer_id", "stripe_customer_id TEXT")
        _ensure_column(c, "users", "stripe_subscription_id", "stripe_subscription_id TEXT")
        _ensure_column(c, "users", "subscription_status", "subscription_status TEXT")
        _ensure_column(c, "users", "paid_updated_at", "paid_updated_at TEXT")
        # Paid extra cities = Stripe subscription quantity ($5/month each).
        _ensure_column(c, "users", "extra_cities", "extra_cities INTEGER")
        # Epoch seconds of the last live re-read of the subscription from the Stripe API.
        _ensure_column(c, "users", "billing_synced_at", "billing_synced_at REAL")
        _migrate_watch_cities(c)
        try:                     # city agendas pages as alert sources (owner order Oct 31 2026): table + CH seed
            import aw_city_sources
            aw_city_sources.ensure(c)
        except Exception as ex:  # noqa: BLE001 - never block startup
            log.error("city_sources table not ready: %r", ex)
        # When each city was last switched on (downgrade keeps the earliest-on cities).
        _ensure_column(c, "watch_cities", "enabled_at", "enabled_at TEXT")
        # City lock: locked_at = when this city was turned on (or locked by city_lock_v1); a city on with
        # locked_at after the user's last unlock date cannot be turned off/swapped until the next one.
        # blocked_at = switched off by a plan downgrade; cannot be turned back on until the next unlock date.
        _ensure_column(c, "watch_cities", "locked_at", "locked_at TEXT")
        _ensure_column(c, "watch_cities", "blocked_at", "blocked_at TEXT")
        # Admin flag (default 0) + one-time migration that makes the owner an admin.
        _ensure_column(c, "users", "is_admin", "is_admin INTEGER NOT NULL DEFAULT 0")
        c.execute("CREATE TABLE IF NOT EXISTS schema_migrations(id TEXT PRIMARY KEY, applied_at TEXT NOT NULL)")
        # Admin accounts come from secret AW_ADMIN_EMAILS (comma-separated). If the
        # secret is missing, no one is granted admin (fail closed). The owner row is a
        # standard user unless that address is also listed in AW_ADMIN_EMAILS.
        # Accounts listed there exist (created if missing) and are marked admin in the DB.
        admins = aw_cities.admin_emails()
        owner = aw_cities.owner_email()
        for a in admins:
            c.execute("INSERT OR IGNORE INTO users(email, created_at) VALUES(?, ?)", (a, now_iso()))
            c.execute("UPDATE users SET is_admin=1 WHERE email=?", (a,))
        if not c.execute("SELECT 1 FROM schema_migrations WHERE id='admin_swap_20261022'").fetchone():
            if owner and owner not in admins:
                c.execute("UPDATE users SET is_admin=0 WHERE email=?", (owner,))
            c.execute("INSERT INTO schema_migrations(id, applied_at) VALUES('admin_swap_20261022', ?)", (now_iso(),))
        aw_store.ensure_schema(c)
        aw_alerts.ensure_schema(c)
        # Runs that sent per-user alert emails but were logged 'silence' (before 2026-10-27 fix).
        _fixed = aw_store.reconcile_alert_outcomes(c)
        if _fixed:
            log.info("fetch_runs outcome corrected to alerts_sent for run id(s) %s", _fixed)
        # Minutes already emailed before minutes had an identity key (idempotent; see MINUTES_BACKFILL).
        _bf = aw_alerts.backfill_minutes_keys(c)
        if _bf:
            log.info("minutes identity backfill: %d sent row(s) added", _bf)
        if _fixed or _bf:
            try:
                (Path(__file__).resolve().parents[1] / "state" / ".persist-now").touch()
            except OSError:
                pass
        c.execute("CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT)")
        for col in ("test_stripe_customer_id", "test_stripe_subscription_id", "test_subscription_status"):
            _ensure_column(c, "users", col, f"{col} TEXT")
        _ensure_column(c, "users", "test_extra_cities", "test_extra_cities INTEGER")
        # Cancel at period end: still paid until Stripe ends the subscription (status canceled).
        _ensure_column(c, "users", "cancel_at", "cancel_at REAL")          # epoch secs, NULL = not canceling
        _ensure_column(c, "users", "period_end", "period_end REAL")        # current paid period ends
        # Signup attribution (2026-11-27): the sanitized ?ref= code of the first visit, set once when a NEW
        # account is created. Nothing else is stored (no IP, no user agent). Additive columns only.
        _ensure_column(c, "users", "ref", "ref TEXT")
        _ensure_column(c, "magic_nonces", "ref", "ref TEXT")
        migrate_stripe_live(c)
        if not c.execute("SELECT 1 FROM schema_migrations WHERE id='city_lock_v1'").fetchone():
            # Existing enabled cities on all accounts count as locked starting now.
            ts = now_iso()
            c.execute("UPDATE watch_cities SET locked_at=? WHERE enabled=1", (ts,))
            c.execute("INSERT INTO schema_migrations(id, applied_at) VALUES('city_lock_v1', ?)", (ts,))
        c.execute("""CREATE TABLE IF NOT EXISTS feedback(
          id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT NOT NULL,
          user_id TEXT REFERENCES users(email),         -- signed-in user's email, NULL if anonymous
          email TEXT NOT NULL DEFAULT '', category TEXT NOT NULL, city TEXT NOT NULL DEFAULT '',
          link TEXT NOT NULL DEFAULT '', message TEXT NOT NULL,
          status TEXT NOT NULL DEFAULT 'new' CHECK(status IN ('new','needs-owner','replied','reviewed')),
          replied_at TEXT, reply_text TEXT, owner_note TEXT)""")
        c.execute("CREATE INDEX IF NOT EXISTS feedback_created ON feedback(created_at)")
        _migrate_feedback_handling(c)
        # Backfill: enabled rows with no enabled_at get their last-saved time (ties -> config order).
        c.execute("UPDATE watch_cities SET enabled_at=updated_at "
                  "WHERE enabled=1 AND enabled_at IS NULL AND updated_at IS NOT NULL")


def _migrate_feedback_handling(c):
    """Widen feedback.status and add reply columns. Existing rows and statuses are kept."""
    if c.execute("SELECT 1 FROM schema_migrations WHERE id='feedback_handling_20261112'").fetchone():
        return
    sql = (c.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='feedback'").fetchone() or [""])[0] or ""
    cols = {r[1] for r in c.execute("PRAGMA table_info(feedback)")}
    need = "needs-owner" not in sql or "replied_at" not in cols or "reply_text" not in cols or "owner_note" not in cols
    if need:
        c.execute("""CREATE TABLE feedback_new(
          id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT NOT NULL,
          user_id TEXT REFERENCES users(email),
          email TEXT NOT NULL DEFAULT '', category TEXT NOT NULL, city TEXT NOT NULL DEFAULT '',
          link TEXT NOT NULL DEFAULT '', message TEXT NOT NULL,
          status TEXT NOT NULL DEFAULT 'new' CHECK(status IN ('new','needs-owner','replied','reviewed')),
          replied_at TEXT, reply_text TEXT, owner_note TEXT)""")
        have_r_at = "replied_at" in cols
        have_r_tx = "reply_text" in cols
        have_note = "owner_note" in cols
        c.execute(
            f"""INSERT INTO feedback_new(id,created_at,user_id,email,category,city,link,message,status,
                   replied_at,reply_text,owner_note)
                SELECT id,created_at,user_id,email,category,city,link,message,status,
                   {"replied_at" if have_r_at else "NULL"},
                   {"reply_text" if have_r_tx else "NULL"},
                   {"owner_note" if have_note else "NULL"}
                FROM feedback""")
        c.execute("DROP TABLE feedback")
        c.execute("ALTER TABLE feedback_new RENAME TO feedback")
        c.execute("CREATE INDEX IF NOT EXISTS feedback_created ON feedback(created_at)")
        mx = c.execute("SELECT MAX(id) FROM feedback").fetchone()[0]
        if mx:
            c.execute("DELETE FROM sqlite_sequence WHERE name='feedback'")
            c.execute("INSERT INTO sqlite_sequence(name, seq) VALUES('feedback', ?)", (mx,))
    c.execute("INSERT INTO schema_migrations(id, applied_at) VALUES('feedback_handling_20261112', ?)", (now_iso(),))


def clock():
    """Current time (aware, Denver). Single injectable clock for lock/unlock logic and timestamps;
    tests monkeypatch webapp.clock to freeze time."""
    return datetime.now(TZ)


def now_iso():
    return clock().astimezone(TZ).isoformat(timespec="seconds")


EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

# ---------------- email transport ----------------
RESEND_URL = "https://api.resend.com/emails"
RESEND_FROM = "Agenda Watch <alerts@agendawatch.app>"
RESEND_REPLY_TO = "hello@agendawatch.app"


def email_provider() -> str:
    """'resend' when RESEND_API_KEY is set (any recipient); 'cloudflare' relay (owner only);
    'smtp' / 'outbox' on the box."""
    if (os.environ.get("RESEND_API_KEY") or "").strip():
        return "resend"
    if os.environ.get("AW_EMAIL_RELAY_URL"):
        return "cloudflare"
    return "smtp" if os.environ.get("AW_SMTP_HOST") else "outbox"


def owner_inbox() -> str:
    """Mail meant for the owner goes to the project inbox (as with the Cloudflare relay)."""
    return (os.environ.get("NOTIFY_TO") or "").strip() or OWNER


LAST_EMAIL: dict = {}   # last send outcome, no address / key (for /internal/status diagnostics)


def _record_email(ok, provider, http=None, detail=""):
    LAST_EMAIL.clear()
    LAST_EMAIL.update({"at": now_iso(), "ok": ok, "provider": provider, "http": http, "detail": detail[:300]})


def _send_via_resend(to, subject, body, redirect_owner=True, budget_exempt=False, mail_from=None, html=None):
    """One attempt, no retries. Never logs or returns the API key. Returns (sent, how).
    redirect_owner: mail addressed to the owner goes to the project inbox (run notices, guard notices).
    Sign-in links and per-user alerts pass False: they go to the address itself.
    Per-user alerts pass False: every user, the owner included, gets alerts at their own address.
    Daily Resend cap (aw_guard cap_resend_sends) covers sign-in links + alerts + run notices; only the
    owner's once-a-day budget notices (budget_exempt) bypass it.
    mail_from: optional From header; defaults to RESEND_FROM (alerts@). Feedback replies pass
    FEEDBACK_REPLY_FROM (hello@). Do not change the default — alerts and sign-in stay on alerts@."""
    import urllib.request, urllib.error
    if not budget_exempt:
        with db() as c:
            left = aw_guard.remaining(c, "cap_resend_sends")
            cap = aw_guard.get(c, "cap_resend_sends")
            if left <= 0:
                aw_guard.trip(c, "cap_resend_sends", f"{cap} Resend sends reached")
        if left <= 0:
            _record_email(False, "resend", None, "daily Resend cap reached")
            log.error("Resend send skipped: daily cap %s reached", cap)
            guard_notify()
            return False, f"send FAILED: daily email limit reached ({cap}/day)"
        with db() as c:
            aw_guard.add(c, "resend_sends")
    key = (os.environ.get("RESEND_API_KEY") or "").strip()
    rcpt = owner_inbox() if (redirect_owner and to.strip().lower() == OWNER) else to.strip()
    req = urllib.request.Request(
        RESEND_URL, method="POST",
        data=json.dumps({k: v for k, v in {
            "from": (mail_from or RESEND_FROM), "to": [rcpt], "reply_to": RESEND_REPLY_TO,
            "subject": subject, "text": body, "html": html,
        }.items() if v is not None}).encode(),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}",
                 "User-Agent": "AgendaWatch-Container/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            out = json.loads(r.read() or b"{}")
        _record_email(True, "resend", 200, out.get("id", ""))
        return True, f"sent via Resend ({out.get('id', '?')})"
    except urllib.error.HTTPError as ex:
        try:
            detail = json.loads(ex.read() or b"{}").get("message", "")
        except Exception:  # noqa: BLE001
            detail = ""
        detail = str(detail).replace(key, "[redacted]")[:200] if key else str(detail)[:200]
        _record_email(False, "resend", ex.code, detail)
        log.error("Resend send failed: HTTP %s %s", ex.code, detail)
        return False, f"send FAILED via Resend: HTTP {ex.code} {detail}".strip()
    except Exception as ex:  # noqa: BLE001
        _record_email(False, "resend", None, f"{type(ex).__name__}: {str(ex)[:200]}".replace(key, "[redacted]"))
        log.error("Resend send failed: %s", LAST_EMAIL["detail"])
        return False, f"send FAILED via Resend: {type(ex).__name__}"


def send_email(to, subject, body, mail_from=None):
    """Outbound mail. With Resend: any address. Without: owner only (Cloudflare relay / SMTP).
    mail_from is Resend-only and optional; omitted callers keep RESEND_FROM (alerts@)."""
    if email_provider() == "resend":
        # Sign-in links go to the address that was typed, the owner's included (2026-10-22 fix: the
        # owner's links had been redirected to the project inbox, so chris@ never received them).
        kw = {"mail_from": mail_from} if mail_from else {}
        return _send_via_resend(to, subject, body, redirect_owner=False, **kw)
    return send_owner_email(to, subject, body)


def send_alert_email(to, subject, body, html=None):
    """Per-user alert to the user's OWN address (no owner redirect). Resend only.
    Returns (ok, resend_id_or_None, detail). html is optional email-safe markup; text is always sent."""
    if email_provider() != "resend":
        return False, None, f"not sent: provider {email_provider()} (per-user alerts need Resend)"
    ok, how = _send_via_resend(to, subject, body, redirect_owner=False, html=html)
    rid = how.rsplit("(", 1)[-1].rstrip(")") if ok and "(" in how else None
    return ok, rid, how


def send_owner_email(to, subject, body, budget_exempt=False):
    """Send ONLY to the owner. Returns (sent: bool, how: str). Refuses everyone else."""
    if to.strip().lower() != OWNER:
        raise PermissionError("Build rule: email only the owner until board approves.")
    if email_provider() == "resend":
        if budget_exempt:
            return _send_via_resend(to, subject, body, budget_exempt=True)
        return _send_via_resend(to, subject, body)
    relay = os.environ.get("AW_EMAIL_RELAY_URL")  # Cloudflare deploy only; unset on the box
    if relay:
        return _send_via_relay(relay, subject, body)
    host = os.environ.get("AW_SMTP_HOST")
    if not host:
        return False, "queued (no SMTP configured; outbox only)"
    msg = EmailMessage(); msg["From"] = os.environ.get("AW_SMTP_FROM", OWNER)
    msg["To"] = OWNER; msg["Subject"] = subject; msg.set_content(body)
    with smtplib.SMTP(host, int(os.environ.get("AW_SMTP_PORT", "587"))) as s:
        s.starttls(); s.login(os.environ["AW_SMTP_USER"], os.environ["AW_SMTP_PASS"]); s.send_message(msg)
    return True, "sent via SMTP"


def _send_via_relay(relay, subject, body):
    """POST to the Cloudflare Worker, which sends with its send_email binding (owner only)."""
    import urllib.request
    req = urllib.request.Request(
        relay, method="POST",
        data=json.dumps({"to": OWNER, "subject": subject, "text": body}).encode(),
        headers={"Content-Type": "application/json",
                 "X-AW-Internal-Secret": os.environ["AW_INTERNAL_SECRET"],
                 "User-Agent": "AgendaWatch-Container/1.0"})
    with urllib.request.urlopen(req, timeout=30) as r:
        out = json.loads(r.read() or b"{}")
    return True, f"sent via Cloudflare send_email ({out.get('messageId', '?')})"


def issue_magic_link(email, ref=None):
    nonce = secrets.token_urlsafe(16)
    with db() as c:
        c.execute("INSERT INTO magic_nonces(nonce,email,created_at,ref) VALUES(?,?,?,?)",
                  (nonce, email, time.time(), clean_ref(ref)))
    token = signer.dumps({"e": email, "n": nonce})
    link = f"{base_url()}/auth/verify?token={token}"
    is_owner = email == OWNER
    can_email = is_owner or email_provider() == "resend"
    subject = "Your Agenda Watch sign-in link"
    body = (f"Sign in to Agenda Watch:\n\n{link}\n\nThis link works once and expires in 15 minutes.\n"
            "If you didn't ask for it, ignore this email.\n\n"
            "Agenda Watch flags public agenda text. It is not legal advice.\n")
    status = "stored only (non-owner: no email sent during build)"
    if can_email and not _signin_send_allowed(email):
        status = "not sent (too many sign-in emails; try again later)"
    elif can_email:
        try:
            sent, how = send_email(email, subject, body)
            status = how
        except Exception as ex:  # transport failure — do not silently skip
            status = f"send FAILED: {type(ex).__name__} (link still in outbox)"
    ts = datetime.now(TZ).strftime("%Y%m%d-%H%M%S")
    rec = {"created": now_iso(), "to": email, "is_owner": is_owner, "subject": subject,
           "body": body, "link": link, "expires_in_s": LINK_TTL, "status": status,
           "email_allowed": is_owner}
    OUTBOX.mkdir(parents=True, exist_ok=True)
    (OUTBOX / f"magic-{ts}-{nonce[:6]}.json").write_text(json.dumps(rec, indent=2))
    if email_provider() == "resend":
        # Real users get real links: never put a working link in the logs.
        log.info("Sign-in link for %s [%s]", email, status)
    else:
        log.info("MAGIC LINK for %s [%s]: %s", email, status, link)
        print(f"\n=== MAGIC LINK ({email}) [{status}] ===\n{link}\n", flush=True)
    return link, status


_signin_sends: dict = {}
SIGNIN_MAX_PER_ADDR = 3        # per 15 minutes per address (no retry storms / abuse)


def _signin_send_allowed(email):
    now = time.time()
    hits = [t for t in _signin_sends.get(email, []) if now - t < 900]
    if len(hits) >= SIGNIN_MAX_PER_ADDR:
        _signin_sends[email] = hits
        return False
    hits.append(now)
    _signin_sends[email] = hits
    return True


def verify_token(token, cookie_ref=None):
    try:
        data = signer.loads(token, max_age=LINK_TTL)
    except SignatureExpired:
        return None, "This sign-in link expired. Request a new one."
    except BadSignature:
        return None, "This sign-in link is not valid."
    with db() as c:
        row = c.execute("SELECT * FROM magic_nonces WHERE nonce=?", (data["n"],)).fetchone()
        if not row or row["email"] != data["e"]:
            return None, "This sign-in link is not valid."
        if row["used_at"]:
            return None, "This sign-in link was already used. Request a new one."
        c.execute("UPDATE magic_nonces SET used_at=? WHERE nonce=?", (time.time(), data["n"]))
        made = c.execute("INSERT OR IGNORE INTO users(email,created_at) VALUES(?,?)", (data["e"], now_iso()))
        if made.rowcount == 1:
            # NEW account only: remember where it came from (the ref saved when the link was requested,
            # else the cookie on this browser). An existing user's ref is never touched.
            first = clean_ref(row["ref"]) or clean_ref(cookie_ref)
            if first:
                c.execute("UPDATE users SET ref=? WHERE email=? AND ref IS NULL", (first, data["e"]))
        # Brand-new watch: default keywords, NO city pre-selected (the user picks on My watch).
        c.execute(
            "INSERT OR IGNORE INTO watches(email,keywords,alerts_on,updated_at) VALUES(?,?,0,?)",
            (data["e"], "\n".join(DEFAULT_KEYWORDS), now_iso()),
        )
    return data["e"], None


# ---------------- app ----------------
app = FastAPI(title="Agenda Watch", docs_url=None, redoc_url=None, openapi_url=None)
app.add_middleware(SessionMiddleware, secret_key=SECRET, session_cookie="aw_session",
                   max_age=14 * 24 * 3600, same_site="lax", https_only=False)
T = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
init_db()
with db() as _c:
    aw_guard.ensure_schema(_c)
load_live_catalog_ids()


@app.middleware("http")
async def _snapshot_after_webhook(request: Request, call_next):
    """Stripe webhooks change billing state: snapshot right away (persist.py sees the flag within ~1 s)."""
    resp = await call_next(request)
    if request.method == "POST" and request.url.path == "/stripe/webhook" and resp.status_code < 400:
        try:
            (ROOT / "state" / ".persist-now").touch()
        except OSError:
            pass
    return resp


REF_PAGES = ("/", "/signin")


@app.middleware("http")
async def _ref_cookie(request: Request, call_next):
    """First-touch attribution cookie for public pages loaded with ?ref=<code>. In production the Worker
    sets it for the cached public pages; this covers pages the app serves itself. SameSite=Lax, 30 days,
    only the code is stored, and an existing cookie is never replaced."""
    resp = await call_next(request)
    try:
        path = request.url.path
        if (request.method in ("GET", "HEAD") and resp.status_code < 400
                and (path in REF_PAGES or path.startswith("/cities/"))
                and not clean_ref(request.cookies.get(REF_COOKIE))
                and not _REF_BOT.search(request.headers.get("user-agent") or "")):
            code = clean_ref(request.query_params.get("ref"))
            if code:
                secure = request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https"
                resp.set_cookie(REF_COOKIE, code, max_age=REF_MAX_AGE, path="/", samesite="lax",
                                httponly=True, secure=secure)
    except Exception:  # noqa: BLE001 - attribution must never break a page
        log.exception("ref cookie failed")
    return resp


@app.on_event("startup")
def _guard_startup():
    if os.environ.get("AW_CLOUD") == "1" and os.environ.get("AW_PERSIST_URL"):
        __import__("threading").Thread(target=sync_guard_to_worker, daemon=True).start()


@app.on_event("startup")
def _live_catalog_startup():
    if billing.is_live() and billing.configured():
        def go():
            try:
                ensure_live_catalog_now()
            except Exception:
                log.exception("Live Stripe catalog setup failed (checkout will retry lazily)")
        __import__("threading").Thread(target=go, daemon=True).start()


def ctx(request, **kw):
    s = request.session
    if "csrf" not in s:
        s["csrf"] = secrets.token_urlsafe(16)
    stripe_ok = billing.configured()
    return {"request": request, "user": s.get("email"), "csrf": s["csrf"], "owner": OWNER,
            "is_admin": is_admin(s.get("email")),
            "stripe_configured": stripe_ok, **kw}


def check_csrf(request, token):
    return token and secrets.compare_digest(token, request.session.get("csrf", ""))


# ---- public city pages (owner order 2026-11-03): one per catalog city, no login, nothing listed
CITY_PAGES = {p["slug"]: p for p in aw_cities.city_pages(CFG)}
SITE_URL = "https://agendawatch.app"  # fixed apex; canonical and sitemap never use the request Host


@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    return T.TemplateResponse(request, "index.html", ctx(request, ex=EXAMPLE, keywords=DEFAULT_KEYWORDS,
                                                          metro_groups=METRO_GROUPS, city_count=len(CITY_LIST),
                                                          area_phrase=aw_cities.area_phrase(CFG),
                                                          canonical=SITE_URL + "/"))


@app.get("/cities/{slug}", response_class=HTMLResponse)
def city_page(request: Request, slug: str):
    page = CITY_PAGES.get(slug)
    if not page:
        return PlainTextResponse("Not found", status_code=404)
    return T.TemplateResponse(request, "city.html", ctx(request, city_name=page["name"],
                                                        canonical=f"{SITE_URL}/cities/{slug}"))


@app.api_route("/cities/{slug}/", methods=["GET", "HEAD"])
def city_page_slash(request: Request, slug: str):
    """301 /cities/<slug>/ to the sitemap form (no trailing slash). Query string is kept."""
    loc = f"/cities/{slug}"
    if request.url.query:
        loc += "?" + request.url.query
    return RedirectResponse(loc, status_code=301)


@app.get("/sitemap.xml")
def sitemap():
    return Response(aw_cities.sitemap_xml(CFG, SITE_URL), media_type="application/xml")


@app.get("/signin", response_class=HTMLResponse)
def signin_form(request: Request):
    return T.TemplateResponse(request, "signin.html", ctx(request))


@app.post("/signin", response_class=HTMLResponse)
def signin_post(request: Request, email: str = Form(...), csrf: str = Form("")):
    if not check_csrf(request, csrf):
        return T.TemplateResponse(request, "signin.html", ctx(request, error="Form expired, try again."), status_code=400)
    email = email.strip().lower()
    if not EMAIL_RE.match(email):
        return T.TemplateResponse(request, "signin.html", ctx(request, error="Enter a valid email."), status_code=400)
    _, status = issue_magic_link(email, ref=request.cookies.get(REF_COOKIE))
    return T.TemplateResponse(request, "check_email.html", ctx(request, email=email, is_owner=(email == OWNER),
                                                              status=status, provider=email_provider()))


@app.get("/auth/verify")
def verify(request: Request, token: str = ""):
    email, err = verify_token(token, cookie_ref=request.cookies.get(REF_COOKIE))
    if err:
        return T.TemplateResponse(request, "signin.html", ctx(request, error=err), status_code=400)
    request.session.clear()
    request.session["email"] = email
    request.session["csrf"] = secrets.token_urlsafe(16)
    return RedirectResponse("/watch", status_code=303)


@app.post("/signout")
def signout(request: Request, csrf: str = Form("")):
    if check_csrf(request, csrf):
        request.session.clear()
    return RedirectResponse("/", status_code=303)


def is_admin(email) -> bool:
    """True only if email is listed in secret AW_ADMIN_EMAILS.

    A missing or empty secret denies everyone (fail closed), even if users.is_admin=1
    is still set in the database from an earlier run. The column is kept in sync at
    init_db but is not enough on its own.
    """
    if not email:
        return False
    allowed = aw_cities.admin_emails()
    if not allowed:
        return False
    return email.strip().lower() in allowed


def require_user(request):
    email = request.session.get("email")
    if email and not load_user(email):   # account deleted: its old session cookie no longer signs in
        request.session.clear()
        return None
    return email


def load_user(email):
    with db() as c:
        row = c.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
        return dict(row) if row else None


def load_watch(email):
    """watches row + cities: {city_key: {enabled, street}} for every supported city."""
    with db() as c:
        row = c.execute("SELECT * FROM watches WHERE email=?", (email,)).fetchone()
        if row is None:
            return None
        d = dict(row)
        cities = {k: {"enabled": False, "street": ""} for k in CITY_KEYS}
        for r in c.execute("SELECT city_key, enabled, street FROM watch_cities WHERE email=?", (email,)):
            if r["city_key"] in cities:
                cities[r["city_key"]] = {"enabled": bool(r["enabled"]), "street": r["street"] or ""}
        d["cities"] = cities
        return d


def user_is_paid(email) -> bool:
    u = load_user(email)
    if not u:
        return False
    return billing.is_paid_status(u.get("subscription_status"))


def extra_cities_for(u) -> int:
    """Paid extra cities (subscription quantity). Legacy paid rows with no quantity = 1."""
    if not u or not billing.is_paid_status(u.get("subscription_status")):
        return 0
    q = u.get("extra_cities")
    if q is None or int(q) < 1:
        q = 1
    return min(int(q), MAX_CITIES - 1)


DENVER = ZoneInfo("America/Denver")


def _add_months(d, n):
    import calendar
    m = d.month - 1 + n
    y, m = d.year + m // 12, m % 12 + 1
    return d.replace(year=y, month=m, day=min(d.day, calendar.monthrange(y, m)[1]))


def unlock_window(u, now=None):
    """(last_unlock, next_unlock) as aware Denver datetimes.
    Paid with a Stripe period end: the billing date (rolled forward monthly if stale).
    Otherwise (free / no Stripe date yet): the 1st of next month, Denver time."""
    now = (now or clock()).astimezone(DENVER)
    pe = u.get("period_end") if u else None
    if u and billing.is_paid_status(u.get("subscription_status")) and pe:
        # Stripe bills in whole UTC months from the anchor (Jan 31 -> Feb 28 -> Mar 31), so a stale
        # period_end is rolled forward as anchor + k months in UTC (no day drift, no DST hour shift).
        anchor = datetime.fromtimestamp(float(pe), timezone.utc)
        k = 0
        while _add_months(anchor, k) <= now:
            k += 1
        return _add_months(anchor, k - 1).astimezone(DENVER), _add_months(anchor, k).astimezone(DENVER)
    first = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    return first, _add_months(first, 1)


def city_locks(email, now=None):
    """{city_key: 'locked'|'blocked'} for this period, plus the next unlock datetime.
    Admin accounts (same check as /admin) are never locked or blocked."""
    last, nxt = unlock_window(load_user(email) or {}, now)
    out = {}
    if is_admin(email):
        return out, nxt
    with db() as c:
        for r in c.execute("SELECT city_key, enabled, locked_at, blocked_at FROM watch_cities WHERE email=?", (email,)):
            # Window is [last, nxt): a save at the exact unlock moment belongs to the new period.
            if r["enabled"] and r["locked_at"] and datetime.fromisoformat(r["locked_at"]) >= last:
                out[r["city_key"]] = "locked"
            elif not r["enabled"] and r["blocked_at"] and datetime.fromisoformat(r["blocked_at"]) >= last:
                out[r["city_key"]] = "blocked"
    return out, nxt


def allowed_cities(email) -> int:
    """1 free city + paid extra cities, capped at MAX_CITIES (10). Admins: every catalog city, free."""
    if is_admin(email):
        return len(CITY_KEYS)
    return min(1 + extra_cities_for(load_user(email)), MAX_CITIES)


def mark_user_paid(email, *, customer_id=None, subscription_id=None, status=None, extra_cities=None):
    """Upsert subscription fields on users. email must already exist."""
    with db() as c:
        u = c.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
        if not u:
            c.execute("INSERT INTO users(email,created_at) VALUES(?,?)", (email, now_iso()))
            u = c.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
        cust = customer_id or u["stripe_customer_id"]
        sub = subscription_id or u["stripe_subscription_id"]
        st = status if status is not None else u["subscription_status"]
        q = extra_cities if extra_cities is not None else u["extra_cities"]
        c.execute(
            "UPDATE users SET stripe_customer_id=?, stripe_subscription_id=?, "
            "subscription_status=?, extra_cities=?, paid_updated_at=? WHERE email=?",
            (cust, sub, st, q, now_iso(), email),
        )
    log.info("Subscription update email=%s status=%s sub=%s cust=%s extra_cities=%s", email, st, sub, cust, q)


SYNC_MAX_AGE = 60  # seconds; /watch and /billing re-read the subscription from Stripe when older than this


def plural_cities(n):
    return f"{n} extra cit{'y' if n == 1 else 'ies'}"


def enforce_city_limit(email):
    """Downgrade rule. If more cities are on than the plan allows (1 + extra, cap 10), keep the
    cities switched on earliest (enabled_at; unknown/legacy first, ties in config order) and
    switch the rest off. Streets are NEVER touched: they stay on their city rows, so turning a
    city back on restores its street. Never switches anything on. Returns keys switched off."""
    allowed = allowed_cities(email)
    order = {k: i for i, k in enumerate(CITY_KEYS)}
    with db() as c:
        rows = [dict(r) for r in c.execute(
            "SELECT city_key, enabled_at FROM watch_cities WHERE email=? AND enabled=1", (email,))]
        rows = [r for r in rows if r["city_key"] in order]
        if len(rows) <= allowed:
            return []
        rows.sort(key=lambda r: (r["enabled_at"] is not None, r["enabled_at"] or "", order[r["city_key"]]))
        off = [r["city_key"] for r in rows[allowed:]]
        ts = now_iso()
        for k in off:
            c.execute("UPDATE watch_cities SET enabled=0, enabled_at=NULL, locked_at=NULL, blocked_at=?, "
                      "updated_at=? WHERE email=? AND city_key=?", (ts, ts, email, k))
    log.info("Plan limit %d for %s: switched off %s (streets kept)", allowed, email, ",".join(off))
    return off


def apply_subscription_state(email, *, customer_id=None, subscription_id=None, status=None, quantity=None,
                             cancel_at=None, period_end=None):
    """Single place that writes subscription state (return sync, /watch sync, webhook, Checkout
    success). extra_cities = quantity while active/trialing, else 0. Then applies the downgrade
    rule. Returns a summary dict used for the on-page notice."""
    before = load_user(email) or {}
    was_paid = billing.is_paid_status(before.get("subscription_status"))
    was_extra = extra_cities_for(before)
    status = (status or "none").lower()
    paid = billing.is_paid_status(status)
    try:
        q = int(quantity) if quantity is not None else 1
    except (TypeError, ValueError):
        q = 1
    extra = max(1, q) if paid else 0
    mark_user_paid(email, customer_id=customer_id, subscription_id=subscription_id,
                   status=status, extra_cities=extra)
    # A pending cancel keeps the plan paid (status stays active) until Stripe ends the subscription.
    with db() as c:
        c.execute("UPDATE users SET cancel_at=?, period_end=? WHERE email=?",
                  (cancel_at if paid else None, period_end if paid else None, email))
    off = enforce_city_limit(email)
    now_extra = extra_cities_for(load_user(email))
    return {"email": email, "status": status, "paid": paid, "was_paid": was_paid, "extra": now_extra,
            "allowed": allowed_cities(email), "switched_off": off,
            "changed": bool(off) or was_paid != paid or was_extra != now_extra}


def billing_notice(res):
    if not res:
        return None
    off = ", ".join(CITY_NAME[k] for k in res["switched_off"])
    tail = f" Switched off: {off} (their streets are kept)." if off else ""
    if res["paid"]:
        u = load_user(res.get("email")) if res.get("email") else None
        if u and u.get("cancel_at"):
            return (f"Billing updated: cancels on {fmt_day(u['cancel_at'])}. "
                    f"Your {plural_cities(res['extra'])} stay{'s' if res['extra'] == 1 else ''} active until then.")
        return f"Billing updated: {plural_cities(res['extra'])} · up to {res['allowed']} cities.{tail}"
    if res["was_paid"]:
        return f"Subscription canceled: back to 1 city.{tail}"
    return f"Billing checked: Free plan, 1 city.{tail}"


def sync_billing(email, *, force=False):
    """Re-read the signed-in user's subscription from the Stripe API (no webhook needed) and
    apply it. Throttled to once per SYNC_MAX_AGE unless force. Returns summary or None."""
    if not billing.configured():
        return None
    u = load_user(email)
    if not u or not u.get("stripe_customer_id"):
        return None
    last = u.get("billing_synced_at")
    if not force and last and time.time() - float(last) < SYNC_MAX_AGE:
        return None
    with db() as c:  # stamp first so a Stripe outage can't make every page load slow
        c.execute("UPDATE users SET billing_synced_at=? WHERE email=?", (time.time(), email))
    try:
        sub = billing.fetch_current_subscription(u["stripe_customer_id"], u.get("stripe_subscription_id"))
    except Exception:
        log.exception("Billing sync failed for %s (left unchanged)", email)
        return None
    if sub is None:
        return apply_subscription_state(email, customer_id=u["stripe_customer_id"], status="none", quantity=0)
    f = billing.subscription_fields_from_obj(sub)
    return apply_subscription_state(
        email, customer_id=f.get("stripe_customer_id") or u["stripe_customer_id"],
        subscription_id=f.get("stripe_subscription_id"), status=f.get("subscription_status"),
        quantity=f.get("extra_cities"), cancel_at=f.get("cancel_at"), period_end=f.get("period_end"))


def load_history(email):
    with db() as c:
        rows = [dict(r) for r in c.execute(
            "SELECT sent_date AS date, city, item, link, meeting FROM alerts_sent WHERE email=? ORDER BY id DESC", (email,))]
    build_tests = []
    if email == OWNER and HISTORY_JSON.exists():
        # Real step-1 build-test email (Gmail id on record), sent to the owner only. Shown separately:
        # these were past meetings, so under current rules they are examples, not alerts.
        for r in json.loads(HISTORY_JSON.read_text()):
            if r.get("duplicate_of"):
                continue
            build_tests.append({k: r.get(k) for k in ("date", "city", "item", "link", "meeting")})
    return rows, build_tests


def plan_context(email, w=None):
    """Plan fields shared by My watch (short plan line) and Billing (full plan box)."""
    u = load_user(email) or {}
    w = w or load_watch(email)
    enabled_count = sum(1 for v in (w or {}).get("cities", {}).values() if v.get("enabled"))
    admin = is_admin(email)
    return dict(
        paid=user_is_paid(email), extra_cities=extra_cities_for(u), allowed=allowed_cities(email),
        max_cities=len(CITY_KEYS) if admin else MAX_CITIES, max_extra=billing.MAX_EXTRA_CITIES, admin_plan=admin,
        subscription_status=u.get("subscription_status") or "", enabled_count=enabled_count,
        has_customer=bool(u.get("stripe_customer_id")),
        product_name=billing.PRODUCT_NAME, price_label=billing.PRICE_LABEL,
        cancel_date=fmt_day(u.get("cancel_at")) if user_is_paid(email) else None,
        **_lock_ctx(email),
        renew_date=fmt_day(u.get("period_end")) if user_is_paid(email) and not u.get("cancel_at") else None,
    )


def _lock_ctx(email):
    """Lock wording for the watch page (never for admins).
    save_locks: show 'Saving locks your selected cities until <date>.' only when Save could actually
    change the city selection, i.e. NOT when every saved city is locked and the user is exactly at
    the plan limit (nothing to add, nothing allowed off)."""
    locks, nxt = city_locks(email)
    if is_admin(email):
        return {"locks": locks, "lock_date": None, "save_locks": False}
    with db() as c:
        on = [r["city_key"] for r in c.execute(
            "SELECT city_key FROM watch_cities WHERE email=? AND enabled=1", (email,)) if r["city_key"] in CITY_NAME]
    all_locked = bool(on) and all(locks.get(k) == "locked" for k in on)
    fully_locked = all_locked and len(on) == allowed_cities(email)
    return {"locks": locks, "lock_date": fmt_day(nxt.timestamp()), "save_locks": not fully_locked}


def fmt_day(epoch):
    """Epoch seconds -> 'November 14, 2026' in America/Denver (None-safe)."""
    if not epoch:
        return None
    d = datetime.fromtimestamp(float(epoch), ZoneInfo("America/Denver"))
    return f"{d:%B} {d.day}, {d.year}"


def watch_page(request, email, *, saved=0, errors=None, w=None, notice=None):
    w = w or load_watch(email)
    hist, build_tests = load_history(email)
    notice = notice or request.session.pop("notice", None)
    return T.TemplateResponse(
        request, "watch.html",
        ctx(request, w=w, saved=saved, errors=errors or [], history=hist, build_tests=build_tests,
            cities=CITIES, metro_groups=METRO_GROUPS, defaults=DEFAULT_KEYWORDS, notice=notice,
            **plan_context(email, w)),
        status_code=400 if errors else 200,
    )


def billing_page(request, email, *, errors=None, checkout=None, portal_err=None, notice=None):
    notice = notice or request.session.pop("notice", None)
    return T.TemplateResponse(
        request, "billing.html",
        ctx(request, errors=errors or [], checkout=checkout, portal_err=portal_err, notice=notice,
            **plan_context(email)),
        status_code=400 if errors else 200,
    )


@app.get("/watch", response_class=HTMLResponse)
def watch(request: Request, saved: int = 0):
    email = require_user(request)
    if not email:
        return RedirectResponse("/signin", status_code=303)
    res = sync_billing(email)  # cheap: at most once a minute; catches a closed portal tab
    notice = billing_notice(res) if res and res["changed"] else None
    return watch_page(request, email, saved=saved, notice=notice)


@app.get("/billing", response_class=HTMLResponse)
def billing_view(request: Request, checkout: str = ""):
    """Plan & billing page (moved off My watch). Same throttled Stripe re-read as /watch."""
    email = require_user(request)
    if not email:
        return RedirectResponse("/signin", status_code=303)
    res = sync_billing(email)
    notice = billing_notice(res) if res and res["changed"] else None
    return billing_page(request, email, checkout=checkout or None, notice=notice)


def parse_keywords_field(raw: str) -> list:
    """Parse keywords textarea. Prefer one-per-line; comma-split only for a single-line paste."""
    text = (raw or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if not text:
        return []
    parts = text.split("\n") if "\n" in text else re.split(r",+", text)
    return [p.strip() for p in parts if p.strip()][:30]


@app.post("/watch", response_class=HTMLResponse)
async def watch_save(request: Request):
    email = require_user(request)
    if not email:
        return RedirectResponse("/signin", status_code=303)
    f = await request.form()
    csrf = f.get("csrf") or ""
    existing = load_watch(email) or {"cities": {k: {"enabled": False, "street": ""} for k in CITY_KEYS}}
    # Unchecked checkboxes are ABSENT from a POST, so "missing" cannot mean "off"
    # unless the full section was on the page. Each section of the watch form carries
    # a hidden sentinel (keywords_submitted / cities_submitted / alerts_submitted).
    # If a sentinel is absent this is a partial POST: keep that section from the DB.
    cities_submitted = f.get("cities_submitted") is not None
    if not cities_submitted:
        cities = {k: dict(v) for k, v in existing["cities"].items()}
    else:
        cities = {}
        for k in CITY_KEYS:
            on = bool(f.get(f"city_{k}"))
            street = str(f.get(f"street_{k}") or "").strip()[:200]
            if not on and f.get(f"street_{k}") is None:
                # A switched-off city's street input is disabled in the browser, so it is not
                # posted. Keep the stored street so turning the city back on restores it
                # (e.g. after a plan downgrade switched the city off).
                cities[k] = {"enabled": False, "street": existing["cities"][k]["street"]}
                continue
            # Street is saved on its own city row only, and only while that city is on.
            cities[k] = {"enabled": on, "street": street if on else ""}
    if f.get("alerts_submitted") is not None:
        on_alerts = int(bool(f.get("alerts_on")))
    else:
        on_alerts = int(bool(existing.get("alerts_on")))
    # FastAPI maps empty Optional form fields to None, so we cannot use Form(None)
    # on keywords alone to tell "cleared" from "omitted".
    if f.get("keywords_submitted") is None:
        kws = parse_keywords_field(existing.get("keywords") or "")
    else:
        kws = parse_keywords_field(str(f.get("keywords") or ""))
    enabled = [k for k in CITY_KEYS if cities[k]["enabled"]]
    allowed = allowed_cities(email)
    paid = user_is_paid(email)
    form = {"cities": cities, "keywords": "\n".join(kws), "alerts_on": on_alerts, "email": email}
    errors = []
    if not csrf or not check_csrf(request, csrf):
        errors.append("Form expired. Reload the page and try again.")
    if cities_submitted and len(enabled) > allowed:
        if not paid:
            errors.append(
                "The free plan watches one city. Turn off the other cities, "
                f"or add an extra city on the Billing page ({billing.PRICE_LABEL})."
            )
        else:
            errors.append(
                f"Your plan allows {allowed} cities (1 free + {allowed - 1} extra at $5/month each) "
                f"and you turned on {len(enabled)}. Turn some off"
                + (", or add extra cities on the Billing page." if allowed < MAX_CITIES else ".")
            )
    if cities_submitted:
        locks, unlock_at = city_locks(email)
        was_on = [k for k in CITY_KEYS if existing["cities"][k]["enabled"]]
        when = fmt_day(unlock_at.timestamp())
        # Over the plan limit (e.g. after a downgrade/legacy data), locked cities may be turned off.
        if len(was_on) <= allowed:
            for k in CITY_KEYS:
                if locks.get(k) == "locked" and not cities[k]["enabled"]:
                    errors.append(f"{CITY_NAME[k]} is locked until {when}.")
                    # keep it on (and its street) in the re-rendered form
                    cities[k] = {"enabled": True, "street": existing["cities"][k]["street"]}
        for k in CITY_KEYS:
            if locks.get(k) == "blocked" and cities[k]["enabled"] and not existing["cities"][k]["enabled"]:
                errors.append(f"{CITY_NAME[k]} stays off until {when}.")
                cities[k] = {"enabled": False, "street": existing["cities"][k]["street"]}
    if cities_submitted and not enabled:
        errors.append("Choose at least 1 city to watch.")
    elif on_alerts and not enabled:
        errors.append("Pick at least one city before turning alerts on.")
    if on_alerts and not kws and not any(cities[k]["street"] for k in enabled):
        errors.append("Add at least one keyword or a street to watch before turning alerts on.")
    if errors:
        return watch_page(request, email, errors=errors, w=form)
    ts = now_iso()
    with db() as c:
        c.execute(
            "UPDATE watches SET keywords=?, alerts_on=?, updated_at=? WHERE email=?",
            (form["keywords"], on_alerts, ts, email),
        )
        if cities_submitted:
            prev = {r["city_key"]: (bool(r["enabled"]), r["enabled_at"], r["locked_at"], r["blocked_at"])
                    for r in c.execute("SELECT city_key, enabled, enabled_at, locked_at, blocked_at "
                                       "FROM watch_cities WHERE email=?", (email,))}
            for k in CITY_KEYS:
                was_on, was_at, was_locked, was_blocked = prev.get(k, (False, None, None, None))
                on = cities[k]["enabled"]
                # Keep the original switch-on time while a city stays on; new switch-on = now (and locks).
                enabled_at = (was_at if was_on else ts) if on else None
                # Still locked this period -> keep its lock; otherwise (new, or kept across an unlock)
                # saving locks it until the next unlock date, as the page says.
                locked_at = (was_locked if was_on and locks.get(k) == "locked" else ts) if on else None
                blocked_at = None if on else was_blocked
                c.execute(
                    "INSERT INTO watch_cities(email,city_key,enabled,street,updated_at,enabled_at,locked_at,blocked_at) "
                    "VALUES(?,?,?,?,?,?,?,?) "
                    "ON CONFLICT(email,city_key) DO UPDATE SET enabled=excluded.enabled, "
                    "street=excluded.street, updated_at=excluded.updated_at, enabled_at=excluded.enabled_at, "
                    "locked_at=excluded.locked_at, blocked_at=excluded.blocked_at",
                    (email, k, int(on), cities[k]["street"], ts, enabled_at, locked_at, blocked_at),
                )
    return RedirectResponse("/watch?saved=1", status_code=303)


# ---------------- Stripe (live on Cloudflare, test on the box) ----------------

@app.post("/billing/checkout")
async def billing_checkout(request: Request, csrf: str = Form(""), quantity: str = Form("1")):
    email = require_user(request)
    if not email:
        return RedirectResponse("/signin", status_code=303)
    if not check_csrf(request, csrf):
        return billing_page(request, email, errors=["Form expired. Reload and try again."])
    if not billing.configured():
        return billing_page(
            request, email,
            errors=["Online payment is temporarily unavailable. Please try again later."],
        )
    if is_admin(email):   # admin accounts get every city free: never send them to Stripe
        return RedirectResponse("/billing?checkout=admin", status_code=303)
    if user_is_paid(email):
        return RedirectResponse("/billing?checkout=already", status_code=303)
    try:
        u = load_user(email) or {}
        cust = billing.get_or_create_customer(email, u.get("stripe_customer_id"))
        mark_user_paid(email, customer_id=cust, status=u.get("subscription_status") or "none")
        session = billing.create_checkout_session(
            email=email,
            customer_id=cust,
            success_url=f"{base_url()}/billing/success?session_id={{CHECKOUT_SESSION_ID}}",
            cancel_url=f"{base_url()}/billing/cancel",
            quantity=billing.clamp_quantity(quantity),
        )
    except billing.StripeConfigError as ex:
        log.error("Checkout config error: %s", ex)
        return billing_page(request, email, errors=["Online payment is temporarily unavailable. Please try again later."])
    except Exception as ex:
        log.exception("Checkout session create failed")
        return billing_page(request, email, errors=["Could not start Checkout. Please try again in a minute."])
    return RedirectResponse(session.url, status_code=303)


@app.get("/billing/success", response_class=HTMLResponse)
def billing_success(request: Request, session_id: str = ""):
    email = require_user(request)
    if not email:
        return RedirectResponse("/signin", status_code=303)
    verified = False
    receipt = {}
    err = None
    if session_id and billing.configured():
        try:
            sess = billing.retrieve_checkout_session(session_id)
            # Bind session to this user
            ref = (sess.client_reference_id or (sess.metadata or {}).get("email") or "").strip().lower()
            cust_email = ""
            if sess.customer_details and getattr(sess.customer_details, "email", None):
                cust_email = (sess.customer_details.email or "").strip().lower()
            if ref and ref != email and cust_email and cust_email != email:
                err = "This Checkout session belongs to a different account."
            else:
                status = None
                sub_id = None
                extra = None
                cust_id = sess.customer if isinstance(sess.customer, str) else getattr(sess.customer, "id", None)
                if sess.subscription:
                    fields = billing.subscription_fields_from_obj(sess.subscription)
                    status = fields.get("subscription_status")
                    sub_id = fields.get("stripe_subscription_id")
                    extra = fields.get("extra_cities")
                    cust_id = fields.get("stripe_customer_id") or cust_id
                if sess.payment_status == "paid" or status in billing.ACTIVE_STATUSES:
                    apply_subscription_state(email, customer_id=cust_id, subscription_id=sub_id,
                                             status=status or "active", quantity=extra,
                                             cancel_at=fields.get("cancel_at") if sess.subscription else None,
                                             period_end=fields.get("period_end") if sess.subscription else None)
                    verified = True
                receipt = {
                    "session_id": sess.id,
                    "payment_status": sess.payment_status,
                    "subscription_status": status,
                    "customer_id": cust_id,
                    "subscription_id": sub_id,
                    "extra_cities": extra,
                    "amount_total": sess.amount_total,
                    "currency": sess.currency,
                    "mode": billing.required_mode(),
                }
                # Persist a local receipt proof for the director
                proof_dir = ROOT / "state" / "stripe"
                proof_dir.mkdir(parents=True, exist_ok=True)
                proof_path = proof_dir / f"checkout-success-{sess.id}.json"
                proof_path.write_text(json.dumps({
                    "verified_at": now_iso(), "email": email, **receipt,
                    "dashboard_hint": f"Stripe Dashboard ({billing.required_mode()} mode) → Payments / Customers",
                    "portal_url_hint": f"{base_url()}/billing/portal",
                }, indent=2))
                receipt["proof_path"] = str(proof_path)
        except Exception as ex:
            log.exception("Checkout success verify failed")
            err = "Could not confirm the payment yet. Reload this page in a minute, or open Billing."
    return T.TemplateResponse(
        request, "billing_success.html",
        ctx(request, verified=verified, receipt=receipt, err=err, session_id=session_id,
            paid=user_is_paid(email)),
    )


@app.get("/billing/cancel", response_class=HTMLResponse)
def billing_cancel(request: Request):
    email = require_user(request)
    if not email:
        return RedirectResponse("/signin", status_code=303)
    return T.TemplateResponse(request, "billing_cancel.html", ctx(request, paid=user_is_paid(email)))


@app.post("/billing/portal")
async def billing_portal(request: Request, csrf: str = Form("")):
    email = require_user(request)
    if not email:
        return RedirectResponse("/signin", status_code=303)
    if not check_csrf(request, csrf):
        return billing_page(request, email, portal_err="Form expired. Reload and try again.")
    if not billing.configured():
        return billing_page(request, email, portal_err="Online payment is temporarily unavailable.")
    u = load_user(email) or {}
    cust = u.get("stripe_customer_id")
    if not cust:
        return billing_page(request, email, portal_err="No Stripe customer yet. Complete Checkout first.")
    try:
        portal = billing.create_portal_session(customer_id=cust, return_url=f"{base_url()}/billing/return",
                                               configuration=billing.portal_config_id())
    except Exception as ex:
        log.exception("Portal session failed")
        return billing_page(request, email, portal_err="Could not open Manage billing. Please try again in a minute.")
    return RedirectResponse(portal.url, status_code=303)


@app.get("/billing/return")
def billing_return(request: Request):
    """Customer Portal return_url: re-read the subscription from Stripe now, apply it, go to /billing."""
    email = require_user(request)
    if not email:
        return RedirectResponse("/signin", status_code=303)
    res = sync_billing(email, force=True)
    note = billing_notice(res)
    if note:
        request.session["notice"] = note
    return RedirectResponse("/billing", status_code=303)


@app.post("/stripe/webhook")
async def stripe_webhook(request: Request):
    payload = await request.body()
    sig = request.headers.get("stripe-signature", "")
    if not billing.configured():
        return PlainTextResponse("stripe not configured", status_code=503)
    try:
        event = billing.construct_event(payload, sig)
    except billing.StripeConfigError as ex:
        return PlainTextResponse(str(ex), status_code=503)
    except Exception as ex:
        log.warning("Webhook signature/verify failed: %s", ex)
        return PlainTextResponse("invalid", status_code=400)

    etype = event["type"]
    obj = event["data"]["object"]
    log.info("Stripe webhook %s id=%s", etype, event.get("id"))

    try:
        if etype == "checkout.session.completed":
            email = (obj.get("client_reference_id") or (obj.get("metadata") or {}).get("email") or "").strip().lower()
            if not email and obj.get("customer_details"):
                email = (obj["customer_details"].get("email") or "").strip().lower()
            cust = obj.get("customer")
            sub = obj.get("subscription")
            status = "active"
            extra = None
            fields = {}
            if sub:
                fields = billing.subscription_fields_from_obj(sub)
                status = fields.get("subscription_status") or status
                extra = fields.get("extra_cities")
                sub = fields.get("stripe_subscription_id") or sub
                cust = fields.get("stripe_customer_id") or cust
            if email:
                # Pass the period end / pending cancel too: apply_subscription_state writes both columns,
                # so omitting them here wiped period_end whenever this event arrived after
                # customer.subscription.created (Stripe does not guarantee event order).
                apply_subscription_state(email, customer_id=cust if isinstance(cust, str) else None,
                                         subscription_id=sub if isinstance(sub, str) else None,
                                         status=status, quantity=extra,
                                         cancel_at=fields.get("cancel_at"), period_end=fields.get("period_end"))
        elif etype in (
            "customer.subscription.created",
            "customer.subscription.updated",
            "customer.subscription.deleted",
        ):
            fields = billing.subscription_fields_from_obj(obj)
            # Resolve email from metadata or customer
            email = (obj.get("metadata") or {}).get("email", "").strip().lower()
            if not email:
                cust_id = fields.get("stripe_customer_id")
                if cust_id:
                    with db() as c:
                        row = c.execute(
                            "SELECT email FROM users WHERE stripe_customer_id=?", (cust_id,)
                        ).fetchone()
                        email = row["email"] if row else ""
            if not email and fields.get("stripe_customer_id"):
                try:
                    stripe = billing._client()
                    cust = stripe.Customer.retrieve(fields["stripe_customer_id"])
                    email = (cust.email or "").strip().lower()
                except Exception:
                    pass
            if email:
                stored = load_user(email) or {}
                cust_id = fields.get("stripe_customer_id") or stored.get("stripe_customer_id")
                current = None
                if cust_id:
                    # The event may be about an older subscription (e.g. a canceled first one) while
                    # another is active: always apply the customer's CURRENT subscription.
                    try:
                        current = billing.fetch_current_subscription(cust_id, stored.get("stripe_subscription_id"))
                    except Exception:
                        log.exception("Webhook: live re-read failed; using the event object")
                        current = False
                if current is None:
                    apply_subscription_state(email, customer_id=cust_id, status="none", quantity=0)
                else:
                    if current is not False:
                        fields = billing.subscription_fields_from_obj(current)
                    status = fields.get("subscription_status")
                    if current is False and etype == "customer.subscription.deleted":
                        status = "canceled"
                    apply_subscription_state(
                        email,
                        customer_id=fields.get("stripe_customer_id") or cust_id,
                        subscription_id=fields.get("stripe_subscription_id"),
                        status=status,
                        quantity=fields.get("extra_cities"),
                        cancel_at=fields.get("cancel_at"), period_end=fields.get("period_end"),
                    )
    except Exception:
        log.exception("Webhook handler error for %s", etype)
        return PlainTextResponse("handler error", status_code=500)

    return JSONResponse({"received": True})


# ---------------- feedback (signed-in users only) ----------------
# Anti-spam layers (2026-10-22), all free and server-side, in this order on POST /feedback:
#   CSRF -> optional Cloudflare Turnstile -> honeypot -> signed time-trap token -> per-IP hourly limit ->
#   content filter (spam phrases + links, link counts) -> daily store cap -> store -> capped owner notice.
# Dropped spam gets the normal "Thanks" page, is NOT stored as feedback and sends NO email. Counts go in
# guard_usage (fb_* keys, shown on /admin Cost and safety); a short snippet of each drop is kept 14 days in
# feedback_spam so false positives can be spotted.
FEEDBACK_CATEGORIES = ["Irrelevant alert", "Missed alert", "Suggest a source to monitor", "Bug", "Other"]
FEEDBACK_MAX = 2000
FEEDBACK_RATE = (5, 3600)        # per IP: 5 submissions / hour
FEEDBACK_EMAIL_DAILY_CAP = 30    # project-inbox notices per day, all visitors (Cloudflare only)
FEEDBACK_STORE_DAILY_CAP = 100   # stored submissions per day; beyond this: "try again tomorrow", not stored
FEEDBACK_RESEND_RESERVE = 10     # feedback notices never use the last 10 Resend sends of the day (sign-in links)
FEEDBACK_MIN_AGE_S = 3           # a human cannot fill the form faster than this
FEEDBACK_MAX_AGE_S = 3600        # the form page expires after an hour
FEEDBACK_SPAM_KEEP_DAYS = 14
FB_DROP_REASONS = ("honeypot", "no_token", "too_fast", "spam_phrase", "many_links", "turnstile", "rate_ip", "daily_cap")
_fb_signer = URLSafeTimedSerializer(SECRET, salt="aw-feedback-form")

TURNSTILE_VERIFY_URL = "https://challenges.cloudflare.com/turnstile/v0/siteverify"


def turnstile_keys():
    """(site_key, secret) when BOTH env vars are set, else None (Turnstile off, other layers only)."""
    site = (os.environ.get("TURNSTILE_SITE_KEY") or "").strip()
    sec = (os.environ.get("TURNSTILE_SECRET") or "").strip()
    return (site, sec) if site and sec else None


def turnstile_verify(token, ip):
    """True/False from Cloudflare siteverify. None = could not reach Cloudflare (fail open; other layers apply)."""
    import urllib.parse, urllib.request
    keys = turnstile_keys()
    if not keys or not token:
        return False
    data = urllib.parse.urlencode({"secret": keys[1], "response": token[:2048],
                                   **({"remoteip": ip} if ip and ip != "?" else {})}).encode()
    try:
        req = urllib.request.Request(TURNSTILE_VERIFY_URL, data=data, method="POST",
                                     headers={"User-Agent": "AgendaWatch-Container/1.0"})
        with urllib.request.urlopen(req, timeout=8) as r:
            return bool(json.loads(r.read() or b"{}").get("success"))
    except Exception as ex:  # noqa: BLE001
        log.warning("turnstile siteverify unreachable: %s", type(ex).__name__)
        return None


def client_ip(request):
    return (request.headers.get("cf-connecting-ip")
            or (request.client.host if request.client else "") or "?")


def _ip_hash(ip):
    import hashlib
    return hashlib.sha256(f"{SECRET}|{ip}".encode()).hexdigest()[:16]


def _ensure_feedback_tables(c):
    c.execute("CREATE TABLE IF NOT EXISTS feedback_hits(ip_hash TEXT NOT NULL, at REAL NOT NULL)")
    c.execute("CREATE INDEX IF NOT EXISTS feedback_hits_ip ON feedback_hits(ip_hash, at)")
    c.execute("CREATE TABLE IF NOT EXISTS feedback_spam(id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT NOT NULL, "
              "reason TEXT NOT NULL, ip_hash TEXT NOT NULL DEFAULT '', category TEXT NOT NULL DEFAULT '', "
              "snippet TEXT NOT NULL DEFAULT '')")


def _rate_ok(ip, now=None, record=False):
    """Per-IP hourly limit (persistent, IPs stored only as a salted hash). record=True counts this attempt."""
    now = now or time.time()
    n, window = FEEDBACK_RATE
    h = _ip_hash(ip)
    with db() as c:
        _ensure_feedback_tables(c)
        c.execute("DELETE FROM feedback_hits WHERE at < ?", (now - window,))
        used = c.execute("SELECT COUNT(*) FROM feedback_hits WHERE ip_hash=?", (h,)).fetchone()[0]
        if used >= n:
            return False
        if record:
            c.execute("INSERT INTO feedback_hits(ip_hash, at) VALUES(?,?)", (h, now))
    return True


def _fb_count(key, n=1):
    with db() as c:
        aw_guard.ensure_schema(c)
        aw_guard.add(c, key, n)


def _fb_used(key):
    with db() as c:
        aw_guard.ensure_schema(c)
        return aw_guard.usage(c).get(key, 0)


def _fb_drop(request, reason, category="", message=""):
    """Record a dropped submission (counter + short snippet). Returns the normal-looking thanks page."""
    ip = client_ip(request)
    log.info("feedback dropped (%s) ip_hash=%s", reason, _ip_hash(ip))
    with db() as c:
        _ensure_feedback_tables(c)
        aw_guard.ensure_schema(c)
        aw_guard.add(c, "fb_drop_" + reason)
        aw_guard.add(c, "fb_dropped")
        c.execute("INSERT INTO feedback_spam(at, reason, ip_hash, category, snippet) VALUES(?,?,?,?,?)",
                  (now_iso(), reason, _ip_hash(ip), (category or "")[:60], (message or "").strip()[:200]))
        c.execute("DELETE FROM feedback_spam WHERE at < ? OR id <= (SELECT COALESCE(MAX(id),0) - 300 FROM feedback_spam)",
                  ((datetime.now(TZ) - timedelta(days=FEEDBACK_SPAM_KEEP_DAYS)).isoformat(timespec="seconds"),))
    return T.TemplateResponse(request, "feedback_thanks.html", ctx(request))


_LINKISH = re.compile(   # bare domains need a lowercase TLD ("ok.In the meeting" is a typo, not a link)
    r"(?:https?://|www\.)\S+"
    r"|\b[a-z0-9][a-z0-9-]*(?:\.[a-z0-9-]+)*\.(?-i:com|net|org|app|live|io|biz|info|xyz|site|online|shop|club|"
    r"click|link|store|ru|cn|tk|ml|ga|cf|gq|pw|cc|ws|vip|icu|buzz|website|tech|dev)\b", re.I)
_SPAM_PHRASES = re.compile(
    r"search index|search engine|google(?:'s|\u2019s)? (?:search|index|ranking|results|first page)|"
    r"dear sir|dear madam|sir\s*/\s*madam|\bseo\b|backlink|web ?traffic|guest post|domain authority|"
    r"domain (?:name )?(?:expir|renew|registration)|crypto|bitcoin|casino|viagra|cialis|\bloans?\b|forex|"
    r"whatsapp|telegram|click here|buy now|limited offer|make money|work from home|submit your (?:site|website)|"
    r"(?:rank|ranking) (?:#?1|first)|digital marketing|web ?design|website design|increase (?:your )?(?:sales|traffic)",
    re.I)
OWN_DOMAIN_RE = re.compile(r"(?:https?://)?(?:www\.)?agendawatch\.app\b\S*", re.I)


def feedback_spam_reason(message, link="", signed_in=False):
    """Content filter -> reason string or None. Own-domain mentions are not counted as links.
    Signed-in users may paste a few links (bots rarely hold accounts); spam phrases still apply."""
    text = OWN_DOMAIN_RE.sub(" ", message)
    links = _LINKISH.findall(text)
    if _SPAM_PHRASES.search(message) and (links or link):
        return "spam_phrase"
    if len(links) > (3 if signed_in else 1):
        return "many_links"
    return None


def _user_no(account):
    """users.rowid of a signed-in account (display only; the account's key is its email)."""
    if not account:
        return None
    with db() as c:
        r = c.execute("SELECT rowid FROM users WHERE email=?", (account,)).fetchone()
    return r[0] if r else None


def feedback_from_line(account, contact="", user_no=None):
    """Who sent it. `account` is the server-side session account (feedback.user_id), never a form field;
    `contact` is the optional typed reply-to email."""
    if account:
        who = f"From: {account} (signed in, user #{user_no})" if user_no else f"From: {account} (signed in)"
    else:
        who = "From: not signed in (anonymous)"
    if contact and contact != account:
        who += f"\nReply-to (typed on the form): {contact}"
    elif not contact and not account:
        who += "\nReply-to: none given"
    return who


def _notify_feedback(fid, category, city, message, account=None, contact=""):
    """One short notice per submission to the project inbox, only on Cloudflare (relay set).
    Capped per day (persistent), counted against the Resend daily cap, and never uses the last
    FEEDBACK_RESEND_RESERVE sends (those stay for sign-in links)."""
    if not os.environ.get("AW_EMAIL_RELAY_URL"):
        return "not sent (local)"
    if _fb_used("fb_emailed") >= FEEDBACK_EMAIL_DAILY_CAP:
        _fb_count("fb_email_suppressed")
        return "not sent (daily cap)"
    with db() as c:
        aw_guard.ensure_schema(c)
        left = aw_guard.remaining(c, "cap_resend_sends")
    if email_provider() == "resend" and left <= FEEDBACK_RESEND_RESERVE:
        _fb_count("fb_email_suppressed")
        return "not sent (Resend budget reserve)"
    _fb_count("fb_emailed")
    try:
        more = "\u2026" if len(message) > 300 else ""
        where = f" \u00b7 {CITY_NAME.get(city, city)}" if city else ""
        who = feedback_from_line(account, contact, _user_no(account))
        body = (f"New feedback #{fid}: {category}{where}\n{who}\n\n{message[:300]}{more}\n\n"
                f"Review: {base_url()}/admin#feedback\n")
        return send_owner_email(OWNER, f"Agenda Watch feedback: {category}", body)[1]
    except Exception as ex:
        log.warning("feedback notice failed: %r", ex)
        return "failed"


def _feedback_login_redirect(request):
    """Signed-in users only. Logged-out GET or POST goes to /signin (no store, no notice)."""
    if not require_user(request):
        return RedirectResponse("/signin", status_code=303)
    return None


def _feedback_form(request, error=None, form=None, status_code=200):
    email = require_user(request)
    form = form or {}
    ts = turnstile_keys()
    if "csrf" not in request.session:
        request.session["csrf"] = secrets.token_urlsafe(16)
    token = _fb_signer.dumps({"c": request.session["csrf"][:12], "t": time.time()})
    return T.TemplateResponse(request, "feedback.html", ctx(
        request, error=error, form=form, categories=FEEDBACK_CATEGORIES, ft=token, fb_account=email,
        turnstile_site_key=ts[0] if ts else None,
        cities=[(k, CITY_NAME[k]) for k in CITY_KEYS], max_len=FEEDBACK_MAX), status_code=status_code)


@app.get("/feedback", response_class=HTMLResponse)
def feedback_form(request: Request):
    if (redir := _feedback_login_redirect(request)):
        return redir
    return _feedback_form(request)


@app.post("/feedback", response_class=HTMLResponse)
async def feedback_post(request: Request):
    if (redir := _feedback_login_redirect(request)):
        return redir
    f = await request.form()
    g = lambda k: str(f.get(k) or "")  # noqa: E731
    csrf, category, message, city, link, website = (
        g("csrf"), g("category"), g("message"), g("city"), g("link"), g("website"))
    form = {"category": category, "message": message, "city": city, "link": link}
    ip = client_ip(request)
    if not check_csrf(request, csrf):
        return _feedback_form(request, "Form expired, try again.", form, 400)
    if turnstile_keys():                        # optional Cloudflare Turnstile (free); off unless both env vars set
        from starlette.concurrency import run_in_threadpool
        ok = await run_in_threadpool(turnstile_verify, g("cf-turnstile-response"), ip)
        if ok is False:
            _fb_count("fb_drop_turnstile"); _fb_count("fb_dropped")
            return _feedback_form(request, "We could not verify you are human. Please try again.", form, 400)
        if ok is None:
            _fb_count("fb_turnstile_unreachable")
    if website.strip():                         # honeypot: humans never see this field
        _rate_ok(ip, record=True)
        return _fb_drop(request, "honeypot", category, message)
    try:                                        # signed time-trap token from the form page
        tok = _fb_signer.loads(g("ft"), max_age=FEEDBACK_MAX_AGE_S)
        issued = float(tok["t"])     # sub-second issue time (itsdangerous' own stamp is whole seconds)
        if tok.get("c") != request.session.get("csrf", "")[:12]:
            raise BadSignature("other session")
    except SignatureExpired:
        return _feedback_form(request, "This form timed out. Please send it again.", form, 400)
    except (BadSignature, KeyError, TypeError, ValueError):
        _rate_ok(ip, record=True)
        return _fb_drop(request, "no_token", category, message)
    if time.time() - issued < FEEDBACK_MIN_AGE_S:
        _rate_ok(ip, record=True)
        return _fb_drop(request, "too_fast", category, message)
    if not _rate_ok(ip):
        _fb_count("fb_drop_rate_ip"); _fb_count("fb_dropped")
        return _feedback_form(request, "Too many messages from here. Please try again later.", form, 429)
    message = message.replace("\x00", "").strip()
    link = link.strip()[:500]
    if category not in FEEDBACK_CATEGORIES:
        return _feedback_form(request, "Pick a category.", form, 400)
    if not message:
        return _feedback_form(request, "Please write a message.", form, 400)
    if len(message) > FEEDBACK_MAX:
        return _feedback_form(request, f"Please keep it under {FEEDBACK_MAX} characters.", form, 400)
    if city and city not in CITY_KEYS:
        return _feedback_form(request, "Pick a city from the list.", form, 400)
    if link and not re.match(r"^https?://\S+$", link):
        return _feedback_form(request, "The link should start with http:// or https://.", form, 400)
    user = require_user(request)
    account = user if user and load_user(user) else None   # session account only; posted email is ignored
    if not account:
        return RedirectResponse("/signin", status_code=303)
    why = feedback_spam_reason(message, link, signed_in=bool(account))
    if why:
        _rate_ok(ip, record=True)
        return _fb_drop(request, why, category, message)
    if _fb_used("fb_accepted") >= FEEDBACK_STORE_DAILY_CAP:
        _fb_count("fb_drop_daily_cap"); _fb_count("fb_dropped")
        return _feedback_form(request, "We are getting a lot of messages today. Please try again tomorrow.", form, 429)
    _rate_ok(ip, record=True)
    with db() as c:
        fid = c.execute(
            "INSERT INTO feedback(created_at,user_id,email,category,city,link,message) VALUES(?,?,?,?,?,?,?)",
            (now_iso(), account, account, category, city, link, message),
        ).lastrowid
    _fb_count("fb_accepted")
    log.info("feedback #%s saved (%s, signed in); notice: %s", fid, category,
             _notify_feedback(fid, category, city, message, account=account, contact=""))
    return T.TemplateResponse(request, "feedback_thanks.html", ctx(request))


def feedback_guard_ctx():
    """Spam-filter counters for /admin Cost and safety: today, last 7 days, and recent drops."""
    day7 = (datetime.now(TZ).date() - timedelta(days=6)).isoformat()
    with db() as c:
        aw_guard.ensure_schema(c)
        _ensure_feedback_tables(c)
        today_use = aw_guard.usage(c)
        wk = dict(c.execute("SELECT key, SUM(count) FROM guard_usage WHERE day >= ? AND key LIKE 'fb\\_%' ESCAPE '\\' "
                            "GROUP BY key", (day7,)).fetchall())
        recent = [dict(r) for r in c.execute("SELECT at, reason, category, snippet FROM feedback_spam "
                                             "ORDER BY id DESC LIMIT 10")]
    return {"today": {k: today_use.get(k, 0) for k in ("fb_accepted", "fb_dropped", "fb_emailed", "fb_email_suppressed")},
            "reasons_today": {r: today_use.get("fb_drop_" + r, 0) for r in FB_DROP_REASONS if today_use.get("fb_drop_" + r)},
            "week": {k: int(wk.get(k, 0)) for k in ("fb_accepted", "fb_dropped", "fb_emailed")},
            "recent": recent, "turnstile": bool(turnstile_keys()),
            "caps": {"emails": FEEDBACK_EMAIL_DAILY_CAP, "stored": FEEDBACK_STORE_DAILY_CAP,
                     "per_ip": f"{FEEDBACK_RATE[0]}/hour"}}


def admin_feedback(category=""):
    where, args = ("WHERE category = ?", [category]) if category in FEEDBACK_CATEGORIES else ("", [])
    with db() as c:
        rows = [dict(r) for r in c.execute(
            f"SELECT f.*, u.rowid AS user_no FROM feedback f LEFT JOIN users u ON u.email = f.user_id "
            f"{where.replace('category', 'f.category')} ORDER BY f.created_at DESC, f.id DESC LIMIT 200", args)]
        new = c.execute("SELECT COUNT(*) FROM feedback WHERE status='new'").fetchone()[0]
        needs_owner = c.execute("SELECT COUNT(*) FROM feedback WHERE status='needs-owner'").fetchone()[0]
    for r in rows:
        r["city_name"] = CITY_NAME.get(r["city"], r["city"])
    return {"rows": rows, "new": new, "needs_owner": needs_owner}


@app.post("/admin/feedback/{fid}/reviewed")
def admin_feedback_reviewed(request: Request, fid: int, csrf: str = Form(""), fb_category: str = Form("")):
    if not is_admin(require_user(request)):
        return PlainTextResponse("Not Found", status_code=404)
    if check_csrf(request, csrf):
        with db() as c:
            c.execute("UPDATE feedback SET status='reviewed' WHERE id=? AND status!='replied'", (fid,))
    from urllib.parse import urlencode
    q = ("?" + urlencode({"fb_category": fb_category})) if fb_category in FEEDBACK_CATEGORIES else ""
    return RedirectResponse(f"/admin{q}#feedback", status_code=303)


# ---------------- internal feedback handling (2026-11-12; secret-gated, cloud only) ----------------
FEEDBACK_STATUSES = ("new", "needs-owner", "replied", "reviewed")
FEEDBACK_STATUS_ALIASES = {"needs-you": "needs-owner"}  # stored value stays needs-owner
FEEDBACK_REPLY_SUBJECT = "Re: your Agenda Watch feedback"
FEEDBACK_REPLY_FROM = "Agenda Watch <hello@agendawatch.app>"  # replies only; alerts/sign-in stay on alerts@


def _canonical_feedback_status(status):
    s = (status or "").strip()
    return FEEDBACK_STATUS_ALIASES.get(s, s)


def _feedback_reply_address(row):
    """Session account email if signed in, else the typed email. Empty if neither."""
    return (row.get("user_id") or row.get("email") or "").strip()


def _feedback_public(row):
    d = dict(row)
    return {
        "id": d["id"], "created_at": d["created_at"], "category": d["category"],
        "city": d.get("city") or "", "link": d.get("link") or "", "message": d["message"],
        "status": d["status"], "signed_in": bool(d.get("user_id")),
        "reply_address": _feedback_reply_address(d) or None,
        "replied_at": d.get("replied_at"), "reply_text": d.get("reply_text"),
        "owner_note": d.get("owner_note"),
    }


def _feedback_counts(c):
    rows = list(c.execute("SELECT status, COUNT(*) FROM feedback GROUP BY status"))
    out = {s: 0 for s in FEEDBACK_STATUSES}
    out.update({k: n for k, n in rows})
    out["total"] = sum(n for k, n in out.items() if k != "total")
    return out


def _feedback_can_send_reply():
    """(ok, reason). Reuses the feedback notice caps so replies never eat the sign-in Resend reserve."""
    if _fb_used("fb_emailed") >= FEEDBACK_EMAIL_DAILY_CAP:
        return False, "daily cap"
    with db() as c:
        aw_guard.ensure_schema(c)
        left = aw_guard.remaining(c, "cap_resend_sends")
    if email_provider() == "resend" and left <= FEEDBACK_RESEND_RESERVE:
        return False, "Resend budget reserve"
    if email_provider() != "resend":
        return False, f"provider {email_provider()} (replies need Resend)"
    return True, ""


@app.get("/internal/feedback")
def internal_feedback_list(request: Request, status: str = ""):
    """Cloud only, secret-gated, READ-ONLY: list feedback for the daily routine."""
    if os.environ.get("AW_CLOUD") != "1":
        return PlainTextResponse("Not Found", status_code=404)
    if not _internal_ok(request):
        return PlainTextResponse("forbidden", status_code=403)
    status = _canonical_feedback_status(status)
    if status and status not in FEEDBACK_STATUSES:
        return JSONResponse({"error": "bad status"}, status_code=400)
    with db() as c:
        if status:
            rows = [dict(r) for r in c.execute(
                "SELECT * FROM feedback WHERE status=? ORDER BY created_at DESC, id DESC LIMIT 200", (status,))]
        else:
            rows = [dict(r) for r in c.execute(
                "SELECT * FROM feedback ORDER BY created_at DESC, id DESC LIMIT 200")]
        counts = _feedback_counts(c)
    return JSONResponse({"items": [_feedback_public(r) for r in rows], "counts": counts})


@app.post("/internal/feedback/{fid}/status")
async def internal_feedback_status(request: Request, fid: int):
    """Cloud only, secret-gated: set status to needs-owner or reviewed (optional note)."""
    if os.environ.get("AW_CLOUD") != "1":
        return PlainTextResponse("Not Found", status_code=404)
    if not _internal_ok(request):
        return PlainTextResponse("forbidden", status_code=403)
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        body = {}
    want = _canonical_feedback_status(str((body or {}).get("status") or ""))
    note = str((body or {}).get("note") or "").strip()[:2000]
    if want not in ("needs-owner", "reviewed"):
        return JSONResponse({"error": "status must be needs-owner or reviewed"}, status_code=400)
    with db() as c:
        row = c.execute("SELECT * FROM feedback WHERE id=?", (fid,)).fetchone()
        if not row:
            return JSONResponse({"error": "not found"}, status_code=404)
        cur = dict(row)
        if cur["status"] == "replied":
            return JSONResponse({"error": "already replied", "status": "replied"}, status_code=409)
        if note:
            c.execute("UPDATE feedback SET status=?, owner_note=? WHERE id=?", (want, note, fid))
        else:
            c.execute("UPDATE feedback SET status=? WHERE id=?", (want, fid))
        out = dict(c.execute("SELECT * FROM feedback WHERE id=?", (fid,)).fetchone())
    return JSONResponse({"ok": True, "item": _feedback_public(out)})


@app.post("/internal/feedback/{fid}/reply")
async def internal_feedback_reply(request: Request, fid: int):
    """Cloud only, secret-gated: send one reply via Resend and mark the item replied.
    A second call on a replied item is a no-op (no second email)."""
    if os.environ.get("AW_CLOUD") != "1":
        return PlainTextResponse("Not Found", status_code=404)
    if not _internal_ok(request):
        return PlainTextResponse("forbidden", status_code=403)
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        body = {}
    text = str((body or {}).get("text") or "").strip()
    with db() as c:
        row = c.execute("SELECT * FROM feedback WHERE id=?", (fid,)).fetchone()
        if not row:
            return JSONResponse({"error": "not found"}, status_code=404)
        cur = dict(row)
    if cur["status"] == "replied":
        return JSONResponse({"ok": True, "already": True, "status": "replied",
                             "item": _feedback_public(cur)})
    if not text:
        return JSONResponse({"error": "text required"}, status_code=400)
    to = _feedback_reply_address(cur)
    if not to:
        return JSONResponse({"error": "no reply address"}, status_code=409)
    ok, why = _feedback_can_send_reply()
    if not ok:
        return JSONResponse({"error": f"not sent ({why})"}, status_code=429)
    sent, how = send_email(to, FEEDBACK_REPLY_SUBJECT, text, mail_from=FEEDBACK_REPLY_FROM)
    if not sent:
        return JSONResponse({"error": how}, status_code=502)
    _fb_count("fb_emailed")
    with db() as c:
        c.execute("UPDATE feedback SET status='replied', replied_at=?, reply_text=? WHERE id=?",
                  (now_iso(), text, fid))
        out = dict(c.execute("SELECT * FROM feedback WHERE id=?", (fid,)).fetchone())
    return JSONResponse({"ok": True, "already": False, "how": how, "item": _feedback_public(out)})


# ---------------- admin (users.is_admin=1 only; everyone else gets 404) ----------------
ADMIN_PAGE_SIZE = 25


def _alerts_last_30_days(c):
    cutoff = (datetime.now(TZ).date().toordinal() - 30)
    n = c.execute("SELECT COUNT(*) FROM alerts_sent WHERE sent_date >= ?",
                  (datetime.fromordinal(cutoff).date().isoformat(),)).fetchone()[0]
    try:
        hist = json.loads(HISTORY_JSON.read_text()) if HISTORY_JSON.exists() else []
    except Exception:
        hist = []
    since = datetime.fromordinal(cutoff).date().isoformat()
    n += sum(1 for h in hist if (h.get("date") or "") >= since and not h.get("duplicate_of"))
    return n


def admin_stats():
    with db() as c:
        users = [dict(r) for r in c.execute("SELECT * FROM users")]
        per_city = {k: 0 for k in CITY_KEYS}
        for r in c.execute("SELECT city_key, COUNT(*) n FROM watch_cities WHERE enabled=1 GROUP BY city_key"):
            if r["city_key"] in per_city:
                per_city[r["city_key"]] = r["n"]
        alerts30 = _alerts_last_30_days(c)
    extras = [extra_cities_for(u) for u in users]
    return {
        "total_users": len(users),
        "paid_users": sum(1 for e in extras if e > 0),
        "extra_cities": sum(extras),
        "users_per_city": [(CITY_NAME[k], per_city[k]) for k in CITY_KEYS],
        "alerts_30d": alerts30,
        "signups_by_ref": list(ref_signup_counts().items()),
    }


def admin_runs(limit=30):
    with db() as c:
        rows = [dict(r) for r in c.execute("SELECT * FROM fetch_runs ORDER BY started_at DESC, id DESC LIMIT ?", (limit,))]
    for r in rows:
        r["per_city"] = json.loads(r.get("per_city_json") or "{}")
        r["outcome_label"] = aw_store.outcome_label(r)
    return rows


def search_notices(city="", metro="", date_from="", date_to="", matched=False, q="", page=1,
                   page_size=ADMIN_PAGE_SIZE):
    where, args = [], []
    if city:
        where.append("city_key = ?"); args.append(city)
    if metro:
        where.append("metro = ?"); args.append(metro)
    if date_from:
        where.append("substr(meeting_date,1,10) >= ?"); args.append(date_from)
    if date_to:
        where.append("substr(meeting_date,1,10) <= ?"); args.append(date_to)
    if matched:
        where.append("matched = 1")
    if q:
        where.append("(title LIKE ? OR body LIKE ? OR text LIKE ? OR matched_terms LIKE ?)")
        args += [f"%{q}%"] * 4
    w = ("WHERE " + " AND ".join(where)) if where else ""
    page = max(1, page)
    with db() as c:
        total = c.execute(f"SELECT COUNT(*) FROM notices {w}", args).fetchone()[0]
        rows = [dict(r) for r in c.execute(
            f"SELECT * FROM notices {w} ORDER BY meeting_date DESC, id DESC LIMIT ? OFFSET ?",
            args + [page_size, (page - 1) * page_size])]
    for r in rows:
        r["pdf_urls"] = json.loads(r.get("pdf_urls") or "[]")
        r["city_name"] = CITY_NAME.get(r["city_key"], r["city_key"])
        txt = " ".join((r.get("text") or "").split())
        i = txt.lower().find(q.lower()) if q else -1
        r["snippet"] = (("…" if i > 80 else "") + txt[max(0, i - 80): i + 220] + "…") if i >= 0 else txt[:200]
    pages = max(1, -(-total // page_size))
    return {"total": total, "rows": rows, "page": min(page, pages), "pages": pages}


PERSIST_STATUS = ROOT / "cache" / "persist_status.json"


def _worker_guard(method="GET", payload=None, timeout=4):
    """Cloudflare only: the Worker's view (wakes, snapshot uploads, last snapshot) or push our settings."""
    base = os.environ.get("AW_PERSIST_URL", "").rsplit("/persist", 1)[0]
    if not base:
        return None
    import urllib.request
    req = urllib.request.Request(base + "/guard", method=method,
                                 data=json.dumps(payload).encode() if payload is not None else None,
                                 headers={"Content-Type": "application/json",
                                          "X-AW-Internal-Secret": os.environ.get("AW_INTERNAL_SECRET", ""),
                                          "User-Agent": "AgendaWatch-Container/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read() or b"{}")
    except Exception as ex:  # noqa: BLE001
        log.warning("worker guard %s failed: %r", method, ex)
        return {"error": f"{type(ex).__name__}"}


def sync_guard_to_worker():
    with db() as c:
        vals = aw_guard.get_all(c)
    return _worker_guard("POST", {k: vals[k] for k in aw_guard.WORKER_FIELDS})


def _denver_label(iso):
    if not iso:
        return None
    try:
        d = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
        if d.tzinfo is None:
            d = d.replace(tzinfo=timezone.utc)
        return d.astimezone(TZ).strftime("%Y-%m-%d %H:%M %Z")
    except ValueError:
        return str(iso)


def cost_safety_ctx(errors=None, saved=False, sync=None):
    with db() as c:
        vals = aw_guard.get_all(c)
        use = aw_guard.usage(c)
        trips = aw_guard.trips(c)
        lock = aw_guard.lock_info(c)
    w = _worker_guard() or {}
    wuse = (w.get("usage") or {}) if isinstance(w, dict) else {}
    try:
        ps = json.loads(PERSIST_STATUS.read_text())
    except Exception:  # noqa: BLE001
        ps = {}
    caps = []
    for name in ("cap_pmn_requests", "cap_alert_emails", "cap_resend_sends", "cap_snapshot_uploads",
                 "cap_container_wakes"):
        key = aw_guard.USAGE_FOR_CAP[name]
        used = wuse.get(key, use.get(key)) if key in ("snapshot_uploads", "container_wakes") else use.get(key, 0)
        caps.append({"name": name, "label": aw_guard.FIELDS[name][3], "cap": vals[name], "used": used,
                     "min": aw_guard.FIELDS[name][1], "max": aw_guard.FIELDS[name][2],
                     "tripped": any(t["key"] == name for t in trips) or name in (w.get("trips") or [])})
    tripped = sorted({t["key"] for t in trips} | set(w.get("trips") or []))
    return {"vals": vals, "caps": caps, "tripped": [GUARD_LABEL.get(k, k) for k in tripped],
            "lock": lock, "worker": w, "persist": ps, "errors": errors or [], "saved": saved, "sync": sync,
            "last_snapshot_at": _denver_label(w.get("last_snapshot_at") or ps.get("last_upload_at")),
            "today": aw_guard.today()}


@app.post("/admin/cost-safety", response_class=HTMLResponse)
async def admin_cost_safety(request: Request):
    """Admin only. Partial save: only submitted fields change; any invalid field -> nothing saved."""
    email = require_user(request)
    if not is_admin(email):
        return PlainTextResponse("Not Found", status_code=404)
    form = await request.form()
    if not check_csrf(request, form.get("csrf", "")):
        return PlainTextResponse("Form expired, reload the admin page.", status_code=400)
    fields = {k: form.get(k) for k in aw_guard.FIELDS if form.get(k) not in (None, "")}
    with db() as c:
        _, errors = aw_guard.update(c, fields, now_iso())
    if errors:
        return admin_page(request, cs_errors=errors)
    sync = sync_guard_to_worker()
    log.info("cost-safety settings saved by admin: %s (worker sync: %s)", sorted(fields), sync)
    return RedirectResponse("/admin?cs_saved=1#cost-safety", status_code=303)


@app.get("/admin", response_class=HTMLResponse)
def admin_page(request: Request, city: str = "", metro: str = "", date_from: str = "", date_to: str = "",
               matched: str = "", q: str = "", page: int = 1, fb_category: str = "", cs_saved: str = "",
               cs_errors=None):
    email = require_user(request)
    if not is_admin(email):
        return PlainTextResponse("Not Found", status_code=404)
    city = city if city in CITY_KEYS else ""
    metros = [m for m, _ in METRO_GROUPS]
    metro = metro if metro in metros else ""
    date_from = date_from if re.fullmatch(r"\d{4}-\d{2}-\d{2}", date_from) else ""
    date_to = date_to if re.fullmatch(r"\d{4}-\d{2}-\d{2}", date_to) else ""
    q = q.strip()[:200]
    res = search_notices(city, metro, date_from, date_to, bool(matched), q, page)
    f = {"city": city, "metro": metro, "date_from": date_from, "date_to": date_to,
         "matched": "1" if matched else "", "q": q}
    from urllib.parse import urlencode
    qs = urlencode({k: v for k, v in f.items() if v})
    return T.TemplateResponse(request, "admin.html", ctx(
        request, stats=admin_stats(), runs=admin_runs(), res=res, f=f, qs=qs,
        fb=admin_feedback(fb_category), fb_category=fb_category if fb_category in FEEDBACK_CATEGORIES else "",
        fb_categories=FEEDBACK_CATEGORIES,
        cities=[(k, CITY_NAME[k]) for k in CITY_KEYS], metros=metros,
        fbg=feedback_guard_ctx(),
        cs=cost_safety_ctx(errors=cs_errors if isinstance(cs_errors, list) else None, saved=bool(cs_saved))),
        status_code=400 if isinstance(cs_errors, list) and cs_errors else 200)


@app.get("/internal/status")
def internal_status(request: Request):
    """Latest fetch_runs row as JSON (Cloudflare only; Worker reaches it via /__aw/status)."""
    if os.environ.get("AW_CLOUD") != "1":
        return PlainTextResponse("Not Found", status_code=404)
    import hmac
    sec = os.environ.get("AW_INTERNAL_SECRET", "")
    got = request.headers.get("x-aw-internal-secret", "")
    if not sec or not hmac.compare_digest(sec, got):
        return PlainTextResponse("forbidden", status_code=403)
    with db() as c:
        latest = aw_store.latest_run(c)
    with _RUN_LOCK:
        run = {k: v for k, v in _RUN.items() if k != "result"}
        last = _RUN["result"]
    try:
        last_alerts = json.loads(get_setting("last_alerts") or "null")
    except Exception:  # noqa: BLE001
        last_alerts = None
    with db() as c:
        g = {"settings": aw_guard.get_all(c), "usage_today": aw_guard.usage(c), "trips_today": aw_guard.trips(c),
             "lock": aw_guard.lock_info(c)}
        refs = ref_signup_counts(c)
    return JSONResponse({"ref_signups": refs, "last_email": dict(LAST_EMAIL) or None, "email_provider": email_provider(),
                         "last_alerts": last_alerts, "cost_guard": g,
                         "alerts_off_cities": sorted(aw_alerts.off_cities(CFG)),
                         "fetch_enabled": bool(g["settings"]["fetch_enabled"]),
                         "latest_run": latest, "in_progress": run if run["running"] else None,
                         "last_job": {k: v for k, v in last.items() if not k.endswith("_tail")} if last else None})


@app.post("/internal/checkout-probe")
def internal_checkout_probe(request: Request):
    """Cloud only, secret-gated: create (NOT complete) a Checkout Session for the owner through the normal
    code path, to prove live Checkout works. Nothing is written to the DB; the session is left to expire.
    Also scans the deployed templates for test-mode wording."""
    if os.environ.get("AW_CLOUD") != "1":
        return PlainTextResponse("Not Found", status_code=404)
    import hmac
    sec = os.environ.get("AW_INTERNAL_SECRET", "")
    if not sec or not hmac.compare_digest(sec, request.headers.get("x-aw-internal-secret", "")):
        return PlainTextResponse("forbidden", status_code=403)
    banned = re.compile(r"no charge yet|test mode|test checkout|4242|sk_test|pk_test|stripe test", re.I)
    tpl = {p.name: bool(banned.search(p.read_text())) for p in (Path(__file__).parent / "templates").glob("*.html")}
    out = {"mode": billing.required_mode(), "templates_with_test_wording": [k for k, v in tpl.items() if v],
           "templates_scanned": len(tpl)}
    try:
        u = load_user(OWNER) or {}
        cust = billing.get_or_create_customer(OWNER, u.get("stripe_customer_id"))
        sess = billing.create_checkout_session(email=OWNER, customer_id=cust,
                                               success_url=f"{base_url()}/billing/success?session_id={{CHECKOUT_SESSION_ID}}",
                                               cancel_url=f"{base_url()}/billing/cancel", quantity=1)
        from urllib.parse import urlparse
        li = billing._client().checkout.Session.list_line_items(sess.id, limit=5)
        out.update({"session_id": sess.id, "url_host": urlparse(sess.url).netloc, "livemode": sess.livemode,
                    "status": sess.status, "payment_status": sess.payment_status, "amount_total": sess.amount_total,
                    "currency": sess.currency, "expires_at": sess.expires_at, "customer_id": cust,
                    "line_items": [{"price": i.price.id, "quantity": i.quantity, "unit_amount": i.price.unit_amount,
                                    "interval": i.price.recurring.interval if i.price.recurring else None}
                                   for i in li.data],
                    "catalog_price_id": billing.LIVE_IDS.get("price_id")})
    except Exception as ex:  # noqa: BLE001
        out["error"] = f"{type(ex).__name__}: {str(ex)[:300]}"
    return JSONResponse(out)


# ---------------- account deletion (2026-10-22, secret-gated, Stripe READ-ONLY check first) ----------------
_PAID_SUB_STATUSES = ("active", "trialing", "past_due", "unpaid", "incomplete")
_USER_TABLES = ("watch_cities", "watches", "magic_nonces", "user_alerts", "alerts_sent")


def _internal_ok(request) -> bool:
    import hmac
    sec = os.environ.get("AW_INTERNAL_SECRET", "")
    return bool(sec) and hmac.compare_digest(sec, request.headers.get("x-aw-internal-secret", ""))


def stripe_user_check(email):
    """READ-ONLY: Stripe customers/subscriptions for this email (stored ids + customer search by email).
    Never creates or changes anything in Stripe."""
    u = load_user(email) or {}
    out = {"mode": billing.required_mode(),
           "app": {k: u.get(k) for k in ("stripe_customer_id", "stripe_subscription_id", "subscription_status",
                                          "test_stripe_customer_id", "test_stripe_subscription_id")},
           "customers": [], "subscriptions": [], "paid_active": False}
    stripe = billing._client()
    cust_ids = []
    if u.get("stripe_customer_id"):
        cust_ids.append(u["stripe_customer_id"])
    for cu in stripe.Customer.list(email=email, limit=20).data:
        if cu.id not in cust_ids:
            cust_ids.append(cu.id)
    for cid in cust_ids:
        out["customers"].append(cid)
        for sub in stripe.Subscription.list(customer=cid, status="all", limit=20).data:
            out["subscriptions"].append({"id": sub.id, "customer": cid, "status": sub.status,
                                         "livemode": sub.livemode})
    if u.get("stripe_subscription_id") and u["stripe_subscription_id"] not in [x["id"] for x in out["subscriptions"]]:
        sub = stripe.Subscription.retrieve(u["stripe_subscription_id"])
        out["subscriptions"].append({"id": sub.id, "customer": sub.customer, "status": sub.status,
                                     "livemode": sub.livemode})
    out["paid_active"] = any(x["status"] in _PAID_SUB_STATUSES for x in out["subscriptions"]) \
        or (u.get("subscription_status") or "") in _PAID_SUB_STATUSES
    return out


def delete_user_data(email) -> dict:
    """Delete one app account and its watch/cities/sign-in nonces/alert history. App DB only."""
    counts = {}
    with db() as c:
        for t in _USER_TABLES:
            counts[t] = c.execute(f"DELETE FROM {t} WHERE email=?", (email,)).rowcount
        counts["feedback"] = c.execute("DELETE FROM feedback WHERE user_id=?", (email,)).rowcount
        counts["users"] = c.execute("DELETE FROM users WHERE email=?", (email,)).rowcount
    return counts


@app.get("/internal/user-check")
def internal_user_check(request: Request, email: str = ""):
    """Cloud only, secret-gated, READ-ONLY: app rows + Stripe customer/subscriptions for one email."""
    if os.environ.get("AW_CLOUD") != "1":
        return PlainTextResponse("Not Found", status_code=404)
    if not _internal_ok(request):
        return PlainTextResponse("forbidden", status_code=403)
    email = email.strip().lower()
    with db() as c:
        rows = {t: c.execute(f"SELECT COUNT(*) FROM {t} WHERE email=?", (email,)).fetchone()[0]
                for t in ("users",) + _USER_TABLES}
    out = {"email": email, "rows": rows, "is_admin": is_admin(email)}
    try:
        out["stripe"] = stripe_user_check(email)
    except Exception as ex:  # noqa: BLE001
        out["stripe_error"] = f"{type(ex).__name__}: {str(ex)[:300]}"
    return JSONResponse(out)


@app.post("/internal/delete-user")
async def internal_delete_user(request: Request):
    """Cloud only, secret-gated. Body {"email": e, "confirm": e}. Refuses the owner and admins, refuses if
    the Stripe check fails or finds a paid/active subscription. Never touches Stripe."""
    if os.environ.get("AW_CLOUD") != "1":
        return PlainTextResponse("Not Found", status_code=404)
    if not _internal_ok(request):
        return PlainTextResponse("forbidden", status_code=403)
    body = await request.json()
    email = str(body.get("email") or "").strip().lower()
    if not email or email != str(body.get("confirm") or "").strip().lower():
        return JSONResponse({"error": "email and confirm must match"}, status_code=400)
    if email == aw_cities.owner_email() or email in aw_cities.admin_emails() or is_admin(email):
        return JSONResponse({"error": "refused: owner/admin account"}, status_code=403)
    if not load_user(email):
        return JSONResponse({"error": "no such user"}, status_code=404)
    try:
        chk = stripe_user_check(email)
    except Exception as ex:  # noqa: BLE001
        return JSONResponse({"error": f"stripe check failed, nothing deleted: {type(ex).__name__}"}, status_code=502)
    if chk["paid_active"]:
        return JSONResponse({"error": "paid/active subscription: nothing deleted", "stripe": chk}, status_code=409)
    counts = delete_user_data(email)
    (ROOT / "state" / ".persist-now").touch()
    log.info("Deleted account %s (rows %s); Stripe untouched", email, counts)
    return JSONResponse({"deleted": email, "counts": counts, "stripe": chk})


def _watch_readback(c, email):
    w = c.execute("SELECT keywords, alerts_on FROM watches WHERE email=?", (email,)).fetchone()
    cities = {r["city_key"]: {"enabled": int(r["enabled"]), "street": r["street"] or ""}
              for r in c.execute("SELECT city_key, enabled, street FROM watch_cities WHERE email=? ORDER BY city_key", (email,))}
    return {"keywords": w["keywords"] if w else None, "alerts_on": int(w["alerts_on"]) if w else None, "cities": cities,
            "enabled": sorted(k for k, v in cities.items() if v["enabled"])}


@app.post("/internal/admin-watch-add")
async def internal_admin_watch_add(request: Request):
    """Cloud only, secret-gated (owner order 2026-11-03). Body {"email": e, "confirm": e, "add": [city keys]}.
    ADMIN accounts only. Switches ON only the listed catalog cities in that admin's watch; every other
    city, street, keyword and the alerts switch are left exactly as they are. Sends nothing, starts no run."""
    if os.environ.get("AW_CLOUD") != "1":
        return PlainTextResponse("Not Found", status_code=404)
    if not _internal_ok(request):
        return PlainTextResponse("forbidden", status_code=403)
    body = await request.json()
    email = str(body.get("email") or "").strip().lower()
    if not email or email != str(body.get("confirm") or "").strip().lower():
        return JSONResponse({"error": "email and confirm must match"}, status_code=400)
    if not is_admin(email):
        return JSONResponse({"error": "refused: not an admin account"}, status_code=403)
    add = body.get("add") or []
    if not isinstance(add, list) or not add or any(k not in CITY_KEYS for k in add):
        return JSONResponse({"error": "add must be a non-empty list of catalog city keys"}, status_code=400)
    ts = now_iso()
    with db() as c:
        if not c.execute("SELECT 1 FROM watches WHERE email=?", (email,)).fetchone():
            return JSONResponse({"error": "admin has no watch row"}, status_code=404)
        before = _watch_readback(c, email)
        for k in add:
            cur = before["cities"].get(k)
            if cur and cur["enabled"]:
                continue                       # already on: untouched
            c.execute(
                "INSERT INTO watch_cities(email,city_key,enabled,street,updated_at,enabled_at,locked_at,blocked_at) "
                "VALUES(?,?,1,'',?,?,?,NULL) "
                "ON CONFLICT(email,city_key) DO UPDATE SET enabled=1, updated_at=excluded.updated_at, "
                "enabled_at=excluded.enabled_at, locked_at=excluded.locked_at, blocked_at=NULL",
                (email, k, ts, ts, ts),
            )
        after = _watch_readback(c, email)
    (ROOT / "state" / ".persist-now").touch()
    log.info("admin-watch-add %s: +%s (nothing sent, no run)", email, add)
    return JSONResponse({"email": email, "added": add, "before": before, "after": after})


@app.get("/internal/admin-watch")
def internal_admin_watch(request: Request, email: str = ""):
    """Cloud only, secret-gated, READ-ONLY: one admin's watch (cities, streets, keywords, alerts)."""
    if os.environ.get("AW_CLOUD") != "1":
        return PlainTextResponse("Not Found", status_code=404)
    if not _internal_ok(request):
        return PlainTextResponse("forbidden", status_code=403)
    email = email.strip().lower()
    if not is_admin(email):
        return JSONResponse({"error": "refused: not an admin account"}, status_code=403)
    with db() as c:
        return JSONResponse({"email": email, **_watch_readback(c, email)})


def _watch_list_account(email):
    """One account as the morning report should count it: enabled cities only, streets, alerts, plan."""
    u = load_user(email)
    if not u:
        return None
    w = load_watch(email)
    enabled = []
    if w:
        for k in CITY_KEYS:
            st = (w.get("cities") or {}).get(k) or {}
            if st.get("enabled"):
                enabled.append({"key": k, "name": CITY_NAME[k], "street": st.get("street") or ""})
    return {
        "email": email,
        "created_at": u.get("created_at"),
        "ref": u.get("ref") or "direct",
        "alerts_on": bool(w["alerts_on"]) if w else False,
        "is_admin": is_admin(email),
        "subscription_status": u.get("subscription_status"),
        "extra_cities": extra_cities_for(u),
        "allowed": allowed_cities(email),
        "enabled": enabled,
    }


@app.get("/internal/watch-list")
def internal_watch_list(request: Request, email: str = ""):
    """Cloud only, secret-gated, READ-ONLY: enabled cities, streets, alerts, and plan for every
    account (or one account with ?email=). Does not change any row. Non-admin accounts included."""
    if os.environ.get("AW_CLOUD") != "1":
        return PlainTextResponse("Not Found", status_code=404)
    if not _internal_ok(request):
        return PlainTextResponse("forbidden", status_code=403)
    want = email.strip().lower()
    if want:
        item = _watch_list_account(want)
        if not item:
            return JSONResponse({"error": "no such user"}, status_code=404)
        return JSONResponse({"accounts": [item]})
    with db() as c:
        emails = [r["email"] for r in c.execute("SELECT email FROM users ORDER BY created_at, email")]
        refs = ref_signup_counts(c)
    # ref_signups: new accounts per ?ref= code ('direct' = no code). Counts only; no addresses.
    return JSONResponse({"accounts": [_watch_list_account(e) for e in emails], "ref_signups": refs})


@app.get("/internal/resend-lookup")
def internal_resend_lookup(request: Request, id: str = ""):
    """Cloud only, secret-gated, read-only: delivery state of one Resend email by id
    (recipient, subject, created_at, last_event). Never returns the API key or the body."""
    if os.environ.get("AW_CLOUD") != "1":
        return PlainTextResponse("Not Found", status_code=404)
    if not _internal_ok(request):
        return PlainTextResponse("forbidden", status_code=403)
    if not re.fullmatch(r"[0-9a-fA-F-]{8,64}", id):
        return JSONResponse({"error": "bad id"}, status_code=400)
    import urllib.request, urllib.error
    key = (os.environ.get("RESEND_API_KEY") or "").strip()
    req = urllib.request.Request(f"{RESEND_URL}/{id}", headers={"Authorization": f"Bearer {key}",
                                                                "User-Agent": "AgendaWatch-Container/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            d = json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as ex:
        return JSONResponse({"error": f"HTTP {ex.code}"}, status_code=502)
    except Exception as ex:  # noqa: BLE001
        return JSONResponse({"error": type(ex).__name__}, status_code=502)
    return JSONResponse({k: d.get(k) for k in ("id", "to", "subject", "created_at", "last_event")})


@app.post("/internal/test-signin")
async def internal_test_signin(request: Request):
    """Cloud only, secret-gated: send ONE sign-in link to the given address through the normal path
    (rate limit applies). Body: {"email": "..."}. Returns provider + status only (never the link)."""
    if os.environ.get("AW_CLOUD") != "1":
        return PlainTextResponse("Not Found", status_code=404)
    import hmac
    sec = os.environ.get("AW_INTERNAL_SECRET", "")
    if not sec or not hmac.compare_digest(sec, request.headers.get("x-aw-internal-secret", "")):
        return PlainTextResponse("forbidden", status_code=403)
    try:
        email = str((await request.json()).get("email", "")).strip().lower()
    except Exception:  # noqa: BLE001
        email = ""
    if not EMAIL_RE.match(email):
        return JSONResponse({"error": "bad email"}, status_code=400)
    _, status = issue_magic_link(email)
    return JSONResponse({"provider": email_provider(), "to": email, "status": status,
                         "sent": status.startswith("sent")})


@app.get("/internal/city-sources-check")
def internal_city_sources_check(request: Request):
    """Cloud only, secret-gated, READ-ONLY: fetches the stored city agendas pages (and their upcoming
    agenda/packet PDFs) from the container, and dry-runs the per-user alert plan on what it found.
    Writes nothing (read-only DB connection), sends nothing. ?emails=a,b limits the rendered emails;
    ?text=1 adds the extracted document texts."""
    if os.environ.get("AW_CLOUD") != "1":
        return PlainTextResponse("Not Found", status_code=404)
    import hmac
    sec = os.environ.get("AW_INTERNAL_SECRET", "")
    if not sec or not hmac.compare_digest(sec, request.headers.get("x-aw-internal-secret", "")):
        return PlainTextResponse("forbidden", status_code=403)
    import aw_city_sources
    want = {e.strip().lower() for e in (request.query_params.get("emails") or "").split(",") if e.strip()}
    now = datetime.now(TZ)
    cfg = aw_cities.load_config()
    ro = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    try:
        srcs = aw_city_sources.sources(ro)
        cache = aw_store.load_cache(ro)[1]
        entries, stats, failures = aw_city_sources.scan(cfg, ro, now, cache, persist=False)
        plan = aw_alerts.plan(ro, entries, cfg, now, cfg.get("max_items_per_email", 5),
                              link_only=stats.get("unreadable") or [])
        users = []
        for w in aw_alerts.load_watches(ro):
            if want and w["email"].lower() not in want:
                continue
            hits = aw_alerts.match_user(entries, w, now, skip_cities=aw_alerts.off_cities(cfg))
            rows = []
            for h in hits:
                pr = (aw_alerts.minutes_prior(ro, w["email"], h["minutes_key"]) if aw_alerts.is_past(h)
                      else aw_alerts.prior(ro, w["email"], h))
                rows.append({"agenda_item": h.get("agenda_item"), "kind": h.get("kind"), "matched": h.get("matched_terms"),
                             "meeting_date": h.get("meeting_date"), "fingerprint": h.get("fingerprint"),
                             "prior": ({k: pr.get(k) for k in ("status", "first_at", "notice_url", "fingerprint")}
                                       if pr else None), "would_send": aw_alerts.should_send(pr)})
            for h in aw_alerts.link_only_hits(stats.get("unreadable"), w, now, skip_cities=aw_alerts.off_cities(cfg)):
                pr = aw_alerts.prior(ro, w["email"], h)
                rows.append({"agenda_item": h.get("agenda_item"), "kind": h.get("kind"), "matched": None,
                             "meeting_date": h.get("meeting_date"), "fingerprint": h.get("fingerprint"),
                             "read_error": h.get("read_error"),
                             "prior": ({k: pr.get(k) for k in ("status", "first_at", "notice_url", "fingerprint")}
                                       if pr else None), "would_send": aw_alerts.should_send(pr)})
            pu = next((u for u in plan["users"] if u["email"] == w["email"]), None)
            mails = [{"kind": k, "subject": s, "body": b}
                     for k, s, b, _g in aw_alerts.build_user_emails(pu["items"], base_url())] if pu else []
            users.append({"email": w["email"], "alerts_on": bool(w["alerts_on"]),
                          "cities_on": sorted(k for k, v in (w.get("cities") or {}).items() if v.get("enabled")),
                          "hits": rows, "would_email": bool(mails), "emails": mails})
        out = {"now": now.isoformat(timespec="seconds"), "sources": srcs, "scan": stats, "failures": failures,
               "entries": [{k: e[k] for k in ("city_key", "notice_url", "meeting_body", "meeting_datetime", "event_iso")}
                           | {"docs": [{"name": d, "url": u, "chars": len(x or "")} for d, u, x in e["docs"]]}
                           for e in entries],
               "users": users, "wrote": "nothing", "sent": "nothing"}
        pr = [u.strip() for u in (request.query_params.get("probe") or "").split(",") if u.strip()][:5]
        if pr:
            out["probe"] = [aw_city_sources.probe(u) for u in pr]
        if request.query_params.get("text") == "1":
            out["texts"] = {u: x for e in entries for _d, u, x in e["docs"]}
        return JSONResponse(out)
    finally:
        ro.close()


@app.get("/internal/email-check")
def internal_email_check(request: Request):
    """Cloud only, secret-gated, READ-ONLY: no email is sent. Calls Resend GET /domains with the key to
    see whether the key reaches Resend, its permission, and the domain status. Never returns the key."""
    if os.environ.get("AW_CLOUD") != "1":
        return PlainTextResponse("Not Found", status_code=404)
    import hmac, urllib.request, urllib.error
    sec = os.environ.get("AW_INTERNAL_SECRET", "")
    if not sec or not hmac.compare_digest(sec, request.headers.get("x-aw-internal-secret", "")):
        return PlainTextResponse("forbidden", status_code=403)
    key = (os.environ.get("RESEND_API_KEY") or "").strip()
    raw = os.environ.get("RESEND_API_KEY") or ""
    out = {"provider": email_provider(), "from": RESEND_FROM, "last_email": dict(LAST_EMAIL) or None,
           "key_present": bool(key), "key_prefix_ok": key.startswith("re_"),
           "key_whitespace": raw != raw.strip(), "key_quoted": raw.strip()[:1] in "\"'" and bool(raw.strip())}
    if key:
        req = urllib.request.Request("https://api.resend.com/domains",
                                     headers={"Authorization": f"Bearer {key}", "User-Agent": "AgendaWatch-Container/1.0"})
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                d = json.loads(r.read() or b"{}")
            out["domains_http"] = 200
            out["domains"] = [{k: x.get(k) for k in ("name", "status", "region", "capabilities")} for x in d.get("data", [])]
        except urllib.error.HTTPError as ex:
            try:
                body = json.loads(ex.read() or b"{}")
            except Exception:  # noqa: BLE001
                body = {}
            out["domains_http"] = ex.code
            out["domains_error"] = {k: str(v).replace(key, "[redacted]")[:200] for k, v in body.items()}
        except Exception as ex:  # noqa: BLE001
            out["domains_error"] = f"{type(ex).__name__}: {str(ex)[:200]}".replace(key, "[redacted]")
    return JSONResponse(out)


@app.get("/internal/billing-check")
def internal_billing_check(request: Request):
    """Cloud only, secret-gated, READ-ONLY: the owner's live subscriptions (no changes) + the live
    portal configuration's cancel/update settings, alongside what the app has stored."""
    if os.environ.get("AW_CLOUD") != "1":
        return PlainTextResponse("Not Found", status_code=404)
    import hmac
    sec = os.environ.get("AW_INTERNAL_SECRET", "")
    if not sec or not hmac.compare_digest(sec, request.headers.get("x-aw-internal-secret", "")):
        return PlainTextResponse("forbidden", status_code=403)
    u = load_user(OWNER) or {}
    out = {"mode": billing.required_mode(),
           "app": {k: u.get(k) for k in ("stripe_customer_id", "stripe_subscription_id", "subscription_status",
                                          "extra_cities", "cancel_at", "period_end")}}
    try:
        stripe = billing._client()
        subs = stripe.Subscription.list(customer=u.get("stripe_customer_id"), status="all", limit=20).data \
            if u.get("stripe_customer_id") else []
        out["subscriptions"] = [{"id": s.id, "status": s.status, "livemode": s.livemode,
                                 "quantity": billing.extra_cities_from_subscription(s),
                                 "cancel_at_period_end": s.cancel_at_period_end, "cancel_at": s.cancel_at,
                                 "schedule": s.schedule,
                                 "period_end": billing.subscription_fields_from_obj(s).get("period_end")}
                                for s in subs]
        cid = billing.LIVE_IDS.get("portal_config_id")
        if cid:
            cfg = stripe.billing_portal.Configuration.retrieve(cid, stripe_version=billing.PORTAL_API_VERSION)
            f = cfg.features
            out["portal"] = {"id": cfg.id, "cancel": f.subscription_cancel.to_dict_recursive()
                             if hasattr(f.subscription_cancel, "to_dict_recursive") else dict(f.subscription_cancel),
                             "update_proration": f.subscription_update.proration_behavior,
                             "schedule_at_period_end": str(getattr(f.subscription_update, "schedule_at_period_end", None))}
    except Exception as ex:  # noqa: BLE001
        out["error"] = f"{type(ex).__name__}: {str(ex)[:300]}"
    return JSONResponse(out)


@app.post("/internal/stripe-descriptor")
def internal_stripe_descriptor(request: Request):
    """Cloud only, secret-gated: set statement_descriptor=AGWATCH on the product of the live catalog
    price and return the product as read back from Stripe (no other changes)."""
    if os.environ.get("AW_CLOUD") != "1":
        return PlainTextResponse("Not Found", status_code=404)
    import hmac
    sec = os.environ.get("AW_INTERNAL_SECRET", "")
    if not sec or not hmac.compare_digest(sec, request.headers.get("x-aw-internal-secret", "")):
        return PlainTextResponse("forbidden", status_code=403)
    out = {"mode": billing.required_mode()}
    try:
        stripe = billing._client()
        price = stripe.Price.retrieve(billing.ensure_price_id())
        out["price"] = {"id": price.id, "livemode": price.livemode, "product": price.product,
                        "lookup_key": price.lookup_key, "unit_amount": price.unit_amount}
        out["settings_product_id"] = billing.LIVE_IDS.get("product_id")
        prod = billing.ensure_product_statement_descriptor(price.product)
        out["product"] = {"id": prod.id, "livemode": prod.livemode, "name": prod.name, "type": prod.type,
                          "statement_descriptor": prod.statement_descriptor}
    except Exception as ex:  # noqa: BLE001
        out["error"] = f"{type(ex).__name__}: {str(ex)[:300]}"
    return JSONResponse(out)


@app.get("/healthz")
def healthz():
    return {
        "ok": True,
        "email_provider": email_provider(),
        "stripe_mode": billing.required_mode(),
        "stripe_configured": billing.configured(),
        **({"stripe_catalog_ready": bool(billing.LIVE_IDS.get("price_id") and billing.LIVE_IDS.get("portal_config_id"))}
           if billing.is_live() else {}),
        "step": 3,
        **({"build": os.environ["AW_BUILD"]} if os.environ.get("AW_BUILD") else {}),
        **({"runtime": os.environ.get("AW_RUNTIME", "cloud")} if os.environ.get("AW_CLOUD") == "1" else {}),
        "city_sources": _city_source_list(),
    }


def _city_source_list():
    """Public list of city agendas pages used as alert sources (city key + URL; both public)."""
    try:
        import aw_city_sources
        with db() as c:
            return [{"city_key": r["city_key"], "url": r["url"]} for r in aw_city_sources.sources(c)]
    except Exception:  # noqa: BLE001 - healthz never fails on this
        return []


# ---------------- Cloudflare Containers only (AW_CLOUD=1); inert (404) on the box ----------------
import hmac as _hmac
import subprocess as _sp
import threading as _th

_RUN = {"running": False, "started_at": None, "finished_at": None, "date": None, "result": None}
_RUN_LOCK = _th.Lock()


def _internal_ok(request) -> bool:
    sec = os.environ.get("AW_INTERNAL_SECRET", "")
    got = request.headers.get("x-aw-internal-secret", "")
    return bool(sec) and _hmac.compare_digest(sec, got)


def _callback(path, payload):
    """POST to the Worker (/__aw/...) so the DO guard learns the outcome. Best effort."""
    base = os.environ.get("AW_PERSIST_URL", "").rsplit("/persist", 1)[0]  # https://host/__aw
    if not base:
        return "no callback url"
    import urllib.request
    req = urllib.request.Request(base + path, method="POST", data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json",
                                          "X-AW-Internal-Secret": os.environ.get("AW_INTERNAL_SECRET", ""),
                                          "User-Agent": "AgendaWatch-Container/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return f"callback {r.status}"
    except Exception as ex:
        log.warning("run-done callback failed: %r", ex)
        return f"callback failed: {ex!r}"


def _mask(email):
    """a***@example.com style, for logs/status (never full user addresses in logs)."""
    local, _, dom = (email or "").partition("@")
    return (local[:1] + "***@" + dom) if dom else "***"


GUARD_LABEL = {"cap_pmn_requests": "PMN fetch requests", "cap_alert_emails": "alert emails",
               "cap_resend_sends": "Resend sends (sign-in links + alerts)",
               "cap_snapshot_uploads": "snapshot uploads", "cap_container_wakes": "container wakes"}


def guard_notify():
    """One owner notice per tripped cap per day, via the existing owner/failure-notice path
    (project inbox). Never emails users. Best effort."""
    with db() as c:
        pending = [t for t in aw_guard.trips(c) if not t["notified"]]
    for t in pending:
        with db() as c:
            if not aw_guard.claim_notice(c, t["key"]):
                continue
        label = GUARD_LABEL.get(t["key"], t["key"])
        subject = f"Agenda Watch: daily {label} limit reached"
        body = (f"Agenda Watch reached its daily safety limit for {label} at {t['at']} ({t['detail']}).\n\n"
                "That work is stopped for the rest of today (America/Denver) and resumes tomorrow. "
                "No user emails go out from a run that hit a limit.\n\n"
                f"Change the limits on the admin page: {base_url()}/admin#cost-safety\n")
        try:
            sent, how = send_owner_email(OWNER, subject, body, budget_exempt=True)
            log.warning("guard notice %s: %s", t["key"], how)
        except Exception as ex:  # noqa: BLE001
            log.error("guard notice %s failed: %r", t["key"], ex)


def deliver_user_alerts(plan, *, now=None):
    """Send one email per user from a clean run's plan (state/user_alert_plan.json) and record each
    item in user_alerts (sent / failed, Resend id, attempts) + alerts_sent (shown on My watch).
    One attempt per user per run; failures are retried on later runs up to aw_alerts.MAX_ATTEMPTS."""
    out = {"mode": plan.get("mode"), "users": 0, "emails_sent": 0, "emails_failed": 0, "items_sent": 0, "hits_sent": 0,
           "held_cities_off": plan.get("held_cities_off") or {}, "failures": [], "sent": []}
    if plan.get("mode") != "send":
        out.update({k: plan[k] for k in ("reason", "seeded", "error") if k in plan})
        return out
    # Daily budgets: the whole batch goes out or none of it (never a partial batch).
    # A user with both kinds gets two emails: upcoming agenda items, and past minutes (separate subjects).
    mails = [(u["email"], m) for u in plan.get("users") or [] if u.get("items")
             for m in aw_alerts.build_user_emails(u["items"], base_url())]
    batch = mails
    if batch and email_provider() == "resend":
        with db() as c:
            left_alerts = aw_guard.remaining(c, "cap_alert_emails")
            left_resend = aw_guard.remaining(c, "cap_resend_sends")
            short = [k for k, left in (("cap_alert_emails", left_alerts), ("cap_resend_sends", left_resend))
                     if left < len(batch)]
            for k in short:
                aw_guard.trip(c, k, f"batch of {len(batch)} alert email(s) held; {min(left_alerts, left_resend)} left today")
        if short:
            out.update(mode="held_budget", reason=f"daily limit reached ({', '.join(short)}): "
                       f"{len(batch)} alert email(s) held, none sent")
            log.error("Alert batch held: %s", out["reason"])
            guard_notify()
            return out
    ts = now_iso()
    out["users"] = len({e for e, _ in mails})
    for email, (kind, subject, body, groups) in mails:
        items = [h for g in groups for h in g["excerpts"]]
        try:
            html = aw_alerts.render_alert_html(kind, groups, base_url())
            ok, rid, detail = send_alert_email(email, subject, body, html=html)
        except Exception as ex:  # noqa: BLE001
            ok, rid, detail = False, None, f"{type(ex).__name__}"
        with db() as c:
            aw_guard.add(c, "alert_emails")
        with db() as c:
            err = None if ok else str(detail)[:300]
            if kind == "past_minutes":                   # dedupe by minutes identity, not by link
                for g in groups:
                    aw_alerts.record_minutes(c, email, g, "sent" if ok else "failed", resend_id=rid,
                                             error=err, now_iso=ts)
            else:
                for h in items:                          # every collapsed hit -> dedupe history
                    aw_alerts.record(c, email, h, "sent" if ok else "failed", resend_id=rid, error=err, now_iso=ts)
            if ok:
                for g in groups:
                    item = (f"Already discussed ({g.get('minutes_of')} minutes): "
                            + (g.get("agenda_item") or g["item_text"]) if kind == "past_minutes"
                            else g.get("agenda_item") or g["item_text"])
                    meeting = (f"{g.get('minutes_of')} (minutes)" if kind == "past_minutes"
                               else g.get("meeting_datetime") or g.get("meeting_date"))
                    c.execute("INSERT INTO alerts_sent(email,sent_date,city,item,link,meeting) VALUES(?,?,?,?,?,?)",
                              (email, ts[:10], g["city"], item[:2000], g.get("link") or "", meeting))
        if ok:
            out["emails_sent"] += 1
            out["items_sent"] += len(groups)
            out["hits_sent"] += len(items)
            out["sent"].append({"to": _mask(email), "kind": kind, "items": len(groups), "hits": len(items),
                                "resend_id": rid})
            log.info("Alert (%s) sent to %s: %d item(s), %d hit(s) (%s)", kind, _mask(email), len(groups),
                     len(items), rid)
        else:
            out["emails_failed"] += 1
            out["failures"].append({"to": _mask(email), "items": len(groups), "detail": str(detail)[:200]})
            log.error("Alert FAILED to %s: %s", _mask(email), str(detail)[:200])
    return out


def _run_notice(date, alerts, plan):
    """Owner/project inbox: short run notice when per-user alerts were attempted."""
    lines = [f"Agenda Watch daily run {date}: {alerts['emails_sent']} alert email(s) sent, "
             f"{alerts['emails_failed']} failed, {alerts['items_sent']} item(s).", ""]
    for u in plan.get("users") or []:
        groups = aw_alerts.group_by_meeting(u["items"]) if u["items"] else []
        lines.append(f"- {u['email']}: {len(groups)} item(s)")
        for g in groups:
            when = (f"{g.get('minutes_of')} minutes (already discussed)" if aw_alerts.is_past(g)
                    else g.get("meeting_date"))
            lines.append(f"    {g['city']} {when}: {(g.get('agenda_item') or g['item_text'])[:140]}"
                         + (f" ({len(g['excerpts'])} matches)" if len(g["excerpts"]) > 1 else ""))
    for f in alerts["failures"]:
        lines.append(f"FAILED {f['to']}: {f['detail']}")
    if alerts.get("held_cities_off"):
        lines.append(f"Held (city alerts off): {alerts['held_cities_off']}")
    return (f"Agenda Watch run {date}: {alerts['emails_sent']} alert(s) sent"
            + (f", {alerts['emails_failed']} FAILED" if alerts["emails_failed"] else ""), "\n".join(lines))


def _run_owner_job(date):
    """Background thread: run-owner (scan + per-user plan), send per-user alerts, mark the owner-watch
    items handled (history), run notice to the project inbox, snapshot, callback."""
    root = Path(__file__).resolve().parents[1]
    result = {"date": date}
    job_started = aw_store.now_iso()
    alerts = None
    plan_path = root / "state" / "user_alert_plan.json"
    holder = f"run-owner:{date}"
    beat = _th.Event()

    def heartbeat():  # keep the 30-min stale-safe lock fresh while this run is alive
        while not beat.wait(300):
            with db() as c:
                aw_guard.refresh_lock(c, holder)
    _th.Thread(target=heartbeat, daemon=True, name="run-lock-heartbeat").start()
    try:
        plan_path.unlink(missing_ok=True)              # never act on a previous run's plan
        p = _sp.run([sys.executable, "agenda_watch.py", "run-owner"], cwd=root,
                    capture_output=True, text=True, timeout=3000)
        result.update(exit_code=p.returncode, stdout_tail=p.stdout[-3000:], stderr_tail=p.stderr[-2000:])
        plan = json.loads(plan_path.read_text()) if plan_path.exists() else {"mode": "none", "users": []}
        if p.returncode not in (0, 4) and plan.get("mode") == "send":
            plan = {"mode": "withheld", "reason": f"run exit {p.returncode}", "users": []}
        alerts = deliver_user_alerts(plan)
        result["alerts"] = {k: v for k, v in alerts.items()}
        set_setting("last_alerts", json.dumps(dict(alerts, date=date, at=now_iso())))
        if alerts["emails_sent"] or alerts["emails_failed"]:
            subj, body = _run_notice(date, alerts, plan)
            try:
                sent, how = send_owner_email(OWNER, subj, body)   # project inbox (owner redirect)
                result["run_notice"] = how
            except Exception as ex:  # noqa: BLE001
                result["run_notice"] = f"FAILED {type(ex).__name__}"
        if p.returncode == 4:  # owner-watch items: record as handled so tomorrow's summary counts only new hits
            obs = sorted((root / "outbox").glob("email-*.json"))
            if obs:
                _sp.run([sys.executable, "agenda_watch.py", "mark-sent", str(obs[-1]), "per-user"],
                        cwd=root, check=True, capture_output=True, text=True)
        result["ok"] = p.returncode in (0, 3, 4) and not alerts["emails_failed"]
    except Exception as ex:
        log.exception("background run-owner failed")
        result.update(ok=False, error=repr(ex))
    finally:
        beat.set()
        with db() as c:
            aw_guard.release_lock(c, holder)
    try:
        with db() as c:
            result["guard"] = {"usage": aw_guard.usage(c), "trips": [t["key"] for t in aw_guard.trips(c)]}
        guard_notify()
    except Exception as ex:  # noqa: BLE001
        log.error("guard notify failed: %r", ex)
    with db() as c:
        lr = aw_store.latest_run(c)
        # This run's row (written by run-owner): alert emails sent -> outcome is not 'silence'.
        if lr and alerts and str(lr.get("started_at") or "") >= job_started[:16]:
            try:
                lr["outcome"] = aw_store.mark_alerts(c, lr["id"], alerts.get("emails_sent", 0),
                                                     alerts.get("items_sent", 0), alerts.get("hits_sent", 0))
                lr = aw_store.latest_run(c)
            except Exception as ex:  # noqa: BLE001
                log.error("run outcome alert update failed: %r", ex)
    result["fetch_run_id"] = lr["id"] if lr else None
    result["outcome"] = aw_store.outcome_label(lr) if lr else None
    (root / "state" / ".persist-now").touch()  # persist.py snapshots within ~1s
    time.sleep(5)
    with _RUN_LOCK:
        _RUN.update(running=False, finished_at=aw_store.now_iso(), result=result)
    result["callback"] = _callback("/run-done", {k: v for k, v in result.items() if not k.endswith("_tail")})
    log.info("background run-owner finished: %s", {k: v for k, v in result.items() if not k.endswith("_tail")})


def _alert_notices(c, now):
    return aw_alerts.notices_from_db(c, CFG, now, city_names=CITY_NAME)


@app.post("/internal/seed-alerts")
async def internal_seed_alerts(request: Request):
    """Cloud only, secret-gated. One-time dedupe seed: every upcoming hit each user's watch matches in
    the stored scan, plus the owner's sent_history.json, is recorded as already handled ('seeded').
    Sends nothing. Body {"dry_run": true} reports counts without writing."""
    if os.environ.get("AW_CLOUD") != "1":
        return PlainTextResponse("Not Found", status_code=404)
    if not _internal_ok(request):
        return PlainTextResponse("forbidden", status_code=403)
    try:
        dry = bool((await request.json()).get("dry_run"))
    except Exception:  # noqa: BLE001
        dry = False
    now = datetime.now(TZ)
    hist = json.loads(HISTORY_JSON.read_text()) if HISTORY_JSON.exists() else []
    name_to_key = {v: k for k, v in CITY_NAME.items()}
    con = sqlite3.connect(DB)
    try:
        aw_alerts.ensure_schema(con)
        notices = _alert_notices(con, now)
        preview = {}
        for w in aw_alerts.load_watches(con):
            hs = aw_alerts.match_user(notices, w, now, require_alerts_on=False)
            preview[_mask(w["email"])] = {
                "alerts_on": w["alerts_on"], "upcoming_matches": len(hs),
                "already_recorded": sum(1 for h in hs if aw_alerts.prior(con, w["email"], h)),
                "items": [f"{h['city']} {h['meeting_date']}: {h['item_text'][:100]}" for h in hs[:8]]}
        out = {"dry_run": dry, "upcoming_notices": len(notices), "users": preview}
        if not dry:
            if not aw_guard.acquire_lock(con, "seed-alerts"):
                return JSONResponse({"error": "run lock held", "lock": aw_guard.lock_info(con)}, status_code=503)
            ts = now_iso()
            out["seeded"] = {_mask(k): v for k, v in aw_alerts.seed(con, notices, now, now_iso=ts).items()}
            out["seeded_from_history"] = aw_alerts.seed_history(con, OWNER, hist, name_to_key, now_iso=ts)
            con.execute("INSERT OR IGNORE INTO schema_migrations(id, applied_at) VALUES('alert_seed_v1', ?)", (ts,))
            con.commit()
            aw_guard.release_lock(con, "seed-alerts")
            (Path(__file__).resolve().parents[1] / "state" / ".persist-now").touch()
        return JSONResponse(out)
    finally:
        con.close()


@app.post("/internal/test-alert")
async def internal_test_alert(request: Request):
    """Cloud only, secret-gated. ONE labeled test alert to ONE user (never the owner) through the real
    per-user path: that user's watch + matcher on the stored scan, Resend to their own address,
    recorded in user_alerts (status 'test') so the daily run never re-sends it. Ignores the user's
    alerts setting and dedupe (it is an explicit test). Body {"email": "...", "dry_run": bool}."""
    if os.environ.get("AW_CLOUD") != "1":
        return PlainTextResponse("Not Found", status_code=404)
    if not _internal_ok(request):
        return PlainTextResponse("forbidden", status_code=403)
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        body = {}
    email = str(body.get("email", "")).strip().lower()
    dry = bool(body.get("dry_run"))
    if not EMAIL_RE.match(email):
        return JSONResponse({"error": "bad email"}, status_code=400)
    if email == OWNER:
        return JSONResponse({"error": "refused: test alerts never go to the owner account"}, status_code=400)
    now = datetime.now(TZ)
    con = sqlite3.connect(DB)
    try:
        w = next((x for x in aw_alerts.load_watches(con) if x["email"] == email), None)
        if w is None:
            return JSONResponse({"error": "no watch for that user"}, status_code=404)
        hs = aw_alerts.match_user(_alert_notices(con, now), w, now,
                                  skip_cities=aw_alerts.off_cities(CFG), require_alerts_on=False)
        hs = [h for h in hs if not aw_alerts.is_past(h)]   # the test uses a real UPCOMING agenda item
    finally:
        con.close()
    if not hs:
        return JSONResponse({"error": "no real upcoming hit for this user's watch", "sent": False}, status_code=409)
    h = hs[0]
    item = {k: h.get(k) for k in ("fingerprint", "city", "city_key", "meeting_date", "meeting_body", "notice_title",
                                  "meeting_datetime", "item_text", "excerpt", "matched_terms", "link", "notice_url")}
    subject, text = aw_alerts.build_user_email([item], base_url(), test=True)
    html = aw_alerts.render_user_email_html([item], base_url(), test=True)
    out = {"to": email, "alerts_on": w["alerts_on"], "subject": subject, "city": item["city"],
           "meeting": item["meeting_datetime"], "item": item["item_text"][:300], "matched": item["matched_terms"],
           "link": item["link"], "dry_run": dry}
    if dry:
        out["body"] = text
        return JSONResponse(out)
    with db() as c:
        if aw_guard.remaining(c, "cap_alert_emails") < 1:
            return JSONResponse({"error": "daily alert email limit reached", "sent": False}, status_code=429)
        if not aw_guard.acquire_lock(c, "test-alert"):
            return JSONResponse({"error": "run lock held", "lock": aw_guard.lock_info(c), "sent": False},
                                status_code=503)
    try:
        ok, rid, detail = send_alert_email(email, subject, text, html=html)
    finally:
        with db() as c:
            aw_guard.add(c, "alert_emails")
            aw_guard.release_lock(c, "test-alert")
    with db() as c:
        aw_alerts.record(c, email, item, "test" if ok else "failed", resend_id=rid,
                         error=None if ok else str(detail)[:300])
        if ok:
            c.execute("INSERT INTO alerts_sent(email,sent_date,city,item,link,meeting) VALUES(?,?,?,?,?,?)",
                      (email, now_iso()[:10], item["city"], item["item_text"][:2000], item["link"] or "",
                       item["meeting_datetime"]))
    (Path(__file__).resolve().parents[1] / "state" / ".persist-now").touch()
    out.update(sent=ok, resend_id=rid, detail=detail)
    return JSONResponse(out)


@app.post("/internal/run-owner")
def internal_run_owner(request: Request, date: str = ""):
    if os.environ.get("AW_CLOUD") != "1":
        return PlainTextResponse("Not Found", status_code=404)
    if not _internal_ok(request):
        return PlainTextResponse("forbidden", status_code=403)
    date = date if re.fullmatch(r"\d{4}-\d{2}-\d{2}", date) else datetime.now(TZ).date().isoformat()
    with db() as c:
        enabled = aw_guard.get(c, "fetch_enabled")
    if not enabled:
        log.warning("run-owner %s refused: daily fetch is turned off (admin kill switch)", date)
        return JSONResponse({"ok": False, "disabled": True, "error": "daily fetch is turned off"}, status_code=423)
    with _RUN_LOCK:
        if _RUN["running"]:
            return JSONResponse({"ok": False, "error": "already running", "started_at": _RUN["started_at"]},
                                status_code=409)
        with db() as c:
            got = aw_guard.acquire_lock(c, f"run-owner:{date}")
            info = aw_guard.lock_info(c)
        if not got:
            return JSONResponse({"ok": False, "error": "run lock held", "lock": info}, status_code=503)
        _RUN.update(running=True, started_at=aw_store.now_iso(), finished_at=None, date=date, result=None)
    _th.Thread(target=_run_owner_job, args=(date,), daemon=True, name="run-owner").start()
    return JSONResponse({"ok": True, "started": True, "date": date, "started_at": _RUN["started_at"]},
                        status_code=202)
