"""City agendas pages as alert sources (owner order Oct 31 2026). Sandbox only: no network, no email.

Fixtures:
  tests/fixtures/ch_pc_agendas_page.html          SYNTHETIC page built from the live CH table rows
  tests/fixtures/ch_pc_20261104_packet_excerpt.txt excerpt of the live Nov 4 2026 CH Planning Commission
                                                   packet (agenda + CUP-26-019 report + ZTA-26-006 STR memo
                                                   + 19.89 draft + start of the Oct 21 minutes), \f = page break
"""
import datetime as dt
import sqlite3
from pathlib import Path

import pytest

import agenda_watch as aw
import aw_alerts
import aw_city_sources as acs
from conftest import FROZEN_NOW, OWNER, FakeResponse
from test_user_alerts import A, B, KW, SOON, STR_AGENDA, add_watch, db, plan, rows, seeded  # noqa: F401
from test_web_watch import webapp  # noqa: F401

FIX = Path(__file__).parent / "fixtures"
PAGE_HTML = (FIX / "ch_pc_agendas_page.html").read_text()
PACKET = (FIX / "ch_pc_20261104_packet_excerpt.txt").read_text()
CH_URL = acs.CH_PC_URL
AGENDA_URL = "https://www.cottonwoodheights.utah.gov/home/showpublisheddocument/12572"
PACKET_URL = "https://www.cottonwoodheights.utah.gov/home/showpublisheddocument/12574"
AGENDA_TEXT = PACKET.split("\f")[0]                     # the agenda pages of the packet
OCT31 = dt.datetime(2026, 10, 31, 12, 0, tzinfo=aw.TZ)
CHRIS = {"email": OWNER, "alerts_on": 1, "keywords": aw_alerts.parse_keywords(KW),
         "cities": {"ch": {"enabled": 1, "street": "100 Example St"}, "sandy": {"enabled": 1, "street": ""}}}


@pytest.fixture(autouse=True)
def _source_scanned_before(monkeypatch):
    """These Oct 31 scenarios model a packet that newly appears on a source that was already scanned
    (the Nov 4 cap baselines a source's first scan). Simulate one earlier scan, source added the day before."""
    real = acs.record_seen

    def wrapped(con, src, rows, now, today, persist=True):
        src = dict(src, added_at=(now - dt.timedelta(days=1)).isoformat())
        try:
            con.execute("INSERT OR IGNORE INTO city_source_docs VALUES(?,?,?,?,?,?)",
                        (src["city_key"], src["url"], "test:earlier-scan", None, src["added_at"], 1))
        except sqlite3.Error:
            pass
        return real(con, src, rows, now, today, persist)
    monkeypatch.setattr(acs, "record_seen", wrapped)


# ------------------------------------------------------------------ validation
@pytest.mark.parametrize("ck,url,title", [
    ("ch", CH_URL, None),
    ("ch", CH_URL + "/", None),
    ("ch", "https://www.ch.utah.gov/your-government/city-council/agendas-packets-minutes", None),
    ("ch", "http://cottonwoodheights.utah.gov/AgendaCenter", None),
    ("orem", "https://orem.gov/AgendaCenter", None),
    ("daniel", "https://danielutah.gov/planning-agendas/", None),
    ("provo", "https://agendas.provo.gov/", "Provo Agendas"),
    ("slc", "https://www.slc.gov/council/agendas/", None),
    ("sandy", "https://sandy.utah.gov/1206/Meeting-Minutes", None),
    ("farmington", "https://farmington.utah.gov/planning-commission-packets/", None),
])
def test_validate_accepts_city_domain_agenda_pages(ck, url, title):
    ok, why = acs.validate(ck, url, title)
    assert ok, why


