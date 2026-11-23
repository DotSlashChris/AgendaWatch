"""City agendas pages as alert sources (owner order Oct 31 2026).

A source is legitimate only if BOTH are true:
  1. it is on that city's own official domain (CITY_DOMAINS, verified by hand; subdomains allowed), and
  2. the page is an agendas / packets / minutes page (its path or its title says so).
Nothing else is ever added.

The daily run (agenda_watch.run) calls run_into(cfg, res) after the PMN fetch:
  * auto_add(): links in the documents of PMN notices that produced a hit are validated per city and
    stored as new sources (logged in the run report);
  * scan(): every stored source page is fetched, its upcoming meeting rows are read, and the rows'
    agenda/packet PDFs are fetched. Each row becomes one notice-like entry appended to res["_docs"],
    so the per-user matcher (aw_alerts.plan) treats it exactly like a PMN notice with packets.
  * a failed page or document fetch goes to res["city_source_failures"] (run log / status report only):
    it never emails the owner and never withholds PMN alerts.
City requests use their own session and count toward the daily request budget, but never trip it
(at the cap they are skipped and reported instead).
"""
import datetime as dt
import hashlib
import re
import sqlite3
import threading
import time
from urllib.parse import urljoin, urlsplit, urlunsplit

import requests
from bs4 import BeautifulSoup

import agenda_watch as aw
import aw_cities
import aw_store

# City key -> the city's own official web domains. Verified Oct 31 2026 (redirects followed by hand):
#   ch: www.ch.utah.gov 301 -> www.cottonwoodheights.utah.gov (both appear in CH's own notices)
#   ogden: ogdencity.com -> ogdencity.gov; layton: laytoncity.org -> laytoncityutah.gov;
#   provo: provo.org -> provo.gov (agendas.provo.gov is a subdomain); orem: orem.org 301 -> orem.gov
# Verified Nov 3 2026: parkcity: www.parkcity.org 301 -> www.parkcity.gov ("Park City, UT");
#   stgeorge: www.sgcity.org 301 -> sgcity.org 301 -> sgcityutah.gov ("City of St. George, UT").
#   No source page is seeded for either (pages are only ever auto-added from PMN hit links).
# Not included (unverified): midvaleut.gov, danielutah.org. Third-party hosts (legistar.com,
# primegov.com, ...) are never a city's own domain.
CITY_DOMAINS = {
    "ch": ("cottonwoodheights.utah.gov", "ch.utah.gov"),
    "slc": ("slc.gov",),
    "sandy": ("sandy.utah.gov",),
    "draper": ("draperutah.gov",),
    "wj": ("westjordan.utah.gov",),
    "murray": ("murray.utah.gov",),
    "sj": ("sjc.utah.gov",),
    "midvale": ("midvale.utah.gov",),
    "ogden": ("ogdencity.gov", "ogdencity.com"),
    "layton": ("laytoncityutah.gov", "laytoncity.org"),
    "farmington": ("farmington.utah.gov",),
    "provo": ("provo.gov", "provo.org"),
    "orem": ("orem.gov", "orem.org"),
    "vineyard": ("vineyardutah.gov",),
    "daniel": ("danielutah.gov",),
    "parkcity": ("parkcity.gov", "parkcity.org"),
    "stgeorge": ("sgcityutah.gov", "sgcity.org"),
}

CH_PC_URL = ("https://www.cottonwoodheights.utah.gov/your-government/boards-and-commissions/"
             "planning-commission/agendas-packets-minutes")
SEEDS = [("ch", CH_PC_URL, "seed: owner order 2026-10-31")]

PAGE_WORDS = {"agenda", "agendas", "packet", "packets", "minutes"}
TITLE_RE = re.compile(r"\b(?:agendas?|packets?|minutes)\b", re.I)
# a link to one document is not an agendas page
DOC_EXT_RE = re.compile(r"\.(?:pdf|docx?|xlsx?|pptx?|rtf|txt|jpe?g|png|gif|mp3|mp4|m4a|zip)$", re.I)
DOC_PATH_RE = re.compile(r"/(?:home/)?showpublisheddocument/|/documentcenter/view/|/archive\.aspx\b|"
                         r"/files/|/download\b", re.I)
SOURCE_KIND = "city_page"

