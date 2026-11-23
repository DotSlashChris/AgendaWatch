"""Per-user alerts: match each user's watch against the morning scan, dedupe, plan, record.

Shared by the daily job (agenda_watch.py run-owner -> state/user_alert_plan.json) and the web app
(sends the plan via Resend, records outcomes; secret-gated seed / test routes).

Rules (product):
  * Upcoming meetings only. Past meetings are never emailed.
  * Only cities enabled on that user's watch, and only when the user's alerts are on.
  * Per user city: send an item if (a) it names the user's street (a street matches only in its own
    city), OR (b) it matches the keyword rules (weak terms need a rental term in the same item, bare
    STR must read as rental, minutes skipped). A street never hides a city-wide hit. Keyword items
    that name some other address are sent too, flagged names_other_address (owner to decide).
  * Cities listed in config "alerts_off_cities" (default West Jordan, Layton) never email anyone
    until the owner removes them from that list.
  * Dedupe: one row per (user, hit fingerprint) in user_alerts. Fingerprint = city + meeting date +
    the matched agenda item's normalized words. An amended notice (new PDF / new notice URL) with the
    same item at the same meeting is the same hit; a re-worded item at the same meeting counts as the
    same hit while >= SAME_ITEM_OVERLAP of its words match an earlier one (typo fixes, renumbering),
    and as a new hit when the wording materially changes or the meeting moves to another date.
"""
import datetime as dt
import hashlib
import html as html_mod
import json
import re
import sqlite3

SAME_ITEM_OVERLAP = 0.8
MAX_ATTEMPTS = 3            # a failing user send is tried on at most 3 daily runs, then left alone
DEFAULT_OFF_CITIES = ("wj", "layton")

SCHEMA = """
CREATE TABLE IF NOT EXISTS user_alerts(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  email TEXT NOT NULL,
  fingerprint TEXT NOT NULL,
  city_key TEXT NOT NULL,
  meeting_date TEXT NOT NULL,          -- YYYY-MM-DD (America/Denver)
  item_text TEXT NOT NULL,
  notice_url TEXT NOT NULL DEFAULT '',
  link TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL,                -- seeded | sent | failed | test
  attempts INTEGER NOT NULL DEFAULT 0,
  resend_id TEXT,
  error TEXT,
  first_at TEXT NOT NULL,
  last_at TEXT NOT NULL,
  UNIQUE(email, fingerprint));
CREATE INDEX IF NOT EXISTS user_alerts_email_city_date ON user_alerts(email, city_key, meeting_date);
"""


_AW = None   # set by agenda_watch on import (also when it runs as __main__)


def _aw():
    if _AW is not None:
        return _AW
    import agenda_watch  # lazy: agenda_watch imports this module too
    return agenda_watch


def ensure_schema(con):
    con.executescript(SCHEMA)


def norm_words(text):
    return re.findall(r"[a-z0-9]+", (text or "").lower())


def fingerprint(city_key, meeting_date, item_text):
    return hashlib.sha1(f"{city_key}|{(meeting_date or '')[:10]}|{' '.join(norm_words(item_text))}".encode()).hexdigest()


def _overlap(a, b):
    a, b = set(w for w in a if len(w) >= 3), set(w for w in b if len(w) >= 3)
    return len(a & b) / max(1, min(len(a), len(b)))


def off_cities(cfg):
    v = (cfg or {}).get("alerts_off_cities")
    return set(DEFAULT_OFF_CITIES if v is None else v)