@pytest.mark.parametrize("ck,url,title,why", [
    ("sandy", "https://sandyutah.legistar.com/Calendar.aspx", "Agendas", "domain"),        # third party
    ("wj", "https://westjordan.primegov.com/public/portal", "Agendas and Minutes", "domain"),
    ("ch", "https://www.utah.gov/pmn/sitemap/notice/1117717.html", "agenda", "domain"),     # PMN itself
    ("ch", "https://cottonwoodheights.utah.gov.evil.example/agendas", None, "domain"),
    ("ch", "https://notcottonwoodheights.utah.gov/agendas", None, "domain"),
    ("ch", "https://evil.example/cottonwoodheights.utah.gov/agendas", None, "domain"),
    ("ch", "https://cottonwoodheights.utah.gov@evil.example/agendas", None, "domain"),
    ("ch", "https://www.cottonwoodheights.utah.gov:8443/agendas", None, "domain"),
    ("ch", "ftp://www.cottonwoodheights.utah.gov/agendas", None, "domain"),
    ("ch", "https://www.slc.gov/council/agendas/", None, "domain"),                         # another city's
    ("midvale", "https://midvaleut.gov/agendas", None, "domain"),                           # unverified
    ("daniel", "https://danielutah.org/agendas", None, "domain"),                           # unverified
    ("xx", "https://www.cottonwoodheights.utah.gov/agendas", None, "catalog"),
    ("ch", "https://www.cottonwoodheights.utah.gov/", None, "not an agendas"),              # homepage
    ("ch", "https://www.ch.utah.gov/planningcommission", None, "not an agendas"),
    ("ch", "https://www.cottonwoodheights.utah.gov/your-government/news", "City News", "not an agendas"),
    ("ch", PACKET_URL, "Packet", "single document"),
    ("ch", "https://www.cottonwoodheights.utah.gov/agendas/nov4.pdf", None, "single document"),
    ("orem", "https://orem.gov/DocumentCenter/View/123/Agenda", None, "single document"),
])
def test_validate_rejects_everything_else(ck, url, title, why):
    ok, reason = acs.validate(ck, url, title)
    assert not ok and why in reason, (url, reason)


def test_domain_map_covers_all_seventeen_catalog_cities(cfg):
    import aw_cities
    assert set(acs.CITY_DOMAINS) == set(aw_cities.key_to_name(cfg))


def _con(tmp_path, name="cs.db"):
    con = sqlite3.connect(tmp_path / name)
    aw.aw_store.ensure_schema(con)
    acs.ensure(con)
    return con


def test_seed_is_only_the_ch_planning_commission_page(tmp_path):
    con = _con(tmp_path)
    acs.ensure(con)                                        # idempotent
    assert [(r["city_key"], r["url"]) for r in acs.sources(con)] == [("ch", CH_URL)]


def test_invalid_rows_in_the_table_are_never_used(tmp_path):
    con = _con(tmp_path)
    con.execute("INSERT INTO city_sources(city_key,url,added_at,added_by) VALUES('ch','https://sandyutah.legistar.com/x','t','x')")
    assert [r["url"] for r in acs.sources(con)] == [CH_URL]


# ------------------------------------------------------------------ auto-add from PMN hits
def test_auto_add_takes_only_valid_links_from_hit_notices(tmp_path):
    con = _con(tmp_path)
    good = "https://www.cottonwoodheights.utah.gov/your-government/city-council/agendas-packets-minutes"
    text = (f"Agenda posted at {good}. See also www.ch.utah.gov/planningcommission, "
            f"https://www.slc.gov/council/agendas/ and {PACKET_URL} and https://www.youtube.com/@CottonwoodHeights.")
    res = {"hits": [{"notice_url": "https://www.utah.gov/pmn/sitemap/notice/1.html"}],
           "_docs": [{"city_key": "ch", "notice_url": "https://www.utah.gov/pmn/sitemap/notice/1.html",
                      "docs": [("PMN notice text", "u", text)]},
                     {"city_key": "ch", "notice_url": "https://www.utah.gov/pmn/sitemap/notice/2.html",   # no hit
                      "docs": [("PMN notice text", "u2", "https://www.cottonwoodheights.utah.gov/minutes-archive")]}]}
    added = acs.auto_add(con, res)
    assert added == [{"city_key": "ch", "url": good, "from": "https://www.utah.gov/pmn/sitemap/notice/1.html"}]
    assert acs.auto_add(con, res) == []                    # already a source
    by = dict(con.execute("SELECT url, added_by FROM city_sources"))
    assert by[good] == "auto: https://www.utah.gov/pmn/sitemap/notice/1.html"
    assert set(by) == {CH_URL, good}