SCHEMA = """
CREATE TABLE IF NOT EXISTS city_sources(
  city_key TEXT NOT NULL,
  url TEXT NOT NULL,
  added_at TEXT NOT NULL,
  added_by TEXT NOT NULL,            -- 'seed: ...' | 'auto: <PMN notice url>'
  last_checked_at TEXT,
  last_ok_at TEXT,
  last_error TEXT,
  last_rows INTEGER,
  PRIMARY KEY(city_key, url)
);
-- Owner cap (Nov 4 2026): every document link seen on a source page, with when it was first seen.
-- baseline=1: present on the source's first successful scan (or seen with a past meeting date) -> never emails.
CREATE TABLE IF NOT EXISTS city_source_docs(
  city_key TEXT NOT NULL,
  source_url TEXT NOT NULL,
  doc_url TEXT NOT NULL,
  meeting_date TEXT,
  first_seen_at TEXT NOT NULL,
  baseline INTEGER NOT NULL,
  PRIMARY KEY(source_url, doc_url)
);
"""


# ---------------------------------------------------------------- validation
def _tokens(s):
    """'agendas-packets-minutes' / 'AgendaCenter' / 'planning_agendas' -> lower-case words."""
    s = re.sub(r"([a-z])([A-Z])", r"\1 \2", s or "")
    return {w.lower() for w in re.findall(r"[A-Za-z]+", s)}


def canonical(url):
    p = urlsplit((url or "").strip())
    path = p.path.rstrip("/") or "/"
    return urlunsplit((p.scheme.lower(), (p.hostname or "").lower(), path, p.query, ""))


def host_ok(city_key, url):
    """True when url's host is the city's own domain or a subdomain of it."""
    try:
        p = urlsplit((url or "").strip())
        host = (p.hostname or "").lower().rstrip(".")
        port = p.port
    except ValueError:
        return False
    if p.scheme.lower() not in ("http", "https") or not host or p.username or p.password:
        return False
    if port not in (None, 80, 443):
        return False
    return any(host == d or host.endswith("." + d) for d in CITY_DOMAINS.get(city_key, ()))


def validate(city_key, url, title=None):
    """(ok, reason). ok only for a city-domain agendas/packets/minutes page."""
    if city_key not in CITY_DOMAINS:
        return False, "not a catalog city"
    if not host_ok(city_key, url):
        return False, "not on the city's own domain"
    path = urlsplit(url.strip()).path
    if DOC_EXT_RE.search(path) or DOC_PATH_RE.search(path):
        return False, "a single document, not an agendas page"
    if _tokens(path) & PAGE_WORDS:
        return True, "city domain + agendas/packets/minutes path"
    if title and TITLE_RE.search(title):
        return True, "city domain + agendas/packets/minutes title"
    return False, "not an agendas, packets or minutes page"


# ---------------------------------------------------------------- storage
def ensure(con):
    con.executescript(SCHEMA)
    cols = {r[1] for r in con.execute("PRAGMA table_info(city_sources)")}
    for col, typ in (
        ("block_status", "TEXT"),
        ("block_count", "INTEGER NOT NULL DEFAULT 0"),
        ("block_http", "TEXT"),
        ("block_first_at", "TEXT"),
        ("block_last_at", "TEXT"),
        ("known_blocked_logged", "INTEGER NOT NULL DEFAULT 0"),
    ):
        if col not in cols:
            con.execute(f"ALTER TABLE city_sources ADD COLUMN {col} {typ}")
    for ck, url, by in SEEDS:
        ok, _ = validate(ck, url)
        if ok:
            con.execute("INSERT OR IGNORE INTO city_sources(city_key,url,added_at,added_by) VALUES(?,?,?,?)",
                        (ck, canonical(url), aw_store.now_iso(), by))


def sources(con):
    """Stored sources that still pass validate() (checked again on every read)."""
    try:
        con.row_factory = sqlite3.Row
        rows = [dict(r) for r in con.execute("SELECT * FROM city_sources ORDER BY city_key, url")]
    except sqlite3.Error:
        rows = []
    finally:
        con.row_factory = None
    return [r for r in rows if validate(r["city_key"], r["url"])[0]]


URL_RE = re.compile(r"(?:https?://|\bwww\.)[^\s<>\"'()\[\]]+", re.I)


