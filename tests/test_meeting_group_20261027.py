"""2026-10-27: one meeting = one email item, agenda title instead of "not identified", and a run that
sent alert emails is never logged as 'silence'. Vineyard fixtures are the stored text of PMN notice
1118321 and packet https://www.utah.gov/pmn/files/1506983.pdf (10.27.26 CC Packet.pdf)."""
import datetime as dt
import sqlite3
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

import agenda_watch  # noqa: F401  (aw_alerts uses its matcher)
import aw_alerts
import aw_store
from conftest import OWNER
from test_user_alerts import add_watch, db, plan, rows, run_and_deliver, seeded, SOON  # noqa: F401

F = Path(__file__).parent / "fixtures" / "vineyard_20261027"
TZ = ZoneInfo("America/Denver")
NOW = dt.datetime(2026, 10, 27, 8, 23, tzinfo=TZ)
KW = ["short-term rental", "STR", "rental license", "business license", "zoning", "conditional use",
      "transient tax", "occupancy"]
PACKET = "https://www.utah.gov/pmn/files/1506983.pdf"
NOTICE = "https://www.utah.gov/pmn/sitemap/notice/1118321.html"


def vineyard():
    return {"city": "Vineyard", "city_key": "vineyard", "notice_url": NOTICE, "meeting_body": "City Council",
            "notice_title": "City Council Meeting", "meeting_datetime": "October 27, 2026 06:00 PM",
            "event_iso": "2026-10-27T18:00:00-06:00",
            "docs": [("PMN notice text", NOTICE, (F / "notice_1118321.txt").read_text()),
                     ("10.27.26 CC Packet.pdf", PACKET, (F / "1506983.txt").read_text())]}


@pytest.fixture
def vy_hits():
    w = {"email": "x@example.invalid", "alerts_on": True, "keywords": KW,
         "cities": {"vineyard": {"enabled": True, "street": ""}}}
    return aw_alerts.match_user([vineyard()], w, NOW)


# ------------------------------------------------------------------ Vineyard Oct 27 data
def test_vineyard_notice_agenda_parses_dotted_items_with_trailing_dot():
    items = {it["num"]: it for it in aw_alerts._agenda_for_notice(vineyard())}
    assert items["6.1"]["heading"] == "Approval of the October 13, 2026, City Council Meeting Minutes"
    assert items["4.1"]["heading"].startswith("Recognize the Public Works Team")
    assert items["7.1"]["heading"].startswith("Approve Resolution 2026 - 43 Flock Safety")


def test_vineyard_hits_all_get_the_agenda_heading_and_minutes_note(vy_hits):
    assert len(vy_hits) == 4                                       # the four hits that went out Oct 27
    assert {h["meeting_date"][:10] for h in vy_hits} == {"2026-10-27"}
    for h in vy_hits:
        assert h["agenda_item"] == "Work session 4.1 Discuss Short-Term Rentals"
        assert h["minutes_of"] == "October 13, 2026"
        assert h["on_agenda_as"] == "6.1 Approval of the October 13, 2026, City Council Meeting Minutes"
    assert {h["fingerprint"] for h in vy_hits} == {
        "93f297e76b6c619bf8c3ecf15011df7d1eaf2f8f", "7b420835a35e5a3145d9b33b82b49fef8a7cbb17",
        "54416e32b3d36db47a52c7f1fd64c99cea6a718c", "5016abd7b01999f961a320b9abad8b2114b7c077"}  # history keys unchanged


def test_vineyard_email_is_one_item_led_by_the_agenda_title(vy_hits):
    subject, body = aw_alerts.build_user_email(vy_hits, "https://agendawatch.app")
    assert subject == "Agenda Watch: Vineyard City Council — October 27, 2026"
    assert "Agenda Watch found 1 matching upcoming agenda item in public agendas" in body
    assert body.count("Item:") == 1 and "not identified" not in body
    assert "\n   Item: Work session 4.1 Discuss Short-Term Rentals\n" in body
    assert "2. Agenda item" not in body
    assert ("Note: this text is from the October 13, 2026 meeting minutes included in the packet "
            "(on this agenda as 6.1 Approval of the October 13, 2026, City Council Meeting Minutes).") in body
    assert "Excerpt (from the packet/attachment 10.27.26 CC Packet.pdf, pages 11–12):" in body
    ex = body.split("pages 11–12):\n")[1].split("   Source:")[0]
    assert ex.count('     "') == 2                                  # at most two minutes sentences (Oct 30 cap)
    assert '"Mr. Kennedy reported that enforcement efforts had resulted in the closure of 26' in ex
    assert "WORK SESSION 4.1" not in ex                            # the title line is not repeated
    assert body.count("Source: " + PACKET) == 1 and body.count("PMN notice: " + NOTICE) == 1


# ------------------------------------------------------------------ general grouping
def _h(**kw):
    base = {"city": "Sandy", "city_key": "sandy", "meeting_date": "2026-10-30", "meeting_body": "Council",
            "meeting_datetime": "October 30, 2026 7:00 PM", "notice_url": "https://x/n1", "link": "https://x/p.pdf",
            "matched_terms": ["short-term rental"], "is_definition": False, "agenda_item": None,
            "excerpt_source": "the packet/attachment p.pdf, page 3", "item_text": "t"}
    return dict(base, **kw)