# ------------------------------------------------------------------ page parsing + scan
def test_parse_page_rows():
    got = [(r["date"].isoformat(), r["body"], r["cancelled"], [l for l, _ in r["docs"]])
           for r in acs.parse_page(PAGE_HTML, CH_URL, "ch")]
    assert got[0] == ("2026-11-04", "Planning Commission", False, ["Agenda", "Packet"])
    assert ("2026-10-07", "Administrative Hearing", True, ["Notice of Cancellation"]) in got
    assert ("2026-03-04", "Planning Commission", False, ["Agenda", "Packet"]) in got        # "March 4,2026"
    off = [r for r in acs.parse_page(PAGE_HTML, CH_URL, "ch") if r["skipped"]]
    assert off and off[0]["skipped"][0]["why"] == "not on the city's own domain" and not off[0]["docs"]


def _fake_doc(texts):
    def f(url, page_url, prev):
        if url not in texts:
            raise acs.CityFetchError(f"GET {url} failed: HTTP 404")
        t = texts[url]
        return t, {"url": url, "notice_url": page_url, "etag": None, "last_modified": None, "length": len(t),
                   "text_hash": "h", "text": t}, "fetched"
    return f


def _scan(tmp_path, now=OCT31, texts=None, page=PAGE_HTML):
    con = _con(tmp_path, "scan.db")
    return acs.scan({"cities": _cities()}, con, now, {}, fetch_page_fn=lambda u: page,
                    fetch_doc_fn=_fake_doc(texts if texts is not None else {AGENDA_URL: AGENDA_TEXT, PACKET_URL: PACKET}))


def _cities():
    from conftest import real_config
    return real_config()["cities"]


def test_scan_keeps_upcoming_uncancelled_rows_only(tmp_path):
    entries, stats, failures = _scan(tmp_path)
    assert failures == [] and stats["pages_ok"] == 1 and stats["upcoming_rows"] == 1
    (e,) = entries
    assert e["city_key"] == "ch" and e["notice_url"] == CH_URL and e["source_kind"] == "city_page"
    assert e["meeting_datetime"] == "November 4, 2026" and e["event_iso"].startswith("2026-11-04T23:59")
    assert [d[0] for d in e["docs"]] == ["Planning Commission Agenda November 4, 2026",
                                         "Planning Commission Packet November 4, 2026"]


def test_failed_page_fetch_is_reported_not_raised(tmp_path):
    con = _con(tmp_path)

    def boom(u):
        raise acs.CityFetchError(f"GET {u} failed: HTTP 403")
    entries, stats, failures = acs.scan({"cities": _cities()}, con, OCT31, {}, fetch_page_fn=boom)
    assert entries == [] and stats["pages_ok"] == 0
    assert failures == [f"city source blocked: {CH_URL} HTTP 403"]
    assert stats["unreadable"] == []                       # blocked page: no documents are guessed
    assert con.execute("SELECT last_error FROM city_sources").fetchone()[0].endswith("HTTP 403")


# ------------------------------------------------------------------ the Nov 4 packet alert
def _entry(texts=None, now=OCT31, tmp=None):
    entries, _s, _f = _scan(tmp, now, texts)
    return entries


def test_nov4_packet_gives_one_upcoming_alert_led_by_3_3(tmp_path):
    notices = _entry(tmp=tmp_path)
    hits = aw_alerts.match_user(notices, CHRIS, OCT31)
    assert hits and all(h["kind"] == "upcoming" for h in hits)
    assert not any(aw_alerts.is_past(h) for h in hits)           # Oct 21 minutes: weak terms only -> no hit
    assert not any("CUP-26-020" in (h["item_text"] + (h.get("agenda_item") or "")) for h in hits)
    mails = aw_alerts.build_user_emails([aw_alerts._brief(h) for h in hits], "https://agendawatch.app")
    assert [m[0] for m in mails] == ["upcoming"]
    kind, subject, body, groups = mails[0]
    assert subject == "Agenda Watch: Cottonwood Heights Planning Commission — November 4, 2026"
    assert len(groups) == 1
    assert body.splitlines()[0] == ("Agenda Watch found 1 matching upcoming agenda item in public agendas posted "
                                    "on the city's own agendas page.")
    assert "\n   Item: 3.3 Project ZTA-26-006 — The Planning Commission will hold a public hearing" in body
    assert "Short-Term Rentals." in body
    assert "Also on this agenda" not in body                    # the STR memo is not CUP-26-019's
    assert f"   Source: {PACKET_URL}" in body and f"   City agendas page: {CH_URL}" in body
    assert "PMN notice:" not in body