def parse_keywords(text):
    text = str(text or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if not text:
        return []
    if "\n" in text:
        return [k.strip() for k in text.split("\n") if k.strip()]
    return [k.strip() for k in re.split(r",+", text) if k.strip()]


def load_watches(con):
    """Every user's watch: [{email, alerts_on, keywords:[..], cities:{key:{enabled, street}}}]."""
    con.row_factory = sqlite3.Row
    out = []
    for w in con.execute("SELECT email, keywords, alerts_on FROM watches ORDER BY email"):
        cities = {r["city_key"]: {"enabled": bool(r["enabled"]), "street": (r["street"] or "").strip()}
                  for r in con.execute("SELECT city_key, enabled, street FROM watch_cities WHERE email=?",
                                       (w["email"],))}
        out.append({"email": w["email"], "alerts_on": bool(w["alerts_on"]),
                    "keywords": parse_keywords(w["keywords"]), "cities": cities})
    con.row_factory = None
    return out


# ---------------------------------------------------------------- agenda item titles + excerpts
# A hit inside a packet/attachment is often definition or staff-report text, not the agenda item.
# Map each match back to the agenda item it belongs to (from the agenda's own item list), and quote
# the matched sentence separately as an excerpt with its source + page.
# "3.4 Project …", "2. …", and Provo's "5<TAB>An ordinance … (26-069)" (bare number + tab).
AGENDA_ITEM_RE = re.compile(r"^[ \t]*(\d{1,2}\.\d{1,2}(?=\.?[ \t])\.?|\d{1,2}\.|\d{1,2}(?=\t))[ \t]+(\S.*)$", re.M)
# ZTA-26-004 / CUP-25-005 style, plus Provo's council file numbers "(26-069)" and planning
# application numbers "PLOTA20250243".
CASE_RE = re.compile(r"\b[A-Z]{2,5}-\d{2,4}-\d{2,5}\b|(?<=\()\d{2}-\d{3}(?=\))|\bPL[A-Z]{2,5}\d{8}\b")
SESSION_RE = re.compile(r"^[ \t]*\d{1,2}:\d{2}\s*[ap]\.?\s?m\.?[ \t]+[^\n]*\b(?:session|meeting)\b[^\n]*$", re.I | re.M)
NOTICE_DOC = "PMN notice text"
# Orem's notice puts the sub-item number on its own line: "1.3\n\nDISCUSSION - Short Term Rental Continued".
# Only dotted numbers (1.3), so bare line/page numbers are never read as agenda items.
SPLIT_ITEM_RE = re.compile(r"^[ \t]*(\d{1,2}\.\d{1,2})\.?[ \t]*\r?\n(?:[ \t]*\r?\n)?[ \t]*([A-Z][^\n]{3,200})$", re.M)


def _item_lines(text, lo=0, hi=None):
    """(start, num, heading) for every agenda-item line: AGENDA_ITEM_RE plus split '1.3' / heading lines."""
    text = text or ""
    hi = len(text) if hi is None else hi
    out = [(m.start(), m.group(1), m.group(2), m.end()) for m in AGENDA_ITEM_RE.finditer(text, lo, hi)]
    taken = {a for a, *_ in out}
    out += [(m.start(), m.group(1), m.group(2), m.end()) for m in SPLIT_ITEM_RE.finditer(text, lo, hi)
            if m.start() not in taken]
    return sorted(out)
# "2104 East Fort Union Blvd", "2193 E. 6525 S.", "100 Main St", "100 Example St"
ADDRESS_LIKE_RE = re.compile(
    r"\b\d{2,6}\s+(?:(?:north|south|east|west|[nsew])\.?\s+)?"
    r"(?:\d{2,5}\s+(?:north|south|east|west|[nsew])\b\.?"
    r"|(?:[A-Z][\w']+\s+){1,3}(?:St|Street|Dr|Drive|Rd|Road|Ave|Avenue|Blvd|Boulevard|Way|Ln|Lane|Cir|Circle|Ct|Court|Pl|Place|Pkwy)\b\.?)",
    re.I)


def _clean(t):
    return re.sub(r"\s+", " ", (t or "").replace("\f", " ")).strip()


def agenda_items(text):
    """Agenda item list from an agenda's text: [{num, heading, desc, cases, session}].
    Items are numbered lines ("3.4 Project ZTA-26-004", "2.0 CUP-25-005 -Old Mill Demolition");
    desc = the item's first sentence on the following lines (the source's own wording)."""
    text = text or ""
    ms = _item_lines(text)
    sessions = [(m.start(), _clean(m.group(0))) for m in SESSION_RE.finditer(text)]
    out = []
    for i, (mstart, mnum, mhead, mend) in enumerate(ms):
        end = ms[i + 1][0] if i + 1 < len(ms) else min(len(text), mend + 1500)
        body = text[mend:end]
        body = re.split(r"\n[ \t]*\n", body, maxsplit=1)[0]       # up to a blank line (none: heading only)
        sm = SESSION_RE.search(body)
        if sm:
            body = body[:sm.start()]                                        # ...or the next session header
        desc = _clean(body)
        if PRESENTER_LINE_RE.match(desc):
            desc = ""                                   # "Presenter: D. Jacob Summers" is not a description
        first = re.match(r"(.+?[.!?])(?:\s+(?=[A-Z])|$)", desc)
        desc = (first.group(1) if first else desc)[:300]
        heading = _clean(mhead)
        session = next((t for pos, t in reversed(sessions) if pos < mstart), None)
        out.append({"num": mnum.rstrip("."), "heading": heading, "desc": desc,
                    "cases": sorted(set(CASE_RE.findall(heading + " " + desc))), "session": session,
                    "pos": mstart, "end": end})
    return out


# a line that opens the next section of the agenda, not a description of the item above it
NEXT_SECTION_RE = re.compile(
    r"^(?:closed meeting|closed session|executive session|adjourn|adjournment|recess)\b"
    r"|\bmotion to (?:close|adjourn) the meeting\b", re.I)
# the heading is already a whole agenda line: it ends a sentence, optionally followed by a case/file number
_HEAD_DONE_RE = re.compile(r"[.!?]\s*(?:\([^()]{1,40}\)\s*)*$")


def item_title(it):
    """The agenda line itself: number + heading; a description only when the heading is a bare label
    ("3.4 Project ZTA-26-004 — Accessory Dwelling Units ..."), never the next section's text."""
    head = f"{it['num']} {PRESENTER_RE.sub('', it['heading'])}".strip()   # drop "(Ed Kennedy - 10 Minutes)"
    desc = it.get("desc") or ""
    if (desc and desc.lower() not in head.lower() and not _HEAD_DONE_RE.search(head)
            and not NEXT_SECTION_RE.search(desc)):
        return f"{head} — {desc}"
    return head


def _agenda_for_notice(n):
    """The meeting's agenda items: from the PMN notice text, else the attachment that reads most
    like an agenda (most numbered items carrying case numbers)."""
    docs = n.get("docs") or []
    best = []
    for dname, _url, text in docs:
        items = agenda_items(text if dname == NOTICE_DOC else (text or "")[:20000])
        items = [it for it in items if not re.match(r"^[A-Z]\.?$", it["num"])]
        if dname == NOTICE_DOC and len(items) >= 2:
            return items
        if sum(1 for it in items if it["cases"]) > sum(1 for it in best if it["cases"]):
            best = items
    return best


def _locate_item(items, text, pos, how=None):
    """Which agenda item does offset pos in this document belong to?
    1) The doc contains the agenda list itself (notice / agenda PDF / packet cover): the item whose
       heading line precedes pos within its span.  2) Otherwise (packet body): the nearest case number
       before pos, if it is one of the agenda's case numbers. Else None (not identified)."""
    if not items:
        return None
    norm = lambda t: re.sub(r"\W+", " ", t).strip().lower()
    best = None                       # the nearest preceding heading wins (short agendas: the 200-char
    for it, nxt in zip(items, items[1:] + [None]):   # floor of item 1 must not swallow item 4.1)
        key = norm(f"{it['num']} {it['heading']}")
        for mstart, mnum, mhead, _e in _item_lines(text):
            if norm(f"{mnum.rstrip('.')} {mhead}") == key and mstart <= pos:
                stop = mstart + max(200, (it["end"] - it["pos"]) + 200)
                # the agenda listing in a packet ends at its page: a staff report on the next page is not
                # part of the last agenda line (Vineyard 10/13: page 3 report is not "7.4 NUCAC")
                if pos <= stop and "\f" not in text[mstart:pos] and (best is None or mstart > best[0]):
                    best = (mstart, it)
    if best:
        if how is not None:
            how.append("listing")
        return best[1]
    case_items = {c: it for it in items for c in it["cases"]}
    prev = [m for m in CASE_RE.finditer(text[:pos])]
    if prev and prev[-1].group(0) in case_items:
        m = prev[-1]
        # A case number far back in a long packet (Provo: 90 pages) belongs to some earlier section;
        # better "not identified" than the wrong item.
        if text.count("\f", m.start(), pos) <= CASE_MAX_PAGES and pos - m.start() <= CASE_MAX_CHARS:
            if how is not None:
                how.append("case")
            return case_items[m.group(0)]
    return None


CASE_MAX_PAGES = 30
CASE_MAX_CHARS = 60000

# Packet body pages: the section a hit sits in is named by its page's title line ("Short-term Rental
# Report"). Map it to the agenda line whose topic words all appear in that title (4.1 "Discuss
# Short-Term Rentals"), never to whichever agenda heading happens to precede it in the PDF.
TOPIC_STOP = {"discuss", "discussion", "presentation", "approve", "approval", "consider", "consideration",
              "review", "update", "report", "item", "items", "the", "of", "and", "a", "an", "to", "for", "in",
              "on", "with", "by", "city", "council", "continued", "possible", "action", "regarding", "re",
              "resolution", "ordinance", "public", "hearing", "work", "session", "staff", "min", "minutes"}
TOPIC_BACK_PAGES = 2


def _stem(w):
    w = w.lower()
    return w[:-1] if len(w) > 3 and w.endswith("s") and not w.endswith("ss") else w


def _topic_words(t):
    return {_stem(w) for w in re.findall(r"[A-Za-z]+", PRESENTER_RE.sub("", t or "")) if _stem(w) not in TOPIC_STOP
            and len(w) > 1}


def _page_title(page):
    """First real line of a packet page (skips minutes line numbers, page numbers, blank lines)."""
    for l in page.split("\n")[:40]:
        c = _clean(l)
        if c and not re.fullmatch(r"[\d\s.]+|page \d+( of \d+)?", c, re.I):
            return c if len(c) <= 120 else None
    return None


def _is_title(line):
    """'Budget Memo' / 'SHORT-TERM RENTAL REPORT': a short heading line, not running body text."""
    ws = re.findall(r"[A-Za-z][\w'-]*", line or "")
    if not ws or len(line) > 80 or re.search(r"[.,;:]$", line) or not line[0].isupper():
        return False
    caps = [w for w in ws if w[0].isupper() or w.lower() in TOPIC_STOP]
    return len(caps) == len(ws)


def _topic_item(items, text, pos):
    """The agenda item whose topic words all (>= 2) appear in the title line of the packet page holding
    pos (or of up to TOPIC_BACK_PAGES pages before it, for a multi-page report). Unique match only."""
    if not items or "\f" not in (text or ""):
        return None
    pages = text.split("\f")
    pg = text.count("\f", 0, pos)
    for k in range(pg, max(-1, pg - TOPIC_BACK_PAGES - 1), -1):
        title = _page_title(pages[k])
        if not title:
            continue
        tw = _topic_words(title)
        hits = [it for it in items if len(_topic_words(it["heading"])) >= 2 and _topic_words(it["heading"]) <= tw]
        if hits and len({it["num"] for it in hits}) == 1:
            return hits[0]                  # the notice text may list the agenda twice (agenda + PDF excerpt)
        if hits or _is_title(title) or (k < pg and _item_lines(pages[k])):
            return None                     # ambiguous, another titled section, or back into an agenda listing
    return None


ABBREV_RE = re.compile(r"\b(?:Mr|Mrs|Ms|Dr|St|No|Jr|Sr)\.$")


def matched_sentence(text, start, end, limit=420):
    """The sentence containing [start, end): back to the previous sentence end / blank line / page
    break, forward to the next sentence end. Long sentences are trimmed around the match."""
    text = text or ""
    lo = 0
    base = max(0, start - 1500)
    for m in re.finditer(r"[.!?]['\")\u201d\u2019]?\s+|\n[ \t]*\n|\f", text[base:start]):
        if ABBREV_RE.search(text[max(0, base + m.start() - 5):base + m.start() + 1]):
            continue                                   # "Mr. Kennedy" is not a sentence end
        lo = base + m.end()
    m = re.search(r"[.!?](?=['\")\u201d\u2019]?(?:\s|$))|\n[ \t]*\n|\f", text[end:end + 1500])
    hi = end + (m.end() if m else min(len(text) - end, 400))
    s = _clean(text[lo:hi])
    if len(s) > limit:
        mpos = _clean(text[lo:start]).__len__()
        a = max(0, mpos - limit // 2)
        s = ("…" if a else "") + s[a:a + limit].strip() + "…"
    return s


DEF_SENTENCE_RE = re.compile(
    r"^\W{0,3}[\w][^.;:]{0,80}?[\u201d\"'\u2019]?\s+(?:means|shall mean|is defined as|is hereby defined as|refers to)\b"
    r"|\b(?:is|are) defined as\b", re.I)
DEF_HEADING_RE = re.compile(r"^[ \t]*(?:[\d.]+[ \t]+)?(?:definitions?|defined terms)\b[^\n]{0,40}$", re.I | re.M)


def is_definition(sentence, text=None, pos=None):
    """'“Short-term rental” means …', 'X is defined as …', or text inside a Definitions section
    (a 'Definitions' heading within the preceding ~1500 chars on the same page)."""
    if DEF_SENTENCE_RE.search(sentence or ""):
        return True
    if text is not None and pos is not None:
        lo = max(0, pos - 1500, text.rfind("\f", 0, pos) + 1)
        heads = list(DEF_HEADING_RE.finditer(text, lo, pos))
        if heads:
            # still in that section: no numbered section heading (e.g. "19.75.030 Standards") after it
            after = text[heads[-1].end():pos]
            if not re.search(r"^[ \t]*\d+(?:\.\d+)+[ \t]+[A-Z][^\n]{0,60}$", after, re.M):
                return True
    return False

# A packet that reproduces an agenda section ("WORK SESSION" / "4.1 Discuss Short-Term Rentals
# (Ed Kennedy – 10 Minutes)") - e.g. inside draft minutes - names its own agenda line even when that
# line is not on the notice's item list. Used only when _locate_item found nothing.
SECTION_LINE_RE = re.compile(r"^[ \t]*(?:\d{1,2}\.[ \t]*)?([A-Z][A-Z&/ \-]{3,40}[A-Z])[ \t]*$")
SUBITEM_RE = re.compile(r"^[ \t]*(\d{1,2}\.\d{1,2})\.?[ \t]+([A-Z][^\n]{3,200})$", re.M)
PRESENTER_RE = re.compile(r"\s*\([^()]*\b\d+\s*(?:Minutes?|min)\.?\)\s*$", re.I)   # "(Ed Kennedy - 10 Minutes)", "(90 min)"
PRESENTER_LINE_RE = re.compile(r"^Presenters?\s*:", re.I)
HEADING_MAX_CHARS = 4000


def nearest_agenda_heading(text, pos):
    """'Work session 4.1 Discuss Short-Term Rentals' for a hit under that heading, else None.
    The heading must be a dotted sub-item (4.1) directly under an all-caps section line
    (WORK SESSION, CONSENT ITEMS, PUBLIC HEARINGS ...), within HEADING_MAX_CHARS and one page break."""
    text = text or ""
    lo = max(0, pos - HEADING_MAX_CHARS)
    found = None
    for m in SUBITEM_RE.finditer(text, lo, min(len(text), pos + 300)):
        if m.start() > pos:
            break
        above = [l for l in text[max(0, m.start() - 300):m.start()].split("\n") if l.strip()][-2:]
        sec = next((SECTION_LINE_RE.match(l) for l in reversed(above) if SECTION_LINE_RE.match(l)), None)
        if sec and text.count("\f", m.start(), pos) <= 1:
            found = (sec.group(1).strip(), m.group(1), PRESENTER_RE.sub("", _clean(m.group(2))))
    if not found:
        return None
    section, num, title = found
    return f"{section.capitalize()} {num} {title}"


MINUTES_RE = re.compile(r"MINUTES OF[^\n]{0,80}\n(?:[^\n]*\n){0,3}?[^\n]*?([A-Z][a-z]+ \d{1,2}, \d{4})")
# A draft-minutes heading line: "DRAFT MINUTES FOR JULY 15, 2026", "DRAFT MINUTES OF THE ... JULY 15, 2026",
# "Draft Minutes – July 15, 2026". Must start the line; a bare "minutes" in prose never counts.
_MONTHS = "January|February|March|April|May|June|July|August|September|October|November|December"
DRAFT_MINUTES_RE = re.compile(
    r"^[ \t\f]*DRAFT[ \t]+MINUTES\b(?:[ \t]+(?:FOR|OF)\b[^\n]{0,80}?|[ \t]*[-\u2013\u2014:,][ \t]*[^\n]{0,80}?)"
    r"(?:\n[^\n]{0,80}?){0,2}?\b((?:" + _MONTHS + r")[ \t]+\d{1,2},?[ \t]+\d{4})", re.I | re.M)


def _minutes_heads(text, pos):
    """(start, end, 'July 15, 2026') for every minutes heading before pos."""
    out = [(m.start(), m.end(), m.group(1)) for m in MINUTES_RE.finditer(text, 0, pos)]
    for m in DRAFT_MINUTES_RE.finditer(text, 0, pos):
        d = re.match(r"([A-Za-z]+)[ \t]+(\d{1,2}),?[ \t]+(\d{4})", m.group(1))
        out.append((m.start(), m.end(), f"{d.group(1).capitalize()} {int(d.group(2))}, {d.group(3)}"))
    return sorted(out)
MINUTES_END_RE = re.compile(r"STAFF REPORT|NOTICE OF A|^AGENDA$", re.M)


def minutes_context(text, pos, items=None):
    """(minutes date, this agenda's minutes-approval line or None) when pos sits inside a set of
    meeting minutes reproduced in the packet; else None."""
    text = text or ""
    ms = _minutes_heads(text, pos)
    if not ms:
        return None
    mstart, mend, date = ms[-1]
    if MINUTES_END_RE.search(text, mend, pos) or text.count("\f", mstart, pos) > 15:
        return None
    on = next((item_title(it) for it in items or []
               if "minutes" in it["heading"].lower() and date.split(",")[0] in it["heading"]), None)
    return date, on


# A packet section that approves prior minutes: "6.1. Approval of the October 13, 2026, City Council
# Meeting Minutes" (numbered or not) and the minutes pages that follow it, up to the next agenda line.
MINUTES_APPROVAL_RE = re.compile(
    r"^[ \t\f]*(?:\d{1,2}(?:\.\d{1,2})?\.?[ \t]+)?(?:approv\w*|adopt\w*|accept\w*|consider\w*)\b[^\n]{0,120}\bminutes\b",
    re.I | re.M)
MINUTES_APPROVAL_MAX_PAGES = 15


def minutes_approval_section(text, pos):
    """The 'Approval of ... Minutes' line whose packet section contains pos (the line itself, or the
    pages after it up to the next numbered agenda line / staff report / notice), else None."""
    text = text or ""
    line_start = text.rfind("\n", 0, pos) + 1
    eol = text.find("\n", pos)
    ms = [m for m in MINUTES_APPROVAL_RE.finditer(text, 0, len(text) if eol < 0 else eol) if m.start() <= pos]
    if not ms:
        return None
    m = ms[-1]
    if m.start() >= line_start or pos <= m.end():
        return _clean(m.group(0))                          # the hit is on the approval line itself
    if text.count("\f", m.start(), pos) > MINUTES_APPROVAL_MAX_PAGES or MINUTES_END_RE.search(text, m.end(), pos):
        return None
    nxt = next((a for a in AGENDA_ITEM_RE.finditer(text, m.end(), pos)
                if not MINUTES_APPROVAL_RE.match(a.group(0))), None)
    return None if nxt else _clean(m.group(0))


def past_minutes_kind(h):
    """Why a hit is past-minutes (not an upcoming agenda line): 'minutes' (text from a prior meeting's
    minutes reproduced in the packet) or 'minutes_approval' (the packet section approving prior minutes)."""
    if h.get("minutes_of"):
        return "minutes"
    if h.get("minutes_approval"):
        return "minutes_approval"
    return None


DATE_RE = re.compile(r"\b(January|February|March|April|May|June|July|August|September|October|November|December)"
                     r"\s+(\d{1,2}),?\s+(\d{4})\b")


def _iso_date(s):
    m = DATE_RE.search(s or "")
    if not m:
        return None
    try:
        return dt.datetime.strptime(f"{m.group(1)} {m.group(2)} {m.group(3)}", "%B %d %Y").date().isoformat()
    except ValueError:
        return None


def _long_date(s):
    m = DATE_RE.search(s or "")
    return f"{m.group(1)} {int(m.group(2))}, {m.group(3)}" if m else None


def minutes_key(city_key, minutes_date, body=None):
    """Identity of one past meeting's minutes (NOT the PDF link): city + past meeting date (+ body).
    The same minutes in a later approval packet (new link) have the same key -> never emailed twice."""
    iso = _iso_date(minutes_date) or _norm(minutes_date or "")
    return "minutes|" + "|".join(x for x in (city_key or "", iso, _norm(body or "")) if x is not None)


def classify(h):
    """kind = 'upcoming' (agenda line of the upcoming meeting) or 'past_minutes'."""
    h["past_minutes"] = past_minutes_kind(h)
    if h["past_minutes"] == "minutes_approval" and not h.get("minutes_of"):
        h["minutes_of"] = _long_date(h["minutes_approval"])
    h["kind"] = "past_minutes" if h["past_minutes"] else "upcoming"
    return h


def page_of(text, pos):
    """1-based page for pdftotext output (pages separated by form feeds); None if no page breaks."""
    return (text.count("\f", 0, pos) + 1) if "\f" in (text or "") else None


def _term_rx(label):
    aw = _aw()
    pats = [aw.KEYWORD_PATTERNS[label][0] if label in aw.KEYWORD_PATTERNS else r"\b" + re.escape(label) + r"\b"]
    if label == "STR":                                   # STR is the short form of short-term rental
        pats.append(aw.KEYWORD_PATTERNS["short-term rental"][0])
    return re.compile("|".join(f"(?:{p})" for p in pats), re.I)


def _term_item(items, h, it):
    """Packet body attributed only by the nearest earlier case number: when the matched term is not in
    that agenda line but is in exactly one other agenda line, the hit belongs to that line (CH Nov 4
    2026: the STR staff memo follows the CUP-26-019 report, but only 3.3 ZTA-26-006 is about STRs)."""
    if h.get("match_type") != "keyword" or not h.get("matched"):
        return None
    rx = _term_rx(h["matched"])
    line = lambda x: f"{x['heading']} {x['desc']}"
    if rx.search(line(it)):
        return None
    other = {x["num"]: x for x in items if rx.search(line(x))}
    return next(iter(other.values())) if len(other) == 1 else None


def annotate(h, n, items=None):
    """Add agenda_item (title or None), session, excerpt_sentence, excerpt_source to a raw hit."""
    items = _agenda_for_notice(n) if items is None else items
    text = next((t for d, u, t in n.get("docs") or [] if u == h.get("link") and d == h.get("document")), None)
    if text is None or h.get("start") is None:
        h.update(agenda_item=None, session=None, excerpt_sentence=_clean(h.get("excerpt", "")).strip(". "),
                 excerpt_source=None, minutes_approval=None)
        return classify(h)
    how = []
    it = _locate_item(items, text, h["start"], how)
    if it and how == ["case"]:
        it = _term_item(items, h, it) or it
    if not it and h.get("document") != NOTICE_DOC:
        it = _topic_item(items, text, h["start"])
    h["agenda_item"] = item_title(it) if it else None
    h["session"] = it["session"] if it else None
    if not it:
        h["agenda_item"] = nearest_agenda_heading(text, h["start"])
    mins = minutes_context(text, h["start"], items)
    if mins:
        h["minutes_of"], h["on_agenda_as"] = mins
    h["minutes_approval"] = minutes_approval_section(text, h["start"]) if not mins else None
    h["excerpt_sentence"] = matched_sentence(text, h["start"], h["end"])
    h["is_definition"] = is_definition(h["excerpt_sentence"], text, h["start"])
    if h.get("document") == NOTICE_DOC:
        h["excerpt_source"] = "the PMN notice (agenda text)"
    else:
        name = h.get("document") or (h.get("link") or "").rsplit("/", 1)[-1]
        pg = page_of(text, h["start"])
        h["excerpt_source"] = f"the packet/attachment {name}" + (f", page {pg}" if pg else "")
    return classify(h)


def match_user(notices, watch, now, skip_cities=(), require_alerts_on=True):
    """Merged upcoming hits for one user (best first), each with 'fingerprint' and 'meeting_date'.
    notices: [{city, city_key, notice_url, meeting_body, notice_title, meeting_datetime, event_iso,
               docs: [(doc_name, url, text)]}]"""
    aw = _aw()
    if require_alerts_on and not watch.get("alerts_on"):
        return []
    on = {k: v for k, v in (watch.get("cities") or {}).items() if v.get("enabled") and k not in skip_cities}
    if not on:
        return []
    kw = aw.build_keyword_matchers({"keywords": watch.get("keywords") or []})
    hits = []
    for n in notices:
        st = on.get(n.get("city_key"))
        if st is None:
            continue
        event = dt.datetime.fromisoformat(n["event_iso"])
        if event < now:
            continue                                   # past meetings are never alerts
        street = st.get("street") or ""
        matchers = list(kw) + aw.build_street_matchers(street)
        seen, nh = set(), []
        for dname, durl, text in n.get("docs") or []:
            for h in aw.find_hits(text or "", matchers, doc_name=dname):
                k = (h["item_text"].lower(), h["matched"])
                if k in seen:
                    continue
                seen.add(k)
                h.update({"city": n["city"], "city_key": n["city_key"], "meeting_body": n.get("meeting_body") or "",
                          "notice_title": n.get("notice_title") or "", "meeting_datetime": n.get("meeting_datetime") or "",
                          "event_iso": n["event_iso"], "upcoming": True, "document": dname, "link": durl,
                          "notice_url": n["notice_url"]})
                if n.get("source_kind"):
                    h["source_kind"] = n["source_kind"]          # city agendas page (aw_city_sources)
                h["key"] = aw.hit_key(n["city"], durl, h["item_text"])
                nh.append(h)
        items = _agenda_for_notice(n)
        for h in nh:
            annotate(h, n, items)
        # A street never hides a city-wide hit: send items that (a) name the user's street (street
        # matchers exist only for this city), OR (b) match the keyword rules. Keyword hits whose item
        # names some OTHER address are still sent, but flagged for the owner's review.
        street_items = {h["item_text"].lower() for h in nh if h["match_type"] == "address"}
        for h in nh:
            h["names_user_street"] = h["item_text"].lower() in street_items
            h["names_other_address"] = (not h["names_user_street"]) and bool(ADDRESS_LIKE_RE.search(h["item_text"]))
        hits.extend(nh)
    # merge per city (same wording in two cities stays two hits), then best first
    merged = []
    for ck in dict.fromkeys(h["city_key"] for h in hits):
        merged.extend(aw.merge_hits([h for h in hits if h["city_key"] == ck], []))
    merged.sort(key=lambda h: (h["score"], h["event_iso"]), reverse=True)
    # merging may swap the link to another document: take title/excerpt/link from one consistent
    # source hit (prefer one whose agenda item was identified, then a PDF).
    by_key = {}
    for h in hits:
        by_key.setdefault(h["key"], []).append(h)
    for m in merged:
        src = [r for k in m.get("keys", [m["key"]]) for r in by_key.get(k, [])]
        if src:
            best = max(src, key=lambda r: (r.get("kind") != "past_minutes", r.get("agenda_item") is not None,
                                           "/pmn/files/" in (r.get("link") or "")))
            for k in ("agenda_item", "session", "excerpt_sentence", "excerpt_source", "link", "document", "is_definition",
                      "minutes_of", "on_agenda_as", "minutes_approval", "past_minutes", "kind"):
                m[k] = best.get(k)          # an upcoming agenda line wins over a minutes quote of the same text
            m["names_user_street"] = any(r.get("names_user_street") for r in src)
            m["names_other_address"] = not m["names_user_street"] and any(r.get("names_other_address") for r in src)
    for h in merged:
        h["meeting_date"] = h["event_iso"][:10]
        h["fingerprint"] = fingerprint(h["city_key"], h["meeting_date"], h["item_text"])
        h.setdefault("kind", "upcoming")
        h["minutes_key"] = (minutes_key(h["city_key"], h["minutes_of"], h.get("meeting_body"))
                            if h["kind"] == "past_minutes" else None)
    return merged


def prior(con, email, h):
    """Existing user_alerts row for this hit (exact fingerprint, or same city+meeting with
    >= SAME_ITEM_OVERLAP word overlap), else None."""
    con.row_factory = sqlite3.Row
    try:
        r = con.execute("SELECT * FROM user_alerts WHERE email=? AND fingerprint=?", (email, h["fingerprint"])).fetchone()
        if r:
            return dict(r)
        if h.get("kind") == LINK_ONLY:        # link-only: once per document link, exact key only
            return None
        words = norm_words(h["item_text"])
        # link-only rows never stand in for a real match (a later readable PDF still sends its alert)
        for r in con.execute("SELECT * FROM user_alerts WHERE email=? AND city_key=? AND meeting_date=? "
                             "AND fingerprint NOT LIKE 'linkonly|%'",
                             (email, h["city_key"], h["meeting_date"])):
            if _overlap(words, norm_words(r["item_text"])) >= SAME_ITEM_OVERLAP:
                return dict(r)
    finally:
        con.row_factory = None
    return None


def should_send(row):
    """No row -> new hit. A failed row is retried until MAX_ATTEMPTS. Anything else is done."""
    return row is None or (row["status"] == "failed" and row["attempts"] < MAX_ATTEMPTS)


def record(con, email, h, status, *, resend_id=None, error=None, now_iso=None, count_attempt=True):
    now_iso = now_iso or dt.datetime.now(dt.timezone.utc).astimezone().isoformat(timespec="seconds")
    row = prior(con, email, h)
    if row:
        con.execute("UPDATE user_alerts SET status=?, attempts=attempts+?, resend_id=COALESCE(?, resend_id), "
                    "error=?, last_at=? WHERE id=?",
                    (status, 1 if count_attempt else 0, resend_id, error, now_iso, row["id"]))
    else:
        con.execute("INSERT INTO user_alerts(email,fingerprint,city_key,meeting_date,item_text,notice_url,link,"
                    "status,attempts,resend_id,error,first_at,last_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (email, h["fingerprint"], h["city_key"], h["meeting_date"], h["item_text"][:2000],
                     h.get("notice_url") or "", h.get("link") or "", status, 1 if count_attempt else 0,
                     resend_id, error, now_iso, now_iso))


def _brief(h):
    return {k: h.get(k) for k in ("fingerprint", "city", "city_key", "meeting_date", "meeting_body", "notice_title",
                                  "meeting_datetime", "item_text", "excerpt", "matched_terms", "link", "notice_url",
                                  "agenda_item", "session", "excerpt_sentence", "excerpt_source",
                                  "names_user_street", "names_other_address", "is_definition",
                                  "minutes_of", "on_agenda_as", "minutes_approval", "past_minutes", "kind",
                                  "minutes_key")} | ({"source_kind": h["source_kind"]} if h.get("source_kind") else {})


def is_past(h):
    return h.get("kind") == "past_minutes" and bool(h.get("minutes_key"))


def minutes_prior(con, email, key):
    """This user's user_alerts row for one past meeting's minutes (fingerprint = minutes key), else None."""
    con.row_factory = sqlite3.Row
    try:
        r = con.execute("SELECT * FROM user_alerts WHERE email=? AND fingerprint=?", (email, key)).fetchone()
        return dict(r) if r else None
    finally:
        con.row_factory = None


def pending(con, email, h):
    """Should this hit go out? Upcoming: per-hit dedupe. Past minutes: once per minutes identity."""
    return should_send(minutes_prior(con, email, h["minutes_key"]) if is_past(h) else prior(con, email, h))


def record_minutes(con, email, g, status, *, resend_id=None, error=None, now_iso=None, count_attempt=True):
    """Sent history for one past meeting's minutes, keyed by identity (city + date + body), not link."""
    now_iso = now_iso or dt.datetime.now(dt.timezone.utc).astimezone().isoformat(timespec="seconds")
    key = g["minutes_key"]
    if minutes_prior(con, email, key):
        con.execute("UPDATE user_alerts SET status=?, attempts=attempts+?, resend_id=COALESCE(?, resend_id), "
                    "error=?, last_at=? WHERE email=? AND fingerprint=?",
                    (status, 1 if count_attempt else 0, resend_id, error, now_iso, email, key))
        return
    label = f"Minutes of the {g.get('minutes_of') or ''} {g.get('city') or ''} {g.get('meeting_body') or ''} meeting"
    con.execute("INSERT INTO user_alerts(email,fingerprint,city_key,meeting_date,item_text,notice_url,link,"
                "status,attempts,resend_id,error,first_at,last_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (email, key, g.get("city_key") or "", _iso_date(g.get("minutes_of")) or "", " ".join(label.split()),
                 g.get("notice_url") or "", g.get("link") or "", status, 1 if count_attempt else 0,
                 resend_id, error, now_iso, now_iso))


