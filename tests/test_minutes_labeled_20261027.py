"""2026-10-27 owner rule (supersedes 'minutes never alert'): minutes are LABELED, not dropped.
- An upcoming agenda line emails as an upcoming item (shape unchanged).
- Minutes of a past meeting (quoted in a packet, or the packet section approving them) email ONCE, when
  first seen, in a separate 'already discussed' email: never 'Agenda item' / 'upcoming'.
- Minutes dedupe by identity (city + past meeting date + body), not by link: the same minutes in a later
  approval packet with a new PDF link never email again.
Vineyard fixtures: PMN notice 1118321 + 10.27.26 CC Packet.pdf (https://www.utah.gov/pmn/files/1506983.pdf),
replayed offline (no network, mocked Resend, no POST /__aw/run, never the owner's watch)."""
import datetime as dt
import shutil
import sqlite3
from pathlib import Path

import pytest

import agenda_watch as aw
import aw_alerts
import aw_store
from conftest import OWNER
from test_meeting_group_20261027 import KW, NOW, F, vineyard  # noqa: F401
from test_user_alerts import add_watch, db, rows, run_and_deliver, seeded, SOON  # noqa: F401

A = "test+a@example.invalid"
BASE = "https://agendawatch.app"
KEY = "minutes|vineyard|2026-10-13|city council"
UPCOMING_STR = ("CITY COUNCIL MEETING AGENDA\n1. Call to Order\n\n2. PUBLIC COMMENTS\n\n"
                "4. WORK SESSION\n4.1 Discuss Short-Term Rentals (Ed Kennedy - 10 Minutes)\n\n"
                "5. CONSENT ITEMS\n5.1 Approval of the October 13, 2026, City Council Meeting Minutes\n\n6. Adjourn")
SUBJECT = "Agenda Watch: Vineyard City Council discussed short-term rentals (October 13, 2026 minutes)"


def _vy_watch():
    return {"email": A, "alerts_on": True, "keywords": KW, "cities": {"vineyard": {"enabled": True, "street": ""}}}


def _con_with(watch):
    c = sqlite3.connect(":memory:")
    c.executescript("CREATE TABLE watches(email TEXT PRIMARY KEY, keywords TEXT, alerts_on INTEGER, updated_at TEXT);"
                    "CREATE TABLE watch_cities(email TEXT, city_key TEXT, enabled INTEGER, street TEXT, updated_at TEXT);")
    c.execute("INSERT INTO watches VALUES(?,?,1,'x')", (watch["email"], "\n".join(watch["keywords"])))
    for k in watch["cities"]:
        c.execute("INSERT INTO watch_cities VALUES(?,?,1,'','x')", (watch["email"], k))
    aw_alerts.ensure_schema(c)
    return c


def later_packet():
    """The same Oct 13 minutes in a LATER packet (new notice, new PDF link, later meeting)."""
    n = vineyard()
    n["notice_url"] = "https://www.utah.gov/pmn/sitemap/notice/1119999.html"
    n["meeting_datetime"], n["event_iso"] = "November 10, 2026 6:00 PM", "2026-11-10T18:00:00-07:00"
    n["docs"] = [(t, ("https://www.utah.gov/pmn/files/1509999.pdf" if "/files/" in u else n["notice_url"]), x)
                 for t, u, x in n["docs"]]
    return n


def _mails(con, notices):
    p = aw_alerts.plan(con, notices, {"alerts_off_cities": []}, NOW)
    return p, [m for u in p["users"] for m in aw_alerts.build_user_emails(u["items"], BASE)]


# ------------------------------------------------------------------ classification
def test_vineyard_oct13_minutes_hits_are_classified_past_minutes():
    hs = aw_alerts.match_user([vineyard()], _vy_watch(), NOW)
    assert len(hs) == 4
    for h in hs:
        assert h["kind"] == "past_minutes" and h["minutes_of"] == "October 13, 2026"
        assert h["minutes_key"] == KEY


# ------------------------------------------------------------------ (a) first seen -> 1 past-minutes item
def test_a_vineyard_oct13_minutes_first_seen_one_past_minutes_item():
    p, mails = _mails(_con_with(_vy_watch()), [vineyard()])
    [u] = p["users"]
    assert len(u["items"]) == 4                                      # 4 hits, all kept (nothing dropped)
    [(kind, subject, body, groups)] = mails                          # 1 email, 1 item
    assert kind == "past_minutes" and len(groups) == 1
    assert subject == SUBJECT
    assert "\n1. Already discussed at the October 13, 2026 meeting\n" in body
    assert "This discussion already happened." in body
    assert "Agenda item" not in body and "upcoming" not in body.lower() and "upcoming" not in subject.lower()
    assert "Ed Kennedy provided an update" in body and "closure of 26 unauthorized" in body
    assert "   Source: https://www.utah.gov/pmn/files/1506983.pdf" in body
    assert "\n   Meeting: Vineyard City Council — October 13, 2026 (minutes)\n" in body
    assert "\n   These minutes were posted in the October 27, 2026 06:00 PM City Council packet for approval" in body
    assert "\n2. " not in body