def test_hearing_notice_already_alerted_does_not_hide_the_packet(tmp_path):
    """Live state Oct 30: the Oct 22 PMN hearing notice for Nov 4 was alerted Oct 23. The packet's items are new."""
    con = _con(tmp_path)
    aw_alerts.ensure_schema(con)
    old = {"city_key": "ch", "meeting_date": "2026-11-04", "notice_url": "https://www.utah.gov/pmn/sitemap/notice/1117717.html",
           "link": "https://www.utah.gov/pmn/files/1505643.pdf",
           "item_text": "What? Notice is hereby given that the Cottonwood Heights Planning Commission will hold a public "
                        "hearing and make a possible recommendation to the City Council on city-initiated requests for a "
                        "Zoning Text Amendment to modify CHMC Chapter 19.80 Off Street Parking Requirements (File: "
                        "ZTA-26-005) and for a Zoning Text Amendment to modify CHMC Chapter 19.89 Short-Term Rentals "
                        "(File: ZTA-26-006)."}
    old["fingerprint"] = aw_alerts.fingerprint("ch", "2026-11-04", old["item_text"])
    aw_alerts.record(con, OWNER, old, "sent")
    hits = aw_alerts.match_user(_entry(tmp=tmp_path), CHRIS, OCT31)
    assert [h for h in hits if aw_alerts.pending(con, OWNER, h)]


def test_dedupe_city_then_pmn_and_new_document(tmp_path):
    con = _con(tmp_path)
    aw_alerts.ensure_schema(con)
    notices = _entry(tmp=tmp_path)
    for h in aw_alerts.match_user(notices, CHRIS, OCT31):
        aw_alerts.record(con, OWNER, h, "sent")
    # the same packet arriving later via PMN (same text, PMN URLs): nothing new to send
    pmn = [{"city": "Cottonwood Heights", "city_key": "ch", "notice_url": "https://www.utah.gov/pmn/sitemap/notice/9.html",
            "meeting_body": "Planning Commission", "notice_title": "Planning Commission Meeting",
            "meeting_datetime": "November 4, 2026 06:00 PM", "event_iso": "2026-11-04T18:00:00-07:00",
            "docs": [("PMN notice text", "https://www.utah.gov/pmn/sitemap/notice/9.html", AGENDA_TEXT),
                     ("Packet", "https://www.utah.gov/pmn/files/9.pdf", PACKET)]}]
    assert [h for h in aw_alerts.match_user(pmn, CHRIS, OCT31) if aw_alerts.pending(con, OWNER, h)] == []
    # the same city page again: nothing new
    assert [h for h in aw_alerts.match_user(notices, CHRIS, OCT31) if aw_alerts.pending(con, OWNER, h)] == []
    # a new document for the already-alerted meeting with a new matching item: a new alert
    supp = ("COTTONWOOD HEIGHTS CITY PLANNING COMMISSION SUPPLEMENTAL STAFF MEMO\nNovember 4, 2026\n"
            "Staff received public comment asking that the short-term rental permit cap be lowered "
            "from 125 to 60 permits per 10,000 residents.\n")
    notices[0]["docs"].append(("Planning Commission ZTA-26-006 Supplemental Staff Memo November 4, 2026",
                               "https://www.cottonwoodheights.utah.gov/home/showpublisheddocument/99999", supp))
    new = [h for h in aw_alerts.match_user(notices, CHRIS, OCT31) if aw_alerts.pending(con, OWNER, h)]
    assert len(new) == 1 and "permit cap be lowered" in new[0]["item_text"]