# Past emails sent before minutes had their own identity: the minutes they covered are marked sent.
# 2026-10-27 Vineyard email (4 hits from the Oct 13 minutes in the Oct 27 packet) = the Oct 13 minutes.
MINUTES_BACKFILL = [{"id": "minutes_key_backfill_20261027", "resend_id": "01a17dbf-0912-795c-9f97-6673c49a7ad6",
                     "city_key": "vineyard", "city": "Vineyard", "meeting_body": "City Council",
                     "minutes_of": "October 13, 2026"}]


def backfill_minutes_keys(con, entries=None):
    """Idempotent: for each entry, every user whose user_alerts rows were sent with that Resend id gets a
    'sent' row for the minutes key (same Resend id and time). Returns the number of rows inserted."""
    ensure_schema(con)
    con.execute("CREATE TABLE IF NOT EXISTS schema_migrations(id TEXT PRIMARY KEY, applied_at TEXT NOT NULL)")
    n = 0
    for e in MINUTES_BACKFILL if entries is None else entries:
        key = minutes_key(e["city_key"], e["minutes_of"], e.get("meeting_body"))
        for email, first_at, nu, link in con.execute(
                "SELECT email, MIN(first_at), MIN(notice_url), MIN(link) FROM user_alerts WHERE resend_id=? "
                "AND status='sent' AND city_key=? AND fingerprint NOT LIKE 'minutes|%' GROUP BY email",
                (e["resend_id"], e["city_key"])).fetchall():
            if minutes_prior(con, email, key) is None:
                record_minutes(con, email, dict(e, minutes_key=key, notice_url=nu, link=link), "sent",
                               resend_id=e["resend_id"], now_iso=first_at)
                n += 1
        con.execute("INSERT OR IGNORE INTO schema_migrations(id, applied_at) VALUES(?, ?)",
                    (e["id"], dt.datetime.now(dt.timezone.utc).astimezone().isoformat(timespec="seconds")))
    con.commit()
    return n