def test_hits_from_one_meeting_collapse_and_other_meetings_stay_separate():
    a = _h(agenda_item="5.2 STR licensing — Update.", excerpt_sentence="STR licenses rise.", matched_terms=["STR"])
    b = _h(agenda_item=None, excerpt_sentence="A short-term rental was cited.", excerpt_source="the packet/attachment p.pdf, page 9")
    c = _h(agenda_item="6.1 Business license fees", excerpt_sentence="Business license fees rise.",
           matched_terms=["business license"])
    other_day = _h(meeting_date="2026-11-06", meeting_datetime="November 6, 2026 7:00 PM", notice_url="https://x/n2",
                   agenda_item="3.1 Zoning", excerpt_sentence="Zoning text.")
    groups = aw_alerts.group_by_meeting([b, a, c, other_day])
    assert len(groups) == 2
    g = groups[0]
    assert g["agenda_item"] == "5.2 STR licensing — Update." and g["also_items"] == ["6.1 Business license fees"]
    assert len(g["excerpts"]) == 3 and g["matched_terms"] == ["short-term rental", "STR", "business license"]
    _, body = aw_alerts.build_user_email([b, a, c, other_day], "https://agendawatch.app")
    assert "found 2 matching upcoming agenda items" in body and body.count("Item:") == 2
    one = body.split("1. ")[1].split("\n2. ")[0]
    assert "Item: 5.2 STR licensing" in one and "Also on this agenda: 6.1 Business license fees" in one
    assert "Excerpt (from the packet/attachment p.pdf, page" in one
    assert one.count('     "') == 2                                  # at most two excerpts (Oct 30 cap)


def test_same_city_and_date_but_different_notice_is_a_different_meeting():
    groups = aw_alerts.group_by_meeting([_h(excerpt_sentence="a"), _h(notice_url="https://x/other", excerpt_sentence="b")])
    assert len(groups) == 2


def test_heading_fallback_needs_an_agenda_section_line():
    t = "Staff report\n\n2.1 Background\nNightly short-term rental licenses rose."
    assert aw_alerts.nearest_agenda_heading(t, t.index("short-term")) is None
    t2 = "5.\tCONSENT ITEMS\n        5.3.\tShort-Term Rental Fee Schedule (Jane Doe – 5 Minutes)\nShort-term rental fees."
    assert aw_alerts.nearest_agenda_heading(t2, t2.index("Short-term rental fees")) == \
        "Consent items 5.3 Short-Term Rental Fee Schedule"
    assert aw_alerts.nearest_agenda_heading(t2, t2.index("Short-Term Rental Fee")) == \
        "Consent items 5.3 Short-Term Rental Fee Schedule"                     # hit inside the line itself


def test_sentence_start_skips_mr_abbreviation():
    t = "Intro text. Mr. Kennedy reported 26 short-term rentals closed. Next."
    assert aw_alerts.matched_sentence(t, t.index("short-term"), t.index("short-term") + 16) == \
        "Mr. Kennedy reported 26 short-term rentals closed."


# ------------------------------------------------------------------ pipeline: one email item, every hit recorded
_FILL = "Routine city business continues with roads, parks and water reports. " * 12
MULTI = ("1. Call to Order\n" + _FILL + "\n\n2. Discussion of short-term rental regulations\n\n" + _FILL +
         "\n\n3. Business license fee schedule for nightly rentals\n\n" + _FILL + "\n\n4. Transient tax report\n\n"
         + _FILL + "\n\n5. Adjourn")


def test_one_meeting_one_email_item_and_all_hits_deduped(db, pmn, run_main, sandbox):  # noqa: F811
    add_watch(db.db, OWNER, {})
    add_watch(db.db, "test+a@example.invalid", {"ch": (1, "")})
    seeded(db.db)
    pmn.add_notice("Cottonwood Heights", SOON, MULTI)
    code, p, res = run_and_deliver(run_main, sandbox)
    [u] = p["users"]
    assert len(u["items"]) >= 2                                    # several hits, one meeting
    assert res["emails_sent"] == 1 and res["items_sent"] == 1 and res["hits_sent"] == len(u["items"])
    [mail] = db.calls
    assert mail["body"].count("Item:") == 1 and "found 1 matching upcoming agenda item" in mail["body"]
    sent_rows = [r for r in rows(db.db, "test+a@example.invalid") if r["status"] == "sent"]
    assert len(sent_rows) == len(u["items"])                       # every collapsed hit -> history
    with sqlite3.connect(db.db) as c:
        assert c.execute("SELECT COUNT(*) FROM alerts_sent WHERE email='test+a@example.invalid'").fetchone()[0] == 1
    db.calls.clear()
    code, p2, res2 = run_and_deliver(run_main, sandbox)
    assert res2["emails_sent"] == 0 and not db.calls               # nothing re-sent next run