def test_pmn_rendering_is_unchanged_without_source_kind():
    n = {"city": "Cottonwood Heights", "city_key": "ch", "notice_url": "https://www.utah.gov/pmn/sitemap/notice/9.html",
         "meeting_body": "City Council", "notice_title": "Regular Meeting", "meeting_datetime": "November 4, 2026 06:00 PM",
         "event_iso": "2026-11-04T18:00:00-07:00",
         "docs": [("PMN notice text", "https://www.utah.gov/pmn/sitemap/notice/9.html",
                   "1. Call to Order\n2. Discussion of short-term rental regulations\n3. Adjourn")]}
    hits = aw_alerts.match_user([n], CHRIS, OCT31)
    assert all("source_kind" not in h and "source_kind" not in aw_alerts._brief(h) for h in hits)
    (_k, _s, body, _g), = aw_alerts.build_user_emails([aw_alerts._brief(h) for h in hits], "https://agendawatch.app")
    assert body.startswith("Agenda Watch found 1 matching upcoming agenda item in public agendas posted on Utah Public Notice.")
    assert "   PMN notice: https://www.utah.gov/pmn/sitemap/notice/9.html" in body


# ------------------------------------------------------------------ the daily run, end to end
def _serve_city(monkeypatch, page_status=200, pdf=None):
    """pdf: optional dict {"status": int} - the Nov 4 agenda/packet status (mutable between runs)."""
    calls = []

    def get(url, timeout=None, headers=None, **kw):
        calls.append(url)
        if url == CH_URL:
            return FakeResponse(page_status, PAGE_HTML if page_status == 200 else "<html>Access Denied</html>")
        if url in (AGENDA_URL, PACKET_URL) and pdf and pdf["status"] != 200:
            return FakeResponse(pdf["status"], "<html>Access Denied</html>")
        if url in (AGENDA_URL, PACKET_URL):
            t = AGENDA_TEXT if url == AGENDA_URL else PACKET
            return FakeResponse(200, "", b"%PDF-FAKE\n" + t.encode(), {"ETag": f'"{url[-5:]}"'})
        return FakeResponse(404, "not found")
    monkeypatch.setattr(acs.CITY_S, "get", get)
    monkeypatch.setattr(aw, "extract_pdf_text", lambda path: path.read_bytes().split(b"\n", 1)[1].decode())
    return calls


def test_daily_run_alerts_nov4_packet_once_through_the_normal_path(db, pmn, run_main, sandbox, monkeypatch):
    calls = _serve_city(monkeypatch)
    add_watch(db.db, OWNER, {})
    add_watch(db.db, A, {"ch": (1, "")})
    seeded(db.db)
    code, out, err = run_main("run-owner")
    p = plan(sandbox)
    assert p["mode"] == "send" and [u["email"] for u in p["users"]] == [A]
    assert "city sources: pages=1 ok=1 upcoming meetings=" in out
    res = webapp.deliver_user_alerts(p)
    # the Nov 4 hit + (frozen clock Oct 7) the Oct 21 row's made-up links 404 -> one link-only email
    assert res["emails_sent"] == 2 and len(db.calls) == 2 and {c["to"] for c in db.calls} == {A}
    assert db.calls[1]["subject"] == ("Agenda Watch: new Cottonwood Heights Planning Commission packet posted "
                                      "(could not be read)")
    assert "October 21, 2026" in db.calls[1]["body"] and "Matched" not in db.calls[1]["body"]
    body = db.calls[0]["body"]
    assert db.calls[0]["subject"] == "Agenda Watch: Cottonwood Heights Planning Commission — November 4, 2026"
    assert "Item: 3.3 Project ZTA-26-006" in body and f"City agendas page: {CH_URL}" in body
    assert {r["status"] for r in rows(db.db, A)} == {"sent"} and all(r["notice_url"] == CH_URL for r in rows(db.db, A))
    # Oct 21 row is upcoming too on the frozen clock (Oct 7): its made-up docs 404 -> report only
    with sqlite3.connect(db.db) as c:
        errs = c.execute("SELECT errors FROM fetch_runs ORDER BY id DESC").fetchone()[0]
    assert "city source: Cottonwood Heights Planning Commission October 21, 2026 Agenda" in errs
    # next run: same packet (conditional GET) -> no second email
    run_main("run-owner")
    assert webapp.deliver_user_alerts(plan(sandbox))["emails_sent"] == 0 and len(db.calls) == 2
    # later the same packet is also posted on PMN -> still no second email
    fid = pmn.set_file(7001, PACKET)
    pmn.add_notice("Cottonwood Heights", dt.datetime(2026, 11, 4, 18, 0, tzinfo=aw.TZ), AGENDA_TEXT,
                   title="Planning Commission Meeting", files=(7001,))
    run_main("run-owner")
    assert webapp.deliver_user_alerts(plan(sandbox))["emails_sent"] == 0 and len(db.calls) == 2
    assert fid in pmn.requests and all(u.startswith("https://www.cottonwoodheights.utah.gov") for u in calls)