def meeting_key(h):
    """One meeting = one email item: same city + same PMN notice + same meeting date. Past minutes:
    one item per past meeting (minutes identity), whichever packet carried them."""
    if is_past(h):
        return ("minutes", h["minutes_key"])
    return (h.get("city_key") or h.get("city"), h.get("notice_url"), (h.get("meeting_date") or "")[:10])


# ---------------------------------------------------------------- link-only (document could not be read)
LINK_ONLY = "link_only"


def link_only_fingerprint(city_key, doc_url):
    return f"linkonly|{city_key}|{url_key(doc_url)}"


def link_only_hits(unreadable, watch, now, skip_cities=(), require_alerts_on=True):
    """City agendas page loaded and lists a document for an UPCOMING meeting, but the document could not
    be read. One link-only item per document link for a user watching that city (any keywords or street):
    no excerpt, no matched terms, never a claim that it matches. Never for past meetings."""
    if require_alerts_on and not watch.get("alerts_on"):
        return []
    today = now.astimezone(_aw().TZ).date().isoformat()
    on = {k for k, v in (watch.get("cities") or {}).items() if v.get("enabled")}
    out = []
    for u in unreadable or []:
        ck = u["city_key"]
        if ck not in on or ck in skip_cities or (u.get("meeting_date") or "") < today:
            continue
        label = f"{u.get('meeting_body') or ''} {u.get('doc_label') or 'document'} {u.get('meeting_datetime') or ''}"
        label = " ".join(label.split())
        out.append(dict(u, kind=LINK_ONLY, fingerprint=link_only_fingerprint(ck, u["link"]),
                        item_text=f"Could not be read: {label} {u['link']}", agenda_item=label,
                        notice_title=f"{u.get('meeting_body')} — {u.get('meeting_datetime')}"))
    return out