def links_in(text):
    out = []
    for m in URL_RE.finditer(text or ""):
        u = m.group(0).rstrip(".,;:!?")
        if u.lower().startswith("www."):
            u = "https://" + u
        out.append(u)
    return out


def auto_add(con, res):
    """New sources from the PMN hits of this run: every link in a hit notice's documents that passes
    validate() for that notice's city. Returns [{city_key, url, from}] (added this run only)."""
    hit_notices = {h.get("notice_url") for h in res.get("hits") or []}
    have = {(r["city_key"], r["url"]) for r in sources(con)}
    added = []
    for n in res.get("_docs") or []:
        if n.get("source_kind") or n.get("notice_url") not in hit_notices:
            continue
        ck = n.get("city_key")
        for _name, _url, text in n.get("docs") or []:
            for u in links_in(text):
                ok, _ = validate(ck, u)
                cu = canonical(u)
                if ok and (ck, cu) not in have:
                    con.execute("INSERT OR IGNORE INTO city_sources(city_key,url,added_at,added_by) "
                                "VALUES(?,?,?,?)", (ck, cu, aw_store.now_iso(), "auto: " + n["notice_url"]))
                    have.add((ck, cu))
                    added.append({"city_key": ck, "url": cu, "from": n["notice_url"]})
    return added


# ---------------------------------------------------------------- fetching
CITY_S = requests.Session()
CITY_S.headers["User-Agent"] = aw.UA
CITY_S.headers["Accept"] = "text/html,application/xhtml+xml,application/pdf;q=0.9,*/*;q=0.8"

# Listing-page HTTP 403: after this many distinct America/Denver dates, report once as
# known-blocked (the city stays on PMN) and stop treating it as a fresh daily error.
# Recheck weekly in case the block lifts. A first successful scan is still baselined.
LISTING_BLOCK_CODES = frozenset({"403"})
LISTING_BLOCK_KNOWN_AFTER = 2
LISTING_BLOCK_RECHECK_DAYS = 7


class CityFetchError(Exception):
    pass


class BudgetSkip(CityFetchError):
    """Our own daily request cap was reached; the document was not requested."""


def short_error(ex):
    """'HTTP 403' / 'not a PDF' / 'timeout' / exception name: a short reason for logs and link-only emails."""
    s = str(ex)
    m = re.search(r"HTTP \d{3}", s)
    if m:
        return m.group(0)
    if s.startswith("not a PDF"):
        return "not a PDF (the site returned a web page instead)"
    if "Timeout" in s or "timed out" in s:
        return "timed out"
    return type(ex).__name__


def unreadable_item(city_name, city_key, page_url, row, label, doc_url, reason):
    d = row["date"]
    return {"city": city_name, "city_key": city_key, "notice_url": page_url, "meeting_body": row["body"],
            "meeting_date": d.isoformat(), "meeting_datetime": _long(d), "doc_label": label, "link": doc_url,
            "read_error": reason, "source_kind": SOURCE_KIND}


def _take():
    """Count one request against the daily budget; at the cap skip (never trips the PMN guard)."""
    b = aw.BUDGET
    if b["cap"] is None:
        return
    with b["lock"]:
        if b["used"] >= b["cap"]:
            raise BudgetSkip("daily request budget reached; city pages skipped")
        b["used"] += 1


def _get(url, headers=None, timeout=60, tries=2, stream=False):
    last = None
    for i in range(tries):
        _take()
        try:
            r = (CITY_S.get(url, timeout=timeout, headers=headers or {}, stream=True) if stream
                 else CITY_S.get(url, timeout=timeout, headers=headers or {}))
        except requests.RequestException as ex:
            last = repr(ex)
        else:
            if r.status_code in (200, 304):
                return r
            last = f"HTTP {r.status_code}"
            if r.status_code < 500:
                break
        if i + 1 < tries:
            time.sleep(2)
    raise CityFetchError(f"GET {url} failed: {last}")


def probe(url):
    """Reachability diagnostics for one city-domain URL (read-only GET, one try, no retries). Never raises."""
    if not any(host_ok(k, url) for k in CITY_DOMAINS):
        return {"url": url, "error": "not on a catalog city's own domain; not fetched"}
    try:
        _take()
        r = CITY_S.get(url, timeout=30)
    except Exception as ex:                                       # noqa: BLE001
        return {"url": url, "error": f"{type(ex).__name__}: {ex}"[:300]}
    body = r.content[:400]
    return {"url": url, "status": r.status_code, "server": r.headers.get("Server"),
            "content_type": r.headers.get("Content-Type"), "bytes": len(r.content),
            "is_pdf": r.content[:4] == b"%PDF",
            "snippet": "" if r.content[:4] == b"%PDF" else re.sub(r"\s+", " ", body.decode("utf-8", "replace"))[:300]}