def test_city_page_failure_never_withholds_pmn_alerts_or_emails_owner(db, pmn, run_main, sandbox, monkeypatch):
    _serve_city(monkeypatch, page_status=403)
    add_watch(db.db, OWNER, {})
    add_watch(db.db, A, {"ch": (1, "")})
    seeded(db.db)
    pmn.add_notice("Cottonwood Heights", SOON, STR_AGENDA)
    code, out, err = run_main("run-owner")
    p = plan(sandbox)
    assert p["mode"] == "send" and code == 0                  # the PMN alert goes out as before; exit code unchanged
    assert f"CITY SOURCE FAILURE (report only): city source blocked: {CH_URL} HTTP 403" in out
    res = webapp.deliver_user_alerts(p)
    assert [c["to"] for c in db.calls] == [A] and "PMN notice:" in db.calls[0]["body"]
    with sqlite3.connect(db.db) as c:
        errs = c.execute("SELECT errors FROM fetch_runs ORDER BY id DESC").fetchone()[0]
    assert f"city source blocked: {CH_URL} HTTP 403" in errs
    assert "city source: city source" not in errs


def test_city_requests_never_trip_the_budget(monkeypatch):
    monkeypatch.setitem(aw.BUDGET, "cap", 5)
    monkeypatch.setitem(aw.BUDGET, "used", 5)
    monkeypatch.setitem(aw.BUDGET, "tripped", False)
    with pytest.raises(acs.CityFetchError, match="budget"):
        acs._take()
    assert aw.BUDGET["tripped"] is False
    monkeypatch.setitem(aw.BUDGET, "cap", None)


def test_probe_only_fetches_city_domains(monkeypatch):
    _serve_city(monkeypatch)
    assert acs.probe("https://evil.example/agendas")["error"].startswith("not on a catalog city")
    p = acs.probe(PACKET_URL)
    assert p["status"] == 200 and p["is_pdf"] is True and p["snippet"] == ""
    assert acs.probe(CH_URL)["status"] == 200


# ------------------------------------------------------------------ blocked-document fallback (owner order Oct 31)
NOV4_LO_SUBJECT = "Agenda Watch: new Cottonwood Heights Planning Commission packet posted (could not be read)"


def _blocked(url, page_url, prev):
    raise acs.CityFetchError(f"GET {url} failed: HTTP 403")


def _scan_blocked(tmp_path, now=OCT31, fetch=_blocked, name="scanb.db"):
    con = _con(tmp_path, name)
    return acs.scan({"cities": _cities()}, con, now, {}, fetch_page_fn=lambda u: PAGE_HTML, fetch_doc_fn=fetch)