def is_link_only(h):
    return h.get("kind") == LINK_ONLY


def plan(con, notices, cfg, now, max_items=5, link_only=None):
    """Per-user send plan for one clean run. Returns {"users": [{email, items:[...]}], "held": {...}}.
    link_only: aw_city_sources unreadable-document items (sent as their own 'could not be read' email)."""
    ensure_schema(con)
    skip = off_cities(cfg)
    users, held = [], {}
    for w in load_watches(con):
        if not w["alerts_on"]:
            continue
        # upcoming agenda lines: per-hit dedupe; past minutes: once per minutes identity (city+date+body)
        items = [h for h in match_user(notices, w, now, skip_cities=skip) if pending(con, w["email"], h)]
        lo = [h for h in link_only_hits(link_only, w, now, skip_cities=skip) if pending(con, w["email"], h)]
        # hits in alerts-off cities (owner switch): counted for the status report only, never sent
        not_off = {k for k in w["cities"] if k not in skip}
        for h in match_user(notices, w, now, skip_cities=not_off):
            if pending(con, w["email"], h):
                held[h["city_key"]] = held.get(h["city_key"], 0) + 1
        if items:
            meetings = list(dict.fromkeys(meeting_key(h) for h in items))
            keep = set(meetings[:max_items])
            users.append({"email": w["email"], "items": [_brief(h) for h in items if meeting_key(h) in keep],
                          "more": max(0, len(meetings) - max_items)})
        if lo:
            u = next((x for x in users if x["email"] == w["email"]), None)
            if u is None:
                u = {"email": w["email"], "items": [], "more": 0}
                users.append(u)
            u["items"] += [_brief_link_only(h) for h in lo]
        if link_only:
            for h in link_only_hits(link_only, w, now, skip_cities=not_off):
                if pending(con, w["email"], h):
                    held[h["city_key"]] = held.get(h["city_key"], 0) + 1
    return {"users": users, "held_cities_off": held}


def _brief_link_only(h):
    return {k: h.get(k) for k in ("fingerprint", "kind", "city", "city_key", "meeting_date", "meeting_body",
                                  "meeting_datetime", "notice_title", "item_text", "agenda_item", "link",
                                  "notice_url", "doc_label", "read_error", "source_kind")}


def group_link_only(items):
    """One group per meeting (city + agendas page + date); every document link in it."""
    groups = {}
    for h in items:
        k = (h["city_key"], h.get("notice_url"), h.get("meeting_date"))
        g = groups.get(k)
        if g is None:
            g = groups[k] = {"city": h["city"], "city_key": h["city_key"], "meeting_body": h.get("meeting_body"),
                             "meeting_date": h.get("meeting_date"), "meeting_datetime": h.get("meeting_datetime"),
                             "notice_url": h.get("notice_url"), "link": h.get("link"),
                             "source_kind": h.get("source_kind"), "excerpts": []}
        g["excerpts"].append(h)
    for g in groups.values():
        labels = [h.get("doc_label") or "document" for h in g["excerpts"]]
        g["doc_word"] = _doc_word(labels)
        g["agenda_item"] = g["item_text"] = (f"New {g['doc_word']} posted (could not be read): "
                                             f"{meeting_name(g['city'], g['meeting_body'])} {g['meeting_datetime']}")
    return list(groups.values())


def _doc_word(labels):
    low = " ".join(labels).lower()
    return "packet" if "packet" in low else "agenda" if "agenda" in low else "document"


def build_link_only_email(items, base_url, test=False):
    """'Could not be read' email: the listing page shows a new document for an upcoming meeting, the
    document itself could not be downloaded or read. Links only: no excerpts, no matched terms."""
    groups = group_link_only(items)
    if len(groups) == 1:
        g = groups[0]
        subject = (f"Agenda Watch: new {meeting_name(g['city'], g['meeting_body'])} {g['doc_word']} posted "
                   f"(could not be read)")
    else:
        subject = f"Agenda Watch: {len(groups)} new city meeting documents posted (could not be read)"
    parts = []
    if test:
        subject = "[Test] " + subject
    noun = "an upcoming meeting" if len(groups) == 1 else f"{len(groups)} upcoming meetings"
    one = len(items) == 1
    parts += [f"{'A new document was' if one else 'New documents were'} posted for {noun} in a city you watch, "
              f"on {_posted_on(groups)}, but Agenda Watch could not read {'it' if one else 'them'}. "
              "This email is not a match: only the links are below.", ""]
    for i, g in enumerate(groups, 1):
        parts.append(f"{i}. {meeting_name(g['city'], g['meeting_body'])} — {g['meeting_datetime']}")
        for h in g["excerpts"]:
            parts.append(f"   {h.get('doc_label') or 'Document'}: {h['link']}")
        reasons = sorted({h.get("read_error") or "" for h in g["excerpts"]} - {""})
        one = len(g["excerpts"]) == 1
        parts.append(f"   {'The document' if one else 'These documents'} could not be read"
                     + (f" ({', '.join(reasons)})" if reasons else "")
                     + f", so {'it was' if one else 'they were'} not checked for rental items.")
        parts.append(f"   City agendas page: {g['notice_url']}")
        parts.append("")
    parts += ["--", FOOTER.format(base=base_url.rstrip("/"))]
    return subject, "\n".join(parts)


def seed(con, notices, now, now_iso=None):
    """Mark every upcoming hit each user's watch matches right now as already handled ('seeded'),
    whatever their alerts setting and including alerts-off cities, plus every item in the owner's
    sent_history.json (already emailed to the project inbox). Existing rows are left alone."""
    ensure_schema(con)
    out = {}
    for w in load_watches(con):
        n = 0
        for h in match_user(notices, w, now, require_alerts_on=False):
            if is_past(h):
                if minutes_prior(con, w["email"], h["minutes_key"]) is None:
                    record_minutes(con, w["email"], h, "seeded", now_iso=now_iso, count_attempt=False)
                    n += 1
            elif prior(con, w["email"], h) is None:
                record(con, w["email"], h, "seeded", now_iso=now_iso, count_attempt=False)
                n += 1
        out[w["email"]] = n
    return out


def seed_history(con, owner, history, name_to_key, now_iso=None):
    """sent_history.json rows (owner, project inbox) -> 'seeded' rows for the owner."""
    ensure_schema(con)
    n = 0
    for r in history or []:
        key = (name_to_key or {}).get(r.get("city"))
        md = _meeting_date(r.get("meeting") or r.get("date") or "")
        if not key or not md or not r.get("item"):
            continue
        h = {"city_key": key, "meeting_date": md, "item_text": r["item"], "link": r.get("link") or "",
             "fingerprint": fingerprint(key, md, r["item"])}
        if prior(con, owner, h) is None:
            record(con, owner, h, "seeded", now_iso=now_iso, count_attempt=False)
            n += 1
    return n


def _meeting_date(s):
    """'October 21, 2026 05:00 PM' / 'October 21, 2026' / '2026-10-21' -> '2026-10-21' (else None)."""
    s = (s or "").strip()
    if re.match(r"\d{4}-\d{2}-\d{2}", s):
        return s[:10]
    m = re.match(r"([A-Za-z]+ \d{1,2}, \d{4})", s)
    if m:
        try:
            return dt.datetime.strptime(m.group(1), "%B %d, %Y").date().isoformat()
        except ValueError:
            return None
    return None