# ------------------------------------------------------------------ (b) later approval packet, new link -> 0
def test_b_same_minutes_in_later_packet_new_link_zero_items(db):  # noqa: F811
    from test_web_watch import webapp
    add_watch(db.db, A, {"vineyard": (1, "")}, keywords="\n".join(KW))
    with sqlite3.connect(db.db) as c:
        p1 = aw_alerts.plan(c, [vineyard()], {"alerts_off_cities": []}, NOW)
    r1 = webapp.deliver_user_alerts(dict(p1, mode="send"))
    assert r1["emails_sent"] == 1 and r1["items_sent"] == 1 and r1["hits_sent"] == 4
    [mail] = db.calls
    assert mail["subject"] == SUBJECT
    [row] = [r for r in rows(db.db, A) if r["fingerprint"] == KEY]
    assert row["status"] == "sent" and row["meeting_date"] == "2026-10-13"
    with sqlite3.connect(db.db) as c:
        p2 = aw_alerts.plan(c, [later_packet()], {"alerts_off_cities": []}, NOW)
        p3 = aw_alerts.plan(c, [vineyard(), later_packet()], {"alerts_off_cities": []}, NOW)
    assert p2["users"] == [] and p3["users"] == []                   # 0 items: same minutes identity
    r2 = webapp.deliver_user_alerts(dict(p2, mode="send"))
    assert r2["emails_sent"] == 0 and len(db.calls) == 1


def test_b_in_memory_new_link_same_identity():
    con = _con_with(_vy_watch())
    _, [(_, _, _, [g])] = _mails(con, [vineyard()])
    aw_alerts.record_minutes(con, A, g, "sent", resend_id="r1")
    assert _mails(con, [later_packet()])[1] == []


def test_pipeline_first_packet_one_minutes_email_later_packet_none(db, pmn, run_main, sandbox, monkeypatch):  # noqa: F811
    monkeypatch.setattr(aw, "extract_pdf_text", lambda path: path.read_bytes().split(b"\n", 1)[1].decode())
    add_watch(db.db, OWNER, {})
    add_watch(db.db, A, {"ch": (1, "")})
    seeded(db.db)
    pmn.set_file(1506983, (F / "1506983.txt").read_text())
    pmn.add_notice("Cottonwood Heights", SOON, (F / "notice_1118321.txt").read_text(), files=[1506983])
    code, p, res = run_and_deliver(run_main, sandbox)
    assert res["emails_sent"] == 1 and res["items_sent"] == 1
    [mail] = db.calls
    assert "discussed" in mail["subject"] and "(October 13, 2026 minutes)" in mail["subject"]
    assert "Agenda item" not in mail["body"] and "upcoming" not in mail["body"].lower()
    pmn.set_file(1509999, (F / "1506983.txt").read_text())
    pmn.add_notice("Cottonwood Heights", SOON + dt.timedelta(days=14), (F / "notice_1118321.txt").read_text(),
                   files=[1509999])
    code, p2, res2 = run_and_deliver(run_main, sandbox)
    assert p2["users"] == [] and res2["emails_sent"] == 0 and len(db.calls) == 1


# ------------------------------------------------------------------ (c) real upcoming line -> 1 upcoming item
def test_c_synthetic_upcoming_41_line_one_upcoming_item():
    n = vineyard()
    n["docs"] = [("PMN notice text", n["notice_url"], UPCOMING_STR)]
    p, mails = _mails(_con_with(_vy_watch()), [n])
    [(kind, subject, body, groups)] = mails
    assert kind == "upcoming" and len(groups) == 1
    assert subject == "Agenda Watch: Vineyard City Council — October 27, 2026"
    assert "Agenda Watch found 1 matching upcoming agenda item" in body
    assert "\n   Item: 4.1 Discuss Short-Term Rentals\n" in body and "Already discussed" not in body


def test_upcoming_pipeline_still_emails(db, pmn, run_main, sandbox):  # noqa: F811
    add_watch(db.db, OWNER, {})
    add_watch(db.db, A, {"ch": (1, "")})
    seeded(db.db)
    pmn.add_notice("Cottonwood Heights", SOON, UPCOMING_STR)
    code, p, res = run_and_deliver(run_main, sandbox)
    assert res["emails_sent"] == 1 and res["items_sent"] == 1
    [mail] = db.calls
    assert "\n   Item: 4.1 Discuss Short-Term Rentals\n" in mail["body"]