def fetch_page(url):
    r = _get(url)
    return r.text


def _close(r):
    c = getattr(r, "close", None)
    if c:
        c()


def fetch_doc(url, page_url, prev):
    """(text, row_to_save or None, how) for one city document; same row shape as aw.fetch_document."""
    headers = {}
    if prev and prev.get("etag"):
        headers["If-None-Match"] = prev["etag"]
    if prev and prev.get("last_modified"):
        headers["If-Modified-Since"] = prev["last_modified"]
    r = _get(url, headers=headers, timeout=90, stream=True)
    if r.status_code == 304 and prev:
        _close(r)
        return prev["text"], None, "304"
    # Stream to disk in pieces (as aw.get_doc_to_file): a 100+ MB packet never sits in memory.
    aw.CACHE.mkdir(exist_ok=True)
    pdf = aw.CACHE / f"city-{threading.get_ident()}-{hashlib.sha1(url.encode()).hexdigest()[:12]}.pdf"
    try:
        length = 0
        try:
            it = r.iter_content(aw.DOC_CHUNK) if hasattr(r, "iter_content") else [r.content]
            with open(pdf, "wb") as f:
                for chunk in it:
                    if chunk:
                        f.write(chunk)
                        length += len(chunk)
        finally:
            _close(r)
        with open(pdf, "rb") as f:
            if f.read(4) != b"%PDF":
                raise CityFetchError(f"not a PDF: {url}")
        text = aw.extract_pdf_text(pdf)
    finally:
        pdf.unlink(missing_ok=True)
    row = {"url": url, "notice_url": page_url, "etag": r.headers.get("ETag"),
           "last_modified": r.headers.get("Last-Modified"), "length": length,
           "text_hash": hashlib.sha256(text.encode()).hexdigest(), "text": text}
    return text, row, "fetched"


# ---------------------------------------------------------------- page parsing
MONTHS = ("january february march april may june july august september october november december").split()
DATE_RE = re.compile(r"\b(" + "|".join(m.capitalize() for m in MONTHS) + r")\.?\s+(\d{1,2})\s*,\s*(\d{4})\b", re.I)
SKIP_LINK_RE = re.compile(r"\b(?:minutes|video|audio|youtube|recording|watch|livestream)\b", re.I)
CANCEL_RE = re.compile(r"\bcancel", re.I)


def _date(m):
    return dt.date(int(m.group(3)), MONTHS.index(m.group(1).lower()) + 1, int(m.group(2)))


def parse_page(html, page_url, city_key):
    """Meeting rows of an agendas page: [{date, body, cancelled, docs: [(label, url)], skipped: [...]}].
    A row is a table row / list item / paragraph holding a 'Month D, YYYY' date and links."""
    soup = BeautifulSoup(html or "", "html.parser")
    rows, seen = [], set()
    for el in soup.find_all(["tr", "li", "p"]):
        if el.find(["tr", "li", "p"]):
            continue                                   # innermost rows only
        text = el.get_text(" ", strip=True)
        m = DATE_RE.search(text)
        links = el.find_all("a", href=True)
        if not m or not links:
            continue
        labels = [a.get_text(" ", strip=True) for a in links]
        rest = text[m.end():]
        for lab in labels:
            if lab:
                rest = rest.replace(lab, " ")
        for cell in el.find_all(["td", "th"]):           # table row: the date cell names the meeting
            ct = cell.get_text(" ", strip=True)
            cm = DATE_RE.search(ct)
            if cm and not cell.find("a"):
                rest = ct[cm.end():]
                break
        # "| Agenda | Packet| Minutes" / "Agenda I Packet": separators and unlinked document words
        rest = re.sub(r"(?:\s*(?:\||\bI\b|\b(?:agenda|packet|minutes)\b))+\s*$", "", rest, flags=re.I)
        body = re.sub(r"\s+", " ", rest.replace("|", " ")).strip(" -–")
        if body in {a.get_text(" ", strip=True) for a in links}:
            body = ""
        docs, skipped = [], []
        for a, lab in zip(links, labels):
            u = urljoin(page_url, a["href"])
            if not lab or SKIP_LINK_RE.search(lab):
                continue
            if not host_ok(city_key, u):
                skipped.append({"label": lab, "url": u, "why": "not on the city's own domain"})
                continue
            if u not in [d for _, d in docs]:
                docs.append((lab, u))
        key = (_date(m), body, tuple(docs))
        if key in seen:
            continue
        seen.add(key)
        rows.append({"date": _date(m), "body": body or "Meeting", "cancelled": bool(CANCEL_RE.search(text)),
                     "docs": docs, "skipped": skipped})
    return rows