def notices_from_db(con, cfg, now, city_names=None):
    """Rebuild upcoming notices (with document texts) from the stored scan: notices.text (PMN notice
    text + PDF excerpt, truncated) plus each attachment's full text from the documents cache.
    Used by the one-time seed and the test-alert route; the daily run uses its in-memory texts."""
    con.row_factory = sqlite3.Row
    try:
        out = []
        for r in con.execute("SELECT * FROM notices WHERE meeting_date >= ? ORDER BY meeting_date",
                             (now.isoformat(),)):
            docs = [("PMN notice text", r["notice_url"], r["text"] or "")]
            for u in json.loads(r["pdf_urls"] or "[]"):
                d = con.execute("SELECT text FROM documents WHERE url=?", (u,)).fetchone()
                if d:
                    docs.append((u.rsplit("/", 1)[-1], u, d["text"] or ""))
            ev = dt.datetime.fromisoformat(r["meeting_date"])
            out.append({"city": (city_names or {}).get(r["city_key"], r["city_key"]), "city_key": r["city_key"],
                        "notice_url": r["notice_url"], "meeting_body": r["body"], "notice_title": r["title"],
                        "meeting_datetime": ev.strftime("%B %d, %Y %I:%M %p").replace(" 0", " "),
                        "event_iso": r["meeting_date"], "docs": docs})
        return out
    finally:
        con.row_factory = None


FOOTER = ("Agenda Watch flags public agenda text and links to the source. It is not legal advice.\n"
          "You get this because alerts are on for these cities on your watch. Change your watch or turn alerts off: {base}/watch")


def group_by_meeting(items):
    """All hits from one meeting become ONE email item (display only; every hit is still recorded
    for dedupe). The item leads with the best identified agenda line; other identified lines of the
    same meeting are listed as 'Also on this agenda'; every hit's sentence goes under Excerpt."""
    out, idx = [], {}
    for h in items:
        k = meeting_key(h)
        if k in idx:
            idx[k]["excerpts"].append(h)
            continue
        g = dict(h, excerpts=[h])
        idx[k] = g
        out.append(g)
    for g in out:
        ex = g["excerpts"]
        lead = next((e for e in ex if e.get("agenda_item")), ex[0])
        g["agenda_item"], g["session"] = lead.get("agenda_item"), lead.get("session")
        for f in ("minutes_of", "on_agenda_as"):
            g[f] = lead.get(f) or next((e.get(f) for e in ex if e.get(f)), None)
        g["also_items"] = list(dict.fromkeys(e["agenda_item"] for e in ex
                                             if e.get("agenda_item") and e["agenda_item"] != g["agenda_item"]))
        g["matched_terms"] = list(dict.fromkeys(t for e in ex for t in (e.get("matched_terms") or [])))
    return out


group_by_agenda_item = group_by_meeting   # old name


def _shown_excerpts(g):
    """Excerpts to show for an entry: definition-style ones are dropped. If every excerpt is a
    definition, the item stays but no excerpt line is shown (terms from all its matches)."""
    ex = g.get("excerpts") or [g]
    keep = [e for e in ex if not e.get("is_definition")]
    terms = keep if keep else ex
    return keep, list(dict.fromkeys(t for e in terms for t in (e.get("matched_terms") or [])))


def _norm(t):
    return re.sub(r"\W+", " ", t or "").strip().lower()


def _src_parts(src):
    m = re.match(r"^(.*?)(?:, page (\d+))?$", src or "")
    return (m.group(1), int(m.group(2)) if m.group(2) else None) if src else (None, None)


def display_item_title(h):
    """Visible item heading. Missing or 'not identified' titles become 'Matching item' (wording only)."""
    title = (h.get("agenda_item") or "").strip()
    if not title or title.lower() == "not identified":
        return "Matching item"
    return title


def format_when(md):
    """'December 2, 2026 05:00 PM' -> 'December 2, 2026 at 5:00 PM'. Unparseable strings pass through."""
    md = (md or "").strip()
    m = re.match(r"^(.*?)(?:\s+(\d{1,2}):(\d{2})\s*([AP]M))?\s*$", md, re.I)
    if not m:
        return md
    date, hh, mm, ap = m.groups()
    date = date.strip(" —-")
    if not hh:
        return date
    return f"{date} at {int(hh)}:{mm} {ap.upper()}"


def _subject_date(md):
    md = (md or "").strip()
    return md.rsplit(" ", 2)[0] if md and ":" in md else md


def split_excerpt(ex):
    """Split an excerpt that contains bullet characters into a lead-in plus list items."""
    ex = (ex or "").strip().strip('"').strip()
    if not ex or not re.search(r"[•●‣▪◦]", ex):
        return ex, []
    parts = re.split(r"\s*[•●‣▪◦]\s*", ex)
    lead = (parts[0] or "").strip()
    bullets = [p.strip() for p in parts[1:] if p.strip()]
    return (lead, bullets) if bullets else (ex, [])


def format_excerpt_lines(ex, src=None, indent="   "):
    """Plain-text excerpt block: one quoted sentence, or a lead-in plus real bullet lines."""
    lead, bullets = split_excerpt(ex)
    src_bit = f" (from {src})" if src else ""
    if not bullets:
        return [f'{indent}Excerpt{src_bit}: "{lead}"']
    lines = [f"{indent}Excerpt{src_bit}:"]
    if lead:
        lines.append(f"{indent}  {lead}")
    for b in bullets:
        lines.append(f"{indent}  • {b}")
    return lines


def upcoming_subject(items, test=False):
    """Professional subject for an upcoming-hit email (wording only)."""
    groups = group_by_meeting(items)
    if len(groups) == 1:
        g = groups[0]
        name = meeting_name(g["city"], g.get("meeting_body"))
        date = _subject_date(g.get("meeting_datetime") or g.get("meeting_date") or "")
        subject = f"Agenda Watch: {name} — {date}".strip(" —")
    else:
        cities = list(dict.fromkeys(g["city"] for g in groups))
        n = len(groups)
        subject = (f"Agenda Watch: {n} matching items in {cities[0]}" if len(cities) == 1
                   else f"Agenda Watch: {n} matching items")
    if test:
        subject = "[Test] " + subject
    return subject


def format_hit(i, h):
    """One meeting, agenda item first. Never presents packet text as the agenda item."""
    title = display_item_title(h)
    when = format_when(h.get("meeting_datetime") or h.get("meeting_date") or "")
    meeting = meeting_name(h["city"], h.get("meeting_body"))
    lines = [f"{i}. {meeting}"]
    if when:
        lines.append(f"   {when}")
    lines += ["", f"   Item: {title}"]
    if h.get("session"):
        lines.append(f"   Session: {h['session']}")
    for a in h.get("also_items") or []:
        lines.append(f"   Also on this agenda: {a}")
    if h.get("minutes_of"):
        note = f"   Note: this text is from the {h['minutes_of']} meeting minutes included in the packet"
        lines.append(note + (f" (on this agenda as {h['on_agenda_as']})." if h.get("on_agenda_as") else "."))
    tnorm = _norm((h.get("agenda_item") or "").split(" ", 2)[-1]) if h.get("agenda_item") else None
    lines.append("")
    return lines + _matched_excerpts_links(h, tnorm, h.get("agenda_item"))


def meeting_name(city, body):
    """'Provo' + 'Provo Municipal Council' -> 'Provo Municipal Council' (no repeated city name)."""
    city, body = (city or "").strip(), (body or "").strip()
    if city and body and re.match(re.escape(city) + r"\b", body, re.I):
        return body
    return " ".join(x for x in (city, body) if x)


MAX_EXCERPTS = 2
# "Staff Recommendation", "Recommendation:", "Staff recommends", "Planning Commission recommends",
# Provo's "ADMINISTRATIVE RECOMMENDATION:"
RECOMMEND_RE = re.compile(
    r"^\W*(?:(?:staff|administrative|planning commission|council staff)\s+recommendation\b"
    r"|recommendation\s*:|(?:city\s+)?staff recommends\b|planning commission recommends\b)", re.I)
# code/ordinance body text: "(1) ...", "(f) ...", "6.33.030 ...", "Chapter 6.33 ...", "Section 6.33.020"
ORDINANCE_BODY_RE = re.compile(r"^\W*(?:\([0-9a-z]{1,4}\)\s|\d+\.\d+\.\d+\b|chapter\s+\d+\.\d+|section\s+\d)", re.I)


def _agenda_line_excerpt(ex, e, title):
    """The matched agenda line itself (from the PMN agenda text), trimmed to start at the item number."""
    if not title or not (e.get("excerpt_source") or "").startswith("the PMN notice"):
        return None
    num, _, rest = title.split(" — ")[0].partition(" ")
    core = _norm(rest)[:60]
    if not core or core[:30] not in _norm(ex):
        return None
    m = re.search(r"(?<![\w.])" + re.escape(num) + r"\s", ex)
    out = ex[m.start():] if m else ex
    return re.sub(r"\s*\bPresenter:.*$", "", out).strip() or None


def _pick_excerpts(cands, title, limit=MAX_EXCERPTS):
    """At most `limit` excerpts: (1) the agenda line, (2) a staff recommendation; if neither, the best
    matched sentences; ordinance body text only when nothing else exists."""
    agenda = next((a for a in (_agenda_line_excerpt(ex, e, title) for ex, e in cands) if a), None)
    rec = next(((ex, e) for ex, e in cands if RECOMMEND_RE.search(ex)), None)
    out = []
    if agenda:
        src = next(e.get("excerpt_source") for ex, e in cands if _agenda_line_excerpt(ex, e, title))
        out.append((agenda, src))
    if rec:
        out.append((rec[0], rec[1].get("excerpt_source")))
    if not out:
        return [(ex, e.get("excerpt_source")) for ex, e in cands[:limit]]
    for ex, e in cands:                                 # fill a free slot, but not with ordinance body text
        if len(out) >= limit:
            break
        if any(_norm(ex) == _norm(x) for x, _ in out) or (agenda and _agenda_line_excerpt(ex, e, title)):
            continue
        if ORDINANCE_BODY_RE.search(ex) or ORDINANCE_BODY_RE.search(e.get("item_text") or ""):
            continue
        out.append((ex, e.get("excerpt_source")))
    return out[:limit]