def test_plan_caps_by_meeting_not_by_hit(db, pmn, run_main, sandbox):  # noqa: F811
    import json
    cfgp = sandbox.root / "config.json"
    cfg = json.loads(cfgp.read_text())
    cfg["max_items_per_email"] = 1
    cfgp.write_text(json.dumps(cfg))
    add_watch(db.db, OWNER, {})
    add_watch(db.db, "test+a@example.invalid", {"ch": (1, "")})
    seeded(db.db)
    pmn.add_notice("Cottonwood Heights", SOON, MULTI)
    pmn.add_notice("Cottonwood Heights", SOON + dt.timedelta(days=1),
                   "1. Welcome\n\n2. Ordinance amending nightly short-term rental parking limits\n\n3. Close")
    code, p, res = run_and_deliver(run_main, sandbox)
    [u] = p["users"]
    meetings = {aw_alerts.meeting_key(h) for h in u["items"]}
    assert len(meetings) == 1 and u["more"] == 1          # capped by meeting (3+ hits in total, 2 meetings)
    assert res["items_sent"] == 1 and res["hits_sent"] == len(u["items"])


# ------------------------------------------------------------------ run outcome
def _runs_db():
    c = sqlite3.connect(":memory:")
    aw_store.ensure_schema(c)
    aw_alerts.ensure_schema(c)
    return c


def _run(c, started, finished, outcome="silence", code=0):
    return c.execute("INSERT INTO fetch_runs(started_at,finished_at,outcome,exit_code,total_notices,per_city_json,"
                     "errors,late_minutes,purged_notices,purged_runs) VALUES(?,?,?,?,366,'{}','',0,0,0)",
                     (started, finished, outcome, code)).lastrowid


def _sent(c, at, rid, fp):
    c.execute("INSERT INTO user_alerts(email,fingerprint,city_key,meeting_date,item_text,status,attempts,resend_id,"
              "first_at,last_at) VALUES('a@x',?,'vineyard','2026-10-27','t','sent',1,?,?,?)", (fp, rid, at, at))


def test_mark_alerts_sent_run_is_not_silence():
    c = _runs_db()
    rid = _run(c, "2026-10-28T08:21:51-06:00", "2026-10-28T08:23:52-06:00")
    assert aw_store.mark_alerts(c, rid, 1, 1, 4) == "alerts_sent"
    lr = aw_store.latest_run(c)
    assert lr["outcome"] == "alerts_sent" and lr["alert_emails"] == 1 and lr["alert_hits"] == 4
    assert lr["outcome_label"] == "alerts sent (1 email, 1 item)"
    quiet = _run(c, "2026-10-29T08:21:51-06:00", "2026-10-29T08:23:52-06:00")
    assert aw_store.mark_alerts(c, quiet, 0, 0, 0) == "silence"
    hit = _run(c, "2026-10-30T08:21:51-06:00", "2026-10-30T08:23:52-06:00", outcome="hit", code=4)
    assert aw_store.mark_alerts(c, hit, 2, 3) == "hit"
    assert aw_store.outcome_label(aw_store.latest_run(c)) == "hit + alerts sent (2 emails, 3 items)"


def test_reconcile_fixes_the_oct27_run_record_once():
    c = _runs_db()
    r14 = _run(c, "2026-10-26T08:21:50-06:00", "2026-10-26T08:23:40-06:00")
    r15 = _run(c, "2026-10-27T08:21:51-06:00", "2026-10-27T08:23:52-06:00")
    r16 = _run(c, "2026-10-28T08:21:51-06:00", "2026-10-28T08:23:50-06:00")
    for fp in ("93f297e7", "7b420835", "54416e32", "5016abd7"):
        _sent(c, "2026-10-27T08:23:53-06:00", "01a17dbf-0912-795c-9f97-6673c49a7ad6", fp)
    _sent(c, "2026-10-25T09:00:00-06:00", "old", "x")              # owner test send outside any run window
    assert aw_store.reconcile_alert_outcomes(c) == [r15]
    got = {r[0]: r[1:] for r in c.execute("SELECT id,outcome,alert_emails,alert_items FROM fetch_runs")}
    assert got[r15] == ("alerts_sent", 1, 4) and got[r14] == ("silence", 0, 0) and got[r16] == ("silence", 0, 0)
    assert aw_store.outcome_label(dict(zip(("outcome", "alert_emails", "alert_items"), got[r15]))) == \
        "alerts sent (1 email, 4 items)"
    assert aw_store.reconcile_alert_outcomes(c) == []                # idempotent


def test_init_db_reconciles_and_admin_shows_label(db):  # noqa: F811
    from test_web_watch import webapp
    with sqlite3.connect(db.db) as c:
        _run(c, "2026-10-27T08:21:51-06:00", "2026-10-27T08:23:52-06:00")
        _sent(c, "2026-10-27T08:23:53-06:00", "rid1", "a")
        _sent(c, "2026-10-27T08:23:53-06:00", "rid1", "b")
    webapp.init_db()
    [r] = webapp.admin_runs()
    assert r["outcome"] == "alerts_sent" and r["outcome_label"] == "alerts sent (1 email, 2 items)"