def _long(d):
    return f"{d.strftime('%B')} {d.day}, {d.year}"


def entry_for(city_name, city_key, page_url, row, docs):
    """One notice-like entry (the shape of agenda_watch._run's res['_docs'] items)."""
    d = row["date"]
    ev = dt.datetime.combine(d, dt.time(23, 59), aw.TZ)     # date only on the page: upcoming all meeting day
    return {"city": city_name, "city_key": city_key, "notice_url": page_url, "meeting_body": row["body"],
            "notice_title": f"{row['body']} — {_long(d)}", "meeting_datetime": _long(d),
            "event_iso": ev.isoformat(), "docs": docs, "source_kind": SOURCE_KIND}


def _parse_iso(s):
    try:
        t = dt.datetime.fromisoformat(s)
        return t if t.tzinfo else t.replace(tzinfo=aw.TZ)
    except (TypeError, ValueError):
        return None


def _http_code(ex):
    m = re.search(r"HTTP\s+(\d{3})", str(ex) or "")
    return m.group(1) if m else None


def _block_day(value):
    t = value if isinstance(value, dt.datetime) else _parse_iso(value)
    return t.astimezone(aw.TZ).date() if t else None


def should_skip_known_block(src, now):
    """True when this listing page is known-blocked and not yet due for a weekly recheck."""
    if (src.get("block_status") or "") != "known":
        return False
    last = _parse_iso(src.get("last_checked_at"))
    if not last:
        return False
    return (now - last) < dt.timedelta(days=LISTING_BLOCK_RECHECK_DAYS)


def known_block_message(url, short, name):
    return (f"city source known-blocked: {url} {short} "
            f"({name} is still covered by Utah Public Notice; "
            "this will not be reported as a daily error)")


def note_listing_ok(con, src, persist=True):
    """Clear known-block state after a successful listing-page fetch."""
    if not persist or not (src.get("block_status") or src.get("block_count")):
        return
    con.execute(
        """UPDATE city_sources SET block_status=NULL, block_count=0, block_http=NULL,
               block_first_at=NULL, block_last_at=NULL, known_blocked_logged=0
           WHERE city_key=? AND url=?""",
        (src["city_key"], src["url"]),
    )
    src.update(block_status=None, block_count=0, block_http=None,
               block_first_at=None, block_last_at=None, known_blocked_logged=0)


def note_listing_block(con, src, ex, now, name, persist=True):
    """Record a listing-page fetch failure. HTTP 403 becomes known-blocked after
    LISTING_BLOCK_KNOWN_AFTER distinct dates. Returns the run-log line, or None to stay quiet.
    Other errors (timeout, 500, ...) keep the existing daily 'city source blocked' line."""
    short = short_error(ex)
    first_msg = f"city source blocked: {src['url']} {short}"
    code = _http_code(ex)
    stamp = now.astimezone(aw.TZ).isoformat(timespec="seconds")
    if code not in LISTING_BLOCK_CODES:
        if persist:
            con.execute(
                "UPDATE city_sources SET last_checked_at=?, last_error=? WHERE city_key=? AND url=?",
                (stamp, f"{type(ex).__name__}: {ex}"[:300], src["city_key"], src["url"]),
            )
        return first_msg
    today = now.astimezone(aw.TZ).date()
    count = int(src.get("block_count") or 0)
    last_day = _block_day(src.get("block_last_at"))
    first_at = src.get("block_first_at") or stamp
    if count == 0:
        count = 1
    elif last_day and last_day < today:
        count += 1
    status = src.get("block_status") or "watching"
    logged = int(src.get("known_blocked_logged") or 0)
    msg = first_msg
    if count >= LISTING_BLOCK_KNOWN_AFTER:
        status = "known"
        if not logged:
            logged = 1
            msg = known_block_message(src["url"], short, name)
        else:
            msg = None
    else:
        status = "watching"
    if persist:
        con.execute(
            """UPDATE city_sources SET last_checked_at=?, last_error=?,
                   block_status=?, block_count=?, block_http=?,
                   block_first_at=?, block_last_at=?, known_blocked_logged=?
               WHERE city_key=? AND url=?""",
            (stamp, f"{type(ex).__name__}: {ex}"[:300], status, count, code,
             first_at, stamp, logged, src["city_key"], src["url"]),
        )
    src.update(block_status=status, block_count=count, block_http=code,
               block_first_at=first_at, block_last_at=stamp, known_blocked_logged=logged,
               last_checked_at=stamp)
    return msg