CITY_PAGE = "city_page"


def _posted_on(groups):
    """'Utah Public Notice' (PMN only: unchanged wording), else names the city agendas page(s)."""
    city = [g.get("source_kind") == CITY_PAGE for g in groups]
    if not any(city):
        return "Utah Public Notice"
    if all(city):
        return "the city's own agendas page" if len({g.get("city_key") for g in groups}) == 1 \
            else "the cities' own agendas pages"
    return "Utah Public Notice and city agendas pages"


def _matched_excerpts_links(h, tnorm, title=None):
    """Matched / Excerpt / Source / PMN notice lines shared by both email shapes.
    Every hit's link is still listed; the excerpt block shows at most MAX_EXCERPTS sentences."""
    lines = []
    shown, terms = _shown_excerpts(h)
    lines.append(f"   Matched: {', '.join(terms)}")
    links, cands = [], []
    for e in shown:
        ex = e.get("excerpt_sentence") or _clean(e.get("excerpt") or "")
        if e.get("link") and e["link"] not in links:
            links.append(e["link"])
        if not ex:
            continue
        if title and _agenda_line_excerpt(ex, e, title):
            cands.append((ex, e))                      # the matched agenda line (priority 1)
            continue
        if tnorm and tnorm in _norm(ex) and len(_norm(ex)) <= len(tnorm) + 40:
            continue                                   # a heading repeating the title is not an excerpt
        if all(_norm(ex) != _norm(x) for x, _ in cands):
            cands.append((ex, e))
    sents = _pick_excerpts(cands, title)
    if len(sents) == 1:
        ex, src = sents[0]
        lines += format_excerpt_lines(ex, src)
    elif sents:
        docs = list(dict.fromkeys(_src_parts(src)[0] for _, src in sents))
        if len(docs) == 1 and docs[0]:
            pages = sorted({p for _, src in sents for p in [_src_parts(src)[1]] if p})
            pg = "" if not pages else (f", page {pages[0]}" if len(pages) == 1 else
                                       f", pages {pages[0]}–{pages[-1]}" if pages == list(range(pages[0], pages[-1] + 1))
                                       else ", pages " + ", ".join(map(str, pages)))
            src_label = f"{docs[0]}{pg}"
            # Several excerpts from one document: one header, then each excerpt (bullets if present).
            lines.append(f"   Excerpt (from {src_label}):")
            for ex, _ in sents:
                lead, bullets = split_excerpt(ex)
                if bullets:
                    if lead:
                        lines.append(f"     {lead}")
                    lines += [f"     • {b}" for b in bullets]
                else:
                    lines.append(f'     "{lead}"')
        else:
            lines.append("   Excerpt:")
            for ex, src in sents:
                lead, bullets = split_excerpt(ex)
                if bullets:
                    if lead:
                        lines.append(f"     {lead}" + (f" ({src})" if src else ""))
                    else:
                        if src:
                            lines.append(f"     ({src})")
                    lines += [f"     • {b}" for b in bullets]
                else:
                    lines.append(f'     "{lead}"' + (f" ({src})" if src else ""))
    if not links:
        links = [e["link"] for e in h.get("excerpts") or [h] if e.get("link")]
    notice = (h.get("notice_url") or "").strip()
    label = "City agendas page" if h.get("source_kind") == CITY_PAGE else "PMN notice"
    lines += [f"   Source: {l}" for l in source_links(links, notice)] + [f"   {label}: {notice}", ""]
    return lines


def url_key(u):
    """Comparison key for a link: scheme (http/https alike), host case, trailing slash, #fragment and
    stray whitespace are ignored; the path's case and the query string stay significant."""
    from urllib.parse import urlsplit
    u = re.sub(r"\s+", "", u or "")
    if not u:
        return ""
    p = urlsplit(u if "//" in u else "//" + u)
    host = (p.hostname or "").lower()
    if p.port and p.port not in (80, 443):
        host += f":{p.port}"
    return f"{host}{p.path.rstrip('/') or ''}" + (f"?{p.query}" if p.query else "")


def source_links(links, notice_url=""):
    """Source lines for one email item: each URL once (first spelling kept, order stable), and never
    the PMN notice URL, which has its own line."""
    seen = {url_key(notice_url)} if notice_url else set()
    out = []
    for l in links or []:
        l = (l or "").strip()
        k = url_key(l)
        if k and k not in seen:
            seen.add(k)
            out.append(l)
    return out


_PLURAL_END = ("rental", "license", "permit", "unit", "variance", "ordinance", "fee", "home", "lot", "sign")


def _topic(terms):
    """'short-term rental' -> 'short-term rentals'; several terms joined with 'and'."""
    ts = [t + "s" if t.lower().endswith(_PLURAL_END) else t for t in terms if t]
    return ts[0] if len(ts) == 1 else (", ".join(ts[:-1]) + " and " + ts[-1] if ts else "your watch topics")


def format_minutes(i, g):
    """One past meeting's minutes. Says the discussion already happened; never 'Agenda item'/'upcoming'."""
    date = g.get("minutes_of") or "an earlier"
    lines = [f"{i}. Already discussed at the {date} meeting",
             f"   Meeting: {meeting_name(g['city'], g.get('meeting_body'))} — {date} (minutes)"]
    topic = g.get("agenda_item")
    if topic and not re.search(r"\bminutes\b", topic, re.I):
        lines.append(f"   Topic in the minutes: {topic}")
    tnorm = _norm(topic.split(" ", 2)[-1]) if topic else None
    body = lines + _matched_excerpts_links(g, tnorm, topic)
    posted = g.get("meeting_datetime") or g.get("meeting_date") or ""
    note = "   These minutes were posted in the " + " ".join(x for x in (posted, g.get("meeting_body")) if x) + " packet"
    note += f" for approval ({g['on_agenda_as']})." if g.get("on_agenda_as") else "."
    return body[:-1] + [note, ""]


def build_minutes_email(items, base_url, test=False):
    """Past-minutes email: one item per past meeting; subject and heading say it was already discussed."""
    groups = group_by_meeting(items)
    if len(groups) == 1:
        g = groups[0]
        _, terms = _shown_excerpts(g)
        subject = (f"Agenda Watch: {meeting_name(g['city'], g.get('meeting_body'))} discussed {_topic(terms)} "
                   f"({g.get('minutes_of')} minutes)").replace("  ", " ")
    else:
        subject = f"Agenda Watch: {len(groups)} past meetings discussed your watch topics (minutes)"
    parts = []
    if test:
        subject = "[Test] " + subject
        parts += ["This is a one-time test of per-user alerts, sent by the Agenda Watch owner.", ""]
    noun = "meeting" if len(groups) == 1 else "meetings"
    parts += [f"Agenda Watch found your watch topics in the minutes of {len(groups)} past {noun}, posted on "
              f"{_posted_on(groups)}. This discussion already happened.", ""]
    for i, g in enumerate(groups, 1):
        parts += format_minutes(i, g)
    parts += ["--", FOOTER.format(base=base_url.rstrip("/"))]
    return subject, "\n".join(parts)


def build_user_emails(items, base_url, test=False):
    """Mixed runs send SEPARATE emails: one for upcoming agenda items, one for past minutes, so each
    subject is accurate. Returns [(kind, subject, body, groups)]."""
    up = [h for h in items if not is_past(h) and not is_link_only(h)]
    past = [h for h in items if is_past(h)]
    lo = [h for h in items if is_link_only(h)]
    out = []
    if up:
        out.append(("upcoming", *build_user_email(up, base_url, test=test), group_by_meeting(up)))
    if past:
        out.append(("past_minutes", *build_minutes_email(past, base_url, test=test), group_by_meeting(past)))
    if lo:
        out.append((LINK_ONLY, *build_link_only_email(lo, base_url, test=test), group_link_only(lo)))
    return out


def build_user_email(items, base_url, test=False):
    subject = upcoming_subject(items, test=test)
    parts = []
    if test:
        parts += ["This is a one-time test of per-user alerts, sent by the Agenda Watch owner. "
                  "It uses a real upcoming agenda item that matches your watch.", ""]
    groups = group_by_meeting(items)
    noun = "item" if len(groups) == 1 else "items"
    parts += [f"Agenda Watch found {len(groups)} matching upcoming agenda {noun} in public agendas posted on "
              f"{_posted_on(groups)}.", ""]
    for i, h in enumerate(groups, 1):
        parts += format_hit(i, h)
    footer = FOOTER.format(base=base_url.rstrip("/"))
    if test:
        footer = footer.split("\n")[0] + (
            "\nThis test was sent once by the owner; it does not change your alert settings. "
            "Manage your watch: " + base_url.rstrip("/") + "/watch")
    parts += ["--", footer]
    return subject, "\n".join(parts)