def test_page_loads_pdf_403_gives_one_link_only_email_without_excerpts(tmp_path):
    entries, stats, failures = _scan_blocked(tmp_path)
    assert entries == [] and stats["docs_unreadable"] == 2
    assert [(u["meeting_date"], u["doc_label"], u["link"], u["read_error"]) for u in stats["unreadable"]] == [
        ("2026-11-04", "Agenda", AGENDA_URL, "HTTP 403"), ("2026-11-04", "Packet", PACKET_URL, "HTTP 403")]
    assert any("Packet" in f and "HTTP 403" in f for f in failures)          # still in the run log
    con = _con(tmp_path)
    aw_alerts.ensure_schema(con)
    hits = aw_alerts.link_only_hits(stats["unreadable"], CHRIS, OCT31)
    assert {h["fingerprint"] for h in hits} == {aw_alerts.link_only_fingerprint("ch", AGENDA_URL),
                                                aw_alerts.link_only_fingerprint("ch", PACKET_URL)}
    assert all(h["fingerprint"].startswith("linkonly|ch|") for h in hits)
    mails = aw_alerts.build_user_emails([aw_alerts._brief_link_only(h) for h in hits], "https://agendawatch.app")
    assert len(mails) == 1
    kind, subject, body, groups = mails[0]
    assert kind == "link_only" and subject == NOV4_LO_SUBJECT and "hit" not in subject.lower()
    assert len(groups) == 1 and len(groups[0]["excerpts"]) == 2
    assert "1. Cottonwood Heights Planning Commission — November 4, 2026" in body
    assert f"   Agenda: {AGENDA_URL}" in body and f"   Packet: {PACKET_URL}" in body
    assert f"   City agendas page: {CH_URL}" in body
    assert "could not be read (HTTP 403), so they were not checked for rental items." in body
    assert "This email is not a match: only the links are below." in body
    for banned in ("Matched", "Agenda item:", "Short-Term", "ZTA-26-006", "Source:", "PMN notice:", "matching"):
        assert banned not in body, banned


def test_link_only_needs_a_watched_city_with_alerts_on(tmp_path):
    _e, stats, _f = _scan_blocked(tmp_path)
    u = stats["unreadable"]
    assert aw_alerts.link_only_hits(u, dict(CHRIS, alerts_on=0), OCT31) == []
    assert aw_alerts.link_only_hits(u, dict(CHRIS, cities={"sandy": {"enabled": 1}}), OCT31) == []
    assert aw_alerts.link_only_hits(u, dict(CHRIS, cities={"ch": {"enabled": 0}}), OCT31) == []
    assert aw_alerts.link_only_hits(u, CHRIS, OCT31, skip_cities={"ch"}) == []          # alerts_off_cities
    # no keyword needed: a user watching the city with other keywords still hears the link
    assert len(aw_alerts.link_only_hits(u, dict(CHRIS, keywords=aw_alerts.parse_keywords("parking")), OCT31)) == 2


def test_past_meeting_row_with_blocked_pdf_gives_nothing(tmp_path):
    nov5 = dt.datetime(2026, 11, 5, 9, 0, tzinfo=aw.TZ)       # every row on the page is now in the past
    entries, stats, _f = _scan_blocked(tmp_path, now=nov5)
    assert entries == [] and stats["unreadable"] == [] and stats["upcoming_rows"] == 0
    # and a stale item from an earlier scan is never sent once its meeting is past
    _e, s31, _f = _scan_blocked(tmp_path, name="scanb2.db")
    assert aw_alerts.link_only_hits(s31["unreadable"], CHRIS, nov5) == []


def test_budget_skip_is_not_could_not_be_read(tmp_path):
    def cap(url, page_url, prev):
        raise acs.BudgetSkip("daily request budget reached; city pages skipped")
    _e, stats, failures = _scan_blocked(tmp_path, fetch=cap)
    assert stats["unreadable"] == [] and failures                 # logged, never emailed


def test_document_read_on_an_earlier_run_is_not_link_only(tmp_path):
    con = _con(tmp_path, "scanc.db")
    cache = {PACKET_URL: {"text": PACKET}, AGENDA_URL: {"text": AGENDA_TEXT}}
    _e, stats, _f = acs.scan({"cities": _cities()}, con, OCT31, cache, fetch_page_fn=lambda u: PAGE_HTML,
                             fetch_doc_fn=_blocked)
    assert stats["unreadable"] == []