def record_seen(con, src, rows, now, today, persist=True):
    """Owner cap (Nov 4 2026). Records first_seen for every document link on a loaded source page and
    returns the set of doc URLs that MAY alert. A document may alert only when ALL hold:
      * the source already had a successful scan recorded in city_source_docs (on the first one, every
        listed document is baseline -> no backfill; this also covers a source added long ago that never
        loaded, e.g. Cottonwood Heights, and the first scan after this deploy);
      * the link was first seen on this scan or a later one that was not baseline, and first_seen_at is
        after the source's added_at;
      * its meeting date is upcoming (today..; past-dated rows are recorded as baseline, never eligible).
    State lives in the DB (persisted in snapshots), not the disk cache, so a fresh container does not
    make old documents look new. persist=False (read-only check) computes the same answer, writes nothing."""
    ck, surl = src["city_key"], src["url"]
    added = _parse_iso(src.get("added_at"))
    try:
        known = {r[0]: (r[1], r[2]) for r in con.execute(
            "SELECT doc_url, first_seen_at, baseline FROM city_source_docs WHERE source_url=?", (surl,))}
    except sqlite3.Error:                     # table missing (read-only check before ensure): first scan
        known, persist = {}, False
    first_scan = not known
    stamp = now.isoformat(timespec="seconds")
    eligible, new = set(), []
    for row in rows:
        for _label, durl in row["docs"]:
            upcoming = row["date"] >= today and not row["cancelled"]
            if durl in known:
                seen_at, base = known[durl]
            else:
                base = 1 if (first_scan or not upcoming) else 0
                seen_at = stamp
                known[durl] = (seen_at, base)
                new.append((ck, surl, durl, row["date"].isoformat(), seen_at, base))
            t = _parse_iso(seen_at)
            if not base and upcoming and t and (added is None or t > added):
                eligible.add(durl)
    if persist and new:
        con.executemany("INSERT OR IGNORE INTO city_source_docs VALUES(?,?,?,?,?,?)", new)
    return eligible, first_scan, len(new)