def render_alert_html(kind, groups, base_url, test=False, intro=None):
    """Email-safe HTML for a per-user alert (inline styles, table layout). Same content as the text body."""
    base = (base_url or "").rstrip("/")
    watch = f"{base}/watch"
    footer1 = "Agenda Watch flags public agenda text and links to the source. It is not legal advice."
    footer2 = ("You get this because alerts are on for these cities on your watch. "
               "Change your watch or turn alerts off:")
    esc = html_mod.escape

    def link(url, label=None):
        url = (url or "").strip()
        if not url:
            return ""
        shown = esc(label or url)
        return f'<a href="{esc(url, quote=True)}" style="color:#1e3a5f;word-break:break-all;">{shown}</a>'

    def excerpt_html(ex, src=None):
        lead, bullets = split_excerpt(ex)
        bits = []
        if src:
            bits.append(f'<div style="font-size:12px;color:#6b7280;margin:0 0 6px 0;">From {esc(src)}</div>')
        if lead and not bullets:
            bits.append(f'<p style="margin:0;color:#1f2937;">{esc(lead)}</p>')
        else:
            if lead:
                bits.append(f'<p style="margin:0 0 8px 0;color:#1f2937;">{esc(lead)}</p>')
            if bullets:
                items = "".join(f'<li style="margin:0 0 6px 0;">{esc(b)}</li>' for b in bullets)
                bits.append('<ul style="margin:0;padding:0 0 0 20px;color:#1f2937;">' + items + "</ul>")
        return "".join(bits)

    def kv(label, value, raw=False):
        shown = value if raw else esc(value or "")
        return (
            f'<div style="margin:0 0 12px 0;"><div style="font-size:11px;letter-spacing:.06em;'
            f'text-transform:uppercase;color:#6b7280;margin:0 0 4px 0;">{esc(label)}</div>'
            f'<div style="color:#111827;font-size:15px;line-height:1.45;">{shown}</div></div>'
        )

    def hit_card(i, h, minutes=False):
        meeting = meeting_name(h.get("city"), h.get("meeting_body"))
        when = format_when(h.get("meeting_datetime") or h.get("meeting_date") or "")
        title = display_item_title(h)
        shown, terms = _shown_excerpts(h)
        tnorm = _norm((h.get("agenda_item") or "").split(" ", 2)[-1]) if h.get("agenda_item") else None
        cands, links = [], []
        for e in shown:
            ex = e.get("excerpt_sentence") or _clean(e.get("excerpt") or "")
            if e.get("link") and e["link"] not in links:
                links.append(e["link"])
            if not ex:
                continue
            if h.get("agenda_item") and _agenda_line_excerpt(ex, e, h.get("agenda_item")):
                cands.append((ex, e))
                continue
            if tnorm and tnorm in _norm(ex) and len(_norm(ex)) <= len(tnorm) + 40:
                continue
            if all(_norm(ex) != _norm(x) for x, _ in cands):
                cands.append((ex, e))
        sents = _pick_excerpts(cands, h.get("agenda_item"))
        if not links:
            links = [e["link"] for e in h.get("excerpts") or [h] if e.get("link")]
        notice = (h.get("notice_url") or "").strip()
        notice_label = "City agendas page" if h.get("source_kind") == CITY_PAGE else "Public notice"
        srcs = source_links(links, notice)

        rows = []
        head = esc(meeting)
        if when:
            head += (
                f'<div style="font-size:14px;font-weight:normal;color:#4b5563;margin-top:4px;">'
                f'{esc(when)}</div>'
            )
        rows.append(
            f'<div style="font-size:18px;font-weight:bold;color:#111827;margin:0 0 16px 0;">{i}. {head}</div>'
        )
        if minutes:
            date = h.get("minutes_of") or "an earlier"
            rows.append(
                f'<p style="margin:0 0 12px 0;color:#1f2937;">Already discussed at the {esc(date)} meeting.</p>'
            )
            topic = h.get("agenda_item")
            if topic and not re.search(r"\bminutes\b", topic, re.I):
                rows.append(kv("Topic in the minutes", topic))
        else:
            rows.append(kv("Item", title))
        if h.get("session"):
            rows.append(kv("Session", h["session"]))
        for a in h.get("also_items") or []:
            rows.append(kv("Also on this agenda", a))
        if h.get("minutes_of") and not minutes:
            note = f"This text is from the {h['minutes_of']} meeting minutes included in the packet"
            note += f" (on this agenda as {h['on_agenda_as']})." if h.get("on_agenda_as") else "."
            rows.append(f'<p style="margin:0 0 12px 0;color:#4b5563;font-size:13px;">{esc(note)}</p>')
        if terms:
            rows.append(kv("Matched", ", ".join(terms)))
        if sents:
            if len(sents) == 1:
                ex, src = sents[0]
                body = excerpt_html(ex, src)
            else:
                docs = list(dict.fromkeys(_src_parts(src)[0] for _, src in sents))
                if len(docs) == 1 and docs[0]:
                    pages = sorted({p for _, src in sents for p in [_src_parts(src)[1]] if p})
                    pg = "" if not pages else (
                        f", page {pages[0]}" if len(pages) == 1 else
                        (f", pages {pages[0]}–{pages[-1]}" if pages == list(range(pages[0], pages[-1] + 1))
                         else ", pages " + ", ".join(map(str, pages)))
                    )
                    body = "".join(
                        excerpt_html(ex, f"{docs[0]}{pg}" if idx == 0 else None)
                        for idx, (ex, _) in enumerate(sents)
                    )
                else:
                    body = "".join(excerpt_html(ex, src) for ex, src in sents)
            rows.append(
                '<div style="margin:0 0 16px 0;"><div style="font-size:11px;letter-spacing:.06em;text-transform:uppercase;'
                'color:#6b7280;margin:0 0 6px 0;">Excerpt</div>'
                f'<div style="background:#f8fafc;border:1px solid #e5e7eb;border-radius:6px;padding:12px 14px;">{body}</div></div>'
            )
        if minutes:
            posted = h.get("meeting_datetime") or h.get("meeting_date") or ""
            note = "These minutes were posted in the " + " ".join(
                x for x in (posted, h.get("meeting_body")) if x
            ) + " packet"
            note += f" for approval ({h['on_agenda_as']})." if h.get("on_agenda_as") else "."
            rows.append(f'<p style="margin:0 0 12px 0;color:#4b5563;font-size:13px;">{esc(note)}</p>')
        if srcs:
            rows.append(kv("Source", "<br>".join(link(u) for u in srcs), raw=True))
        if notice:
            rows.append(kv(notice_label, link(notice), raw=True))
        inner = "".join(rows)
        return (
            '<tr><td style="padding:0 0 20px 0;">'
            '<table role="presentation" width="100%" cellspacing="0" cellpadding="0" '
            'style="border:1px solid #e5e7eb;border-radius:8px;">'
            f'<tr><td style="padding:20px 22px;">{inner}</td></tr></table></td></tr>'
        )

    if intro is None:
        if kind == "upcoming":
            noun = "item" if len(groups) == 1 else "items"
            intro = (
                f"Agenda Watch found {len(groups)} matching upcoming agenda {noun} in public agendas posted on "
                f"{_posted_on(groups)}."
            )
        elif kind == "past_minutes":
            noun = "meeting" if len(groups) == 1 else "meetings"
            intro = (
                f"Agenda Watch found your watch topics in the minutes of {len(groups)} past {noun}, posted on "
                f"{_posted_on(groups)}."
            )
        else:
            intro = ""
    if kind == "upcoming":
        kicker = "A matching item on an upcoming public agenda"
    elif kind == "past_minutes":
        kicker = "A past meeting that discussed your watch topics"
    else:
        kicker = "A notice from Agenda Watch"
    test_banner = ""
    if test and kind == "upcoming":
        test_banner = (
            '<p style="margin:0 0 16px 0;padding:10px 12px;background:#fff7ed;border:1px solid #fdba74;'
            'color:#9a3412;font-size:13px;">This is a one-time test of per-user alerts, sent by the '
            "Agenda Watch owner. It uses a real upcoming agenda item that matches your watch.</p>"
        )
    cards = "".join(hit_card(i, g, minutes=(kind == "past_minutes")) for i, g in enumerate(groups or [], 1))
    return (
        '<!DOCTYPE html>\n'
        '<html lang="en">\n'
        '<head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">\n'
        '<title>Agenda Watch</title></head>\n'
        '<body style="margin:0;padding:0;background:#eef1f4;">\n'
        '<table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="background:#eef1f4;">\n'
        '<tr><td align="center" style="padding:24px 12px;">\n'
        '<table role="presentation" width="600" cellspacing="0" cellpadding="0" '
        'style="background:#ffffff;border:1px solid #d9dee5;max-width:600px;width:100%;">\n'
        '<tr><td style="background:#1e3a5f;color:#ffffff;padding:22px 28px;font-family:Arial,Helvetica,sans-serif;">\n'
        '  <div style="font-size:20px;font-weight:bold;letter-spacing:.02em;">Agenda Watch</div>\n'
        f'  <div style="font-size:13px;opacity:.9;margin-top:4px;">{esc(kicker)}</div>\n'
        '</td></tr>\n'
        '<tr><td style="padding:24px 28px 8px 28px;font-family:Arial,Helvetica,sans-serif;'
        'font-size:15px;line-height:1.55;color:#1f2937;">\n'
        f'  {test_banner}\n'
        f'  <p style="margin:0 0 20px 0;">{esc(intro)}</p>\n'
        '</td></tr>\n'
        '<tr><td style="padding:0 28px;font-family:Arial,Helvetica,sans-serif;">\n'
        f'  <table role="presentation" width="100%" cellspacing="0" cellpadding="0">{cards}</table>\n'
        '</td></tr>\n'
        '<tr><td style="padding:8px 28px 28px 28px;font-family:Arial,Helvetica,sans-serif;font-size:12px;'
        'line-height:1.55;color:#6b7280;border-top:1px solid #e5e7eb;">\n'
        f'  <p style="margin:16px 0 8px 0;">{esc(footer1)}</p>\n'
        f'  <p style="margin:0;">{esc(footer2)} {link(watch, "Manage your watch")}</p>\n'
        '</td></tr>\n'
        '</table>\n'
        '</td></tr>\n'
        '</table>\n'
        '</body>\n'
        '</html>\n'
    )


def render_user_email_html(items, base_url, test=False):
    """HTML companion for build_user_email (upcoming hits)."""
    groups = group_by_meeting(items)
    noun = "item" if len(groups) == 1 else "items"
    intro = (
        f"Agenda Watch found {len(groups)} matching upcoming agenda {noun} in public agendas posted on "
        f"{_posted_on(groups)}."
    )
    return render_alert_html("upcoming", groups, base_url, test=test, intro=intro)