def test_link_only_record_never_suppresses_the_real_alert(tmp_path):
    con = _con(tmp_path)
    aw_alerts.ensure_schema(con)
    _e, stats, _f = _scan_blocked(tmp_path)
    for h in aw_alerts.link_only_hits(stats["unreadable"], CHRIS, OCT31):
        assert aw_alerts.pending(con, OWNER, h)
        aw_alerts.record(con, OWNER, h, "sent")
        assert not aw_alerts.pending(con, OWNER, h)              # once per document link
    # the PDF becomes readable: normal matching, and every real hit is still pending
    hits = aw_alerts.match_user(_entry(tmp=tmp_path), CHRIS, OCT31)
    assert hits and all(aw_alerts.pending(con, OWNER, h) for h in hits)
    assert all(aw_alerts.prior(con, OWNER, h) is None for h in hits)


def test_daily_run_pdf_403_link_only_once_then_readable_pdf_sends_normal_alert(db, pmn, run_main, sandbox, monkeypatch):
    pdf = {"status": 403}
    _serve_city(monkeypatch, pdf=pdf)
    add_watch(db.db, OWNER, {})
    add_watch(db.db, A, {"ch": (1, "")})
    add_watch(db.db, B, {"sandy": (1, "")})                    # not watching CH -> nothing
    seeded(db.db)
    run_main("run-owner")
    p = plan(sandbox)
    assert p["mode"] == "send" and [u["email"] for u in p["users"]] == [A]
    assert all(i["kind"] == "link_only" for i in p["users"][0]["items"])
    res = webapp.deliver_user_alerts(p)
    assert res["emails_sent"] == 1 and [c["to"] for c in db.calls] == [A]
    s, body = db.calls[0]["subject"], db.calls[0]["body"]
    # frozen clock Oct 7: the Nov 4 row (403) and the Oct 21 row (made-up links, 404) are both upcoming
    assert s == "Agenda Watch: 2 new city meeting documents posted (could not be read)"
    assert f"   Packet: {PACKET_URL}" in body and "(HTTP 403)" in body and "(HTTP 404)" in body
    assert "Matched" not in body and "Agenda item:" not in body
    with sqlite3.connect(db.db) as c:
        sent = c.execute("SELECT item, meeting FROM alerts_sent WHERE email=?", (A,)).fetchall()
    assert ("New packet posted (could not be read): Cottonwood Heights Planning Commission November 4, 2026",
            "November 4, 2026") in sent
    # second run, still blocked: no resend
    run_main("run-owner")
    assert webapp.deliver_user_alerts(plan(sandbox))["emails_sent"] == 0 and len(db.calls) == 1
    # the packet becomes readable: the normal hit alert goes out
    pdf["status"] = 200
    run_main("run-owner")
    res = webapp.deliver_user_alerts(plan(sandbox))
    assert res["emails_sent"] == 1 and len(db.calls) == 2
    assert db.calls[1]["subject"] == "Agenda Watch: Cottonwood Heights Planning Commission — November 4, 2026"
    assert "Item: 3.3 Project ZTA-26-006" in db.calls[1]["body"]
    run_main("run-owner")
    assert webapp.deliver_user_alerts(plan(sandbox))["emails_sent"] == 0 and len(db.calls) == 2


def test_daily_run_page_403_logs_only_and_emails_nobody(db, pmn, run_main, sandbox, monkeypatch):
    calls = _serve_city(monkeypatch, page_status=403)
    add_watch(db.db, OWNER, {"ch": (1, "")})
    add_watch(db.db, A, {"ch": (1, "")})
    seeded(db.db)
    code, out, err = run_main("run-owner")
    assert code == 0 and calls == [CH_URL]                      # no document is guessed or requested
    p = plan(sandbox)
    assert not any(u.get("items") for u in p.get("users") or [])
    assert webapp.deliver_user_alerts(p)["emails_sent"] == 0 and db.calls == []
    with sqlite3.connect(db.db) as c:
        errs = c.execute("SELECT errors FROM fetch_runs ORDER BY id DESC").fetchone()[0]
    assert f"city source blocked: {CH_URL} HTTP 403" in errs