def scan(cfg, con, now, cache_docs=None, fetch_page_fn=None, fetch_doc_fn=None, persist=True):
    """Fetch every stored source; returns (entries, stats, failures, saves)."""
    cfg = aw_cities.expand_config(cfg)
    names = aw_cities.key_to_name(cfg)
    lookahead = dt.timedelta(days=cfg.get("lookahead_days", 90))
    today = now.astimezone(aw.TZ).date()
    fetch_page_fn = fetch_page_fn or fetch_page
    fetch_doc_fn = fetch_doc_fn or fetch_doc
    cache_docs = cache_docs or {}
    entries, failures, saves, checked, unreadable = [], [], [], [], []
    stats = {"pages": 0, "pages_ok": 0, "upcoming_rows": 0, "docs": 0, "docs_fetched": 0, "docs_304": 0,
             "docs_unreadable": 0, "per_source": [], "unreadable": unreadable, "known_blocked": []}
    for src in sources(con):
        ck, url = src["city_key"], src["url"]
        name = names.get(ck, ck)
        st = {"city_key": ck, "url": url, "ok": False, "rows": 0, "upcoming": [], "error": None}
        stats["pages"] += 1
        if should_skip_known_block(src, now):
            st["error"] = src.get("last_error") or "known-blocked"
            st["known_blocked"] = True
            stats["known_blocked"].append(url)
            stats["per_source"].append(st)
            continue
        try:
            html = fetch_page_fn(url)
            rows = parse_page(html, url, ck)
            st.update(ok=True, rows=len(rows))
            stats["pages_ok"] += 1
            note_listing_ok(con, src, persist)
            eligible, first_scan, n_new = record_seen(con, src, rows, now, today, persist)
            st.update(first_scan=first_scan, new_links=n_new, eligible=len(eligible))
        except Exception as ex:                       # any failure: report, never raise into the run
            st["error"] = f"{type(ex).__name__}: {ex}"[:300]
            # listing page blocked/failed: run log only. No documents are guessed and nobody is emailed.
            msg = note_listing_block(con, src, ex, now, name, persist)
            if msg:
                failures.append(msg)
            else:
                st["known_blocked"] = True
                stats["known_blocked"].append(url)
            stats["per_source"].append(st)
            continue
        for row in rows:
            if row["cancelled"] or not (today <= row["date"] <= today + lookahead) or not row["docs"]:
                continue
            stats["upcoming_rows"] += 1
            docs, info = [], {"date": row["date"].isoformat(), "body": row["body"], "docs": []}
            for label, durl in row["docs"]:
                stats["docs"] += 1
                if durl not in eligible:      # can never email (baseline / before source added): no request
                    stats["docs_capped"] = stats.get("docs_capped", 0) + 1
                    stats["docs_skipped"] = stats.get("docs_skipped", 0) + 1
                    info["docs"].append({"label": label, "url": durl, "how": "skipped (cap)", "eligible": False})
                    continue
                try:
                    text, save, how = fetch_doc_fn(durl, url, cache_docs.get(durl))
                except Exception as ex:
                    err = f"{type(ex).__name__}: {ex}"[:300]
                    failures.append(f"{name} {row['body']} {_long(row['date'])} {label} {durl}: {err}")
                    info["docs"].append({"label": label, "url": durl, "error": err})
                    # The listing page loaded and lists this document, but it could not be read. A link
                    # that was never read before -> link-only alert (aw_alerts.link_only_hits). A document
                    # read on an earlier run already went through normal matching; our own request cap is
                    # not "could not be read".
                    if durl not in cache_docs and durl in eligible and not isinstance(ex, BudgetSkip):
                        stats["docs_unreadable"] += 1
                        unreadable.append(unreadable_item(name, ck, url, row, label, durl, short_error(ex)))
                    continue
                stats["docs_fetched" if how == "fetched" else "docs_304"] += 1
                if save:
                    saves.append(save)
                checked.append(durl)
                info["docs"].append({"label": label, "url": durl, "how": how, "chars": len(text or ""),
                                     "eligible": True,
                                     "sha256": hashlib.sha256((text or "").encode()).hexdigest()[:16]})
                docs.append((f"{row['body']} {label} {_long(row['date'])}", durl, text))
            st["upcoming"].append(info)
            if docs:
                entries.append(entry_for(name, ck, url, row, docs))
        stats["per_source"].append(st)
        if persist:
            con.execute("UPDATE city_sources SET last_checked_at=?, last_ok_at=?, last_error=NULL, last_rows=? "
                        "WHERE city_key=? AND url=?", (aw_store.now_iso(), aw_store.now_iso(), st["rows"], ck, url))
    if persist and (saves or checked):
        aw_store.save_documents(con, saves, checked)
    return entries, stats, failures


def run_into(cfg, res, db=None, now=None):
    """Daily-run hook: auto-add sources from PMN hits, scan sources, append entries to res['_docs'].
    Failures go to res['city_source_failures'] only (never res['failures'])."""
    now = now or aw.dt.datetime.now(aw.TZ)      # aw.dt: the run clock (frozen in tests)
    with sqlite3.connect(db or aw.DB) as con:
        ensure(con)
        added = auto_add(con, res)
        _fps, cache_docs = aw_store.load_cache(con)
        entries, stats, failures = scan(cfg, con, now, cache_docs)
    res.setdefault("_docs", []).extend(entries)
    stats["added"] = added
    stats["sources"] = [s["url"] for s in stats["per_source"]]
    res["city_sources"] = stats
    res["city_source_failures"] = failures
    res["city_unreadable"] = stats.get("unreadable") or []     # link-only alert candidates (aw_alerts.plan)
    return res