# ------------------------------------------------------------------ mixed run: separate emails, label correct
def test_mixed_run_sends_two_separate_emails_and_alerts_sent_label(db):  # noqa: F811
    from test_web_watch import webapp
    add_watch(db.db, A, {"vineyard": (1, "")}, keywords="\n".join(KW))
    n = vineyard()
    n["docs"] = [("PMN notice text", n["notice_url"], UPCOMING_STR)] + n["docs"][1:]
    with sqlite3.connect(db.db) as c:
        p = aw_alerts.plan(c, [n], {"alerts_off_cities": []}, NOW)
    r = webapp.deliver_user_alerts(dict(p, mode="send"))
    assert r["emails_sent"] == 2 and r["items_sent"] == 2 and r["users"] == 1
    up, past = db.calls
    assert up["subject"].startswith("Agenda Watch: Vineyard") and "Item: 4.1" in up["body"]
    assert "Already discussed" not in up["body"]
    assert past["subject"] == SUBJECT and "Agenda item" not in past["body"]
    c = sqlite3.connect(":memory:")
    aw_store.ensure_schema(c)
    rid = c.execute("INSERT INTO fetch_runs(started_at,finished_at,outcome,exit_code,total_notices,per_city_json,"
                    "errors,late_minutes,purged_notices,purged_runs) VALUES('2026-10-27T08:21:51-06:00',"
                    "'2026-10-27T08:23:52-06:00','silence',0,366,'{}','',0,0,0)").lastrowid
    assert aw_store.mark_alerts(c, rid, r["emails_sent"], r["items_sent"], r["hits_sent"]) == aw_store.ALERTS_SENT


# ------------------------------------------------------------------ approval-of-minutes section
PACKET = ("AGENDA\n4.1 Short-term rental licensing update\n5.1 Approval of the October 13, 2026 Meeting Minutes\n"
          "6.1 Adjourn\n\fApproval of the October 13, 2026 City Council Meeting Minutes\n\nCouncil Meeting, "
          "October 13, 2026\nThe council heard a report on short-term rental enforcement and complaints.\n\f"
          "STAFF REPORT\nNightly short-term rental license fees for the coming year.")


def test_minutes_approval_section_is_past_minutes_with_same_identity():
    n = vineyard()
    n["docs"] = [("PMN notice text", n["notice_url"], "1. Call to Order\n2. Adjourn"),
                 ("packet.pdf", "https://www.utah.gov/pmn/files/9.pdf", PACKET)]
    hs = {h["excerpt_sentence"][:30]: h for h in aw_alerts.match_user([n], _vy_watch(), NOW)}
    past = [h for h in hs.values() if h["kind"] == "past_minutes"]
    assert past and all(h["minutes_key"] == KEY for h in past)
    assert any(h["kind"] == "upcoming" for h in hs.values())


# ------------------------------------------------------------------ (d) backfill
def _vy_sent_db(path):
    c = sqlite3.connect(path)
    aw_alerts.ensure_schema(c)
    for i, h in enumerate(aw_alerts.match_user([vineyard()], _vy_watch(), NOW)):
        aw_alerts.record(c, A, h, "sent", resend_id=aw_alerts.MINUTES_BACKFILL[0]["resend_id"],
                         now_iso="2026-10-27T08:23:53-06:00")
    c.commit()
    return c


def test_d_backfill_marks_oct13_minutes_sent_idempotent(tmp_path):
    c = _vy_sent_db(tmp_path / "x.db")
    assert aw_alerts.backfill_minutes_keys(c) == 1
    assert aw_alerts.backfill_minutes_keys(c) == 0                   # idempotent
    r = aw_alerts.minutes_prior(c, A, KEY)
    assert r["status"] == "sent" and r["resend_id"] == "01a17dbf-0912-795c-9f97-6673c49a7ad6"
    assert r["first_at"] == "2026-10-27T08:23:53-06:00"
    c.executescript("CREATE TABLE watches(email TEXT PRIMARY KEY, keywords TEXT, alerts_on INTEGER, updated_at TEXT);"
                    "CREATE TABLE watch_cities(email TEXT, city_key TEXT, enabled INTEGER, street TEXT, updated_at TEXT);")
    c.execute("INSERT INTO watches VALUES(?,?,1,'x')", (A, "\n".join(KW)))
    c.execute("INSERT INTO watch_cities VALUES(?,'vineyard',1,'','x')", (A,))
    assert _mails(c, [vineyard()])[1] == [] and _mails(c, [later_packet()])[1] == []


SNAP = Path("/workspace/backups/live-20261027-minutes/pre-labeled/x/data/agenda_watch.db")


@pytest.mark.skipif(not SNAP.exists(), reason="live snapshot not on this machine")
def test_d_backfill_on_copy_of_live_snapshot(tmp_path):
    shutil.copy(SNAP, tmp_path / "live.db")
    c = sqlite3.connect(tmp_path / "live.db")
    assert aw_alerts.backfill_minutes_keys(c) == 1
    assert aw_alerts.backfill_minutes_keys(c) == 0
    [(email, st, rid)] = c.execute("SELECT email,status,resend_id FROM user_alerts WHERE fingerprint=?", (KEY,)).fetchall()
    assert st == "sent" and rid == "01a17dbf-0912-795c-9f97-6673c49a7ad6" and email != OWNER


def test_d_init_db_runs_backfill(db):  # noqa: F811
    from test_web_watch import webapp
    c = _vy_sent_db(db.db)
    c.close()
    webapp.init_db()
    webapp.init_db()
    assert [r["status"] for r in rows(db.db, A) if r["fingerprint"] == KEY] == ["sent"]
