"""Per-user alerts (throwaway sandbox DB, fake PMN, mocked Resend; no network, no real email)."""
import datetime as dt
import json
import sqlite3
import types

import pytest
from fastapi.testclient import TestClient

import agenda_watch as aw
import aw_alerts
from conftest import FROZEN_NOW, OWNER
from test_web_watch import webapp  # noqa: F401

KW = "short-term rental\nSTR\nrental license\nbusiness license\nzoning\nconditional use\ntransient tax\noccupancy"
SOON = FROZEN_NOW + dt.timedelta(days=5)
PAST = FROZEN_NOW - dt.timedelta(days=5)
STR_ITEM = "2. Discussion of short-term rental regulations"
STR_AGENDA = f"1. Call to Order\n{STR_ITEM}\n3. Adjourn"
A, B, C, D, E = (f"test+{x}@example.invalid" for x in "abcde")
H = {"x-aw-internal-secret": "s3cret"}


@pytest.fixture
def db(sandbox, monkeypatch):
    """App + daily job share one throwaway DB; Resend is mocked (records every send)."""
    monkeypatch.setattr(webapp, "DB", sandbox.db)
    monkeypatch.setattr(webapp, "HISTORY_JSON", sandbox.state / "sent_history.json")
    monkeypatch.setattr(webapp, "OUTBOX", sandbox.outbox / "magic")
    webapp.init_db()
    monkeypatch.setenv("RESEND_API_KEY", "re_test_dummy")
    monkeypatch.setenv("NOTIFY_TO", "project@example.invalid")
    sent = types.SimpleNamespace(calls=[], fail_for=set())

    def fake_resend(to, subject, body, redirect_owner=True, **kwargs):
        rcpt = webapp.owner_inbox() if (redirect_owner and to.lower() == webapp.OWNER) else to
        sent.calls.append({"to": rcpt, "subject": subject, "body": body})
        if to in sent.fail_for:
            return False, "send FAILED via Resend: HTTP 422 bad address"
        return True, f"sent via Resend (re_{len(sent.calls)})"

    monkeypatch.setattr(webapp, "_send_via_resend", fake_resend)
    sent.db = sandbox.db
    return sent


def add_watch(path, email, cities, alerts_on=1, keywords=KW):
    with sqlite3.connect(path) as c:
        c.execute("INSERT OR IGNORE INTO users(email, created_at) VALUES(?, '2026-10-01T00:00:00-06:00')", (email,))
        c.execute("INSERT OR REPLACE INTO watches(email,keywords,alerts_on,updated_at) VALUES(?,?,?,?)",
                  (email, keywords, alerts_on, "2026-10-01T00:00:00-06:00"))
        c.execute("DELETE FROM watch_cities WHERE email=?", (email,))
        for k, (on, street) in cities.items():
            c.execute("INSERT INTO watch_cities(email,city_key,enabled,street,updated_at) VALUES(?,?,?,?,?)",
                      (email, k, on, street, "2026-10-01T00:00:00-06:00"))


def seeded(path):
    with sqlite3.connect(path) as c:
        c.execute("CREATE TABLE IF NOT EXISTS schema_migrations(id TEXT PRIMARY KEY, applied_at TEXT NOT NULL)")
        c.execute("INSERT OR IGNORE INTO schema_migrations(id, applied_at) VALUES('alert_seed_v1', 'x')")


def plan(sandbox):
    return json.loads((sandbox.state / "user_alert_plan.json").read_text())


def by_user(p):
    return {u["email"]: [h["item_text"] for h in u["items"]] for u in p["users"]}


def rows(path, email=None):
    with sqlite3.connect(path) as c:
        c.row_factory = sqlite3.Row
        q = "SELECT * FROM user_alerts" + (" WHERE email=?" if email else "")
        return [dict(r) for r in c.execute(q, (email,) if email else ())]


def run_and_deliver(run_main, sandbox):
    code, out, err = run_main("run-owner")
    p = plan(sandbox)
    return code, p, webapp.deliver_user_alerts(p)


# ------------------------------------------------------------------ filters
def test_city_street_alerts_off_and_multiple_users(db, pmn, run_main, sandbox):
    add_watch(db.db, OWNER, {"sandy": (1, "")})
    add_watch(db.db, A, {"ch": (1, "")})                      # CH, keywords
    add_watch(db.db, B, {"sandy": (1, "")})                   # other city only
    add_watch(db.db, C, {"ch": (1, "")}, alerts_on=0)         # alerts off
    add_watch(db.db, D, {"ch": (1, "100 Main St")})           # CH with a street
    seeded(db.db)
    pmn.add_notice("Cottonwood Heights", SOON,
                   f"1. Call to Order\n{STR_ITEM}\n3. Variance request at 100 Main St\n4. Adjourn")
    code, p, res = run_and_deliver(run_main, sandbox)
    got = by_user(p)
    assert got[A] == [STR_ITEM]                               # keyword hit; the street item is not A's
    # street set: the street item AND the city-wide keyword item (a street never hides a city-wide hit)
    assert sorted(got[D]) == sorted(["3. Variance request at 100 Main St", STR_ITEM])
    assert B not in got and C not in got and OWNER not in got
    assert res["emails_sent"] == 2 and res["emails_failed"] == 0
    assert sorted(c["to"] for c in db.calls) == sorted([A, D])   # one email each, at their own address


def test_keyword_item_naming_another_address_is_sent_and_flagged(db, pmn, run_main, sandbox):
    add_watch(db.db, OWNER, {})
    add_watch(db.db, D, {"ch": (1, "100 Main St")})
    seeded(db.db)
    other = "2. Conditional use permit for a short-term rental at 2104 East Fort Union Blvd"
    pmn.add_notice("Cottonwood Heights", SOON, f"1. Call to Order\n{other}\n3. Adjourn")
    run_main("run-owner")
    [u] = plan(sandbox)["users"]
    [h] = u["items"]
    assert h["item_text"] == other and h["names_other_address"] is True and h["names_user_street"] is False


def test_street_only_matches_its_own_city(db, pmn, run_main, sandbox):
    add_watch(db.db, OWNER, {})
    add_watch(db.db, D, {"ch": (1, "100 Main St")})
    seeded(db.db)
    pmn.add_notice("Sandy", SOON, "1. Call to Order\n2. Variance at 100 Main St\n3. Adjourn")
    run_main("run-owner")
    assert plan(sandbox)["users"] == []


def test_weak_terms_need_rental_language_and_past_meetings_never_alert(db, pmn, run_main, sandbox):
    add_watch(db.db, OWNER, {})
    add_watch(db.db, A, {"ch": (1, "")})
    seeded(db.db)
    pmn.add_notice("Cottonwood Heights", SOON, "1. Call to Order\n2. Zoning map amendment, 10 acres\n3. Adjourn")
    pmn.add_notice("Cottonwood Heights", PAST, STR_AGENDA)
    run_main("run-owner")
    assert plan(sandbox)["users"] == []


def test_cities_in_alerts_off_are_held(db, pmn, run_main, sandbox):
    # The hold mechanism still works when cities are listed (live config is now [] since 2026-10-25).
    p = sandbox.root / "config.json"
    c = json.loads(p.read_text()); c["alerts_off_cities"] = ["wj", "layton"]; p.write_text(json.dumps(c))
    add_watch(db.db, OWNER, {})
    add_watch(db.db, E, {"wj": (1, ""), "layton": (1, "")})
    seeded(db.db)
    pmn.add_notice("West Jordan", SOON, STR_AGENDA)
    pmn.add_notice("Layton", SOON, STR_AGENDA)
    run_main("run-owner")
    p = plan(sandbox)
    assert p["users"] == [] and p["held_cities_off"] == {"wj": 1, "layton": 1}


def test_west_jordan_and_layton_upcoming_hits_now_alert(db, pmn, run_main, sandbox):
    add_watch(db.db, OWNER, {})
    add_watch(db.db, E, {"wj": (1, ""), "layton": (1, "")})
    seeded(db.db)
    pmn.add_notice("West Jordan", SOON, STR_AGENDA)
    pmn.add_notice("Layton", SOON, STR_AGENDA)
    run_main("run-owner")
    p = plan(sandbox)
    assert [u["email"] for u in p["users"]] == [E] and p["held_cities_off"] == {}


def test_owner_alerts_go_to_the_owners_own_address_not_the_project_inbox(db, pmn, run_main, sandbox):
    add_watch(db.db, OWNER, {"sandy": (1, "")})
    seeded(db.db)
    pmn.add_notice("Sandy", SOON, STR_AGENDA)
    code, p, res = run_and_deliver(run_main, sandbox)
    assert [c["to"] for c in db.calls] == [OWNER]
    # run notices addressed to the owner still go to the project inbox; sign-in links go to the owner
    webapp.send_owner_email(OWNER, "s", "b")
    assert db.calls[-1]["to"] == "project@example.invalid"
    webapp.send_email(OWNER, "s", "b")
    assert db.calls[-1]["to"] == OWNER


# ------------------------------------------------------------------ dedupe
def test_dedupe_across_runs(db, pmn, run_main, sandbox):
    add_watch(db.db, OWNER, {})
    add_watch(db.db, A, {"ch": (1, "")})
    seeded(db.db)
    pmn.add_notice("Cottonwood Heights", SOON, STR_AGENDA)
    run_and_deliver(run_main, sandbox)
    assert len(db.calls) == 1
    [r] = rows(db.db, A)
    assert r["status"] == "sent" and r["resend_id"] == "re_1" and r["attempts"] == 1
    _, p, res = run_and_deliver(run_main, sandbox)
    assert p["users"] == [] and len(db.calls) == 1


def test_amended_notice_is_same_hit_unless_item_materially_changes(db, pmn, run_main, sandbox):
    add_watch(db.db, OWNER, {})
    add_watch(db.db, A, {"ch": (1, "")})
    seeded(db.db)
    pmn.add_notice("Cottonwood Heights", SOON, STR_AGENDA)
    run_and_deliver(run_main, sandbox)
    # amended agenda re-posted as a new notice, same meeting, item renumbered + punctuation
    pmn.add_notice("Cottonwood Heights", SOON, "1. Call to Order\n4. Discussion of short-term rental regulations.\n5. Adjourn",
                   title="AMENDED Regular Meeting")
    _, p, _ = run_and_deliver(run_main, sandbox)
    assert p["users"] == [] and len(db.calls) == 1
    # materially different item at the same meeting -> new hit
    pmn.add_notice("Cottonwood Heights", SOON,
                   "1. Call to Order\n6. Public hearing: ordinance banning short-term rentals in R-1 zones\n7. Adjourn",
                   title="AMENDED 2")
    _, p, _ = run_and_deliver(run_main, sandbox)
    assert by_user(p)[A] == ["6. Public hearing: ordinance banning short-term rentals in R-1 zones"]


def test_first_run_before_seed_records_everything_and_sends_nothing(db, pmn, run_main, sandbox):
    add_watch(db.db, OWNER, {"sandy": (1, "")})
    add_watch(db.db, A, {"ch": (1, "")})
    pmn.add_notice("Cottonwood Heights", SOON, STR_AGENDA)
    pmn.add_notice("Sandy", SOON, STR_AGENDA)
    code, p, res = run_and_deliver(run_main, sandbox)
    assert p["mode"] == "seeded" and p["users"] == [] and db.calls == []
    assert {r["email"]: r["status"] for r in rows(db.db)} == {A: "seeded", OWNER: "seeded"}
    _, p, _ = run_and_deliver(run_main, sandbox)              # next run: send mode, nothing new
    assert p["mode"] == "send" and p["users"] == [] and db.calls == []


# ------------------------------------------------------------------ failures
def test_fetch_failure_sends_no_user_email(db, pmn, run_main, sandbox):
    add_watch(db.db, OWNER, {})
    add_watch(db.db, A, {"ch": (1, "")})
    seeded(db.db)
    pmn.add_notice("Cottonwood Heights", SOON, STR_AGENDA)
    layton_bid = next(iter(pmn.bodies[pmn.entities["Layton"]].values()))
    pmn.fail[f"notices.html?id={layton_bid}&"] = 503
    code, p, res = run_and_deliver(run_main, sandbox)
    assert code == 3 and p["mode"] == "withheld" and db.calls == [] and rows(db.db) == []


def test_send_failure_logged_reported_and_retried_at_most_three_runs(db, pmn, run_main, sandbox):
    add_watch(db.db, OWNER, {})
    add_watch(db.db, A, {"ch": (1, "")})
    add_watch(db.db, B, {"ch": (1, "")})
    seeded(db.db)
    db.fail_for.add(A)
    pmn.add_notice("Cottonwood Heights", SOON, STR_AGENDA)
    _, _, res = run_and_deliver(run_main, sandbox)
    assert res["emails_sent"] == 1 and res["emails_failed"] == 1
    assert res["failures"][0]["to"] == "t***@example.invalid" and "422" in res["failures"][0]["detail"]
    [ra] = rows(db.db, A)
    assert ra["status"] == "failed" and ra["attempts"] == 1
    for _ in range(4):
        run_and_deliver(run_main, sandbox)
    assert [c["to"] for c in db.calls].count(A) == 3 and [c["to"] for c in db.calls].count(B) == 1
    assert rows(db.db, A)[0]["attempts"] == 3


# ------------------------------------------------------------------ cloud job + routes
@pytest.fixture
def cloud(db, monkeypatch, sandbox):
    monkeypatch.setenv("AW_CLOUD", "1")
    monkeypatch.setenv("AW_INTERNAL_SECRET", "s3cret")
    (sandbox.root / "web").mkdir(exist_ok=True)
    monkeypatch.setattr(webapp, "__file__", str(sandbox.root / "web" / "app.py"))
    monkeypatch.setattr(webapp, "now_iso", lambda: "2026-10-07T15:00:00-06:00")
    return db


def test_job_sends_per_user_and_run_notice_to_project_inbox(cloud, monkeypatch, sandbox):
    add_watch(cloud.db, A, {"ch": (1, "")})
    monkeypatch.setattr(webapp.time, "sleep", lambda *_: None)
    monkeypatch.setattr(webapp, "_callback", lambda path, payload: "callback 200")
    item = {"fingerprint": aw_alerts.fingerprint("ch", "2026-10-12", STR_ITEM), "city": "Cottonwood Heights",
            "city_key": "ch", "meeting_date": "2026-10-12", "meeting_datetime": "October 12, 2026 06:00 PM",
            "item_text": STR_ITEM, "matched_terms": ["short-term rental"], "link": "https://x/1.pdf", "notice_url": "https://x/n"}

    def fake_run(argv, **kw):
        if argv[2] == "run-owner":
            (sandbox.state / "user_alert_plan.json").write_text(json.dumps(
                {"mode": "send", "users": [{"email": A, "items": [item]}], "held_cities_off": {}}))
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(webapp._sp, "run", fake_run)
    webapp._RUN.update(running=True)
    webapp._run_owner_job("2026-10-07")
    assert [c["to"] for c in cloud.calls] == [A, "project@example.invalid"]
    assert cloud.calls[1]["subject"] == "Agenda Watch run 2026-10-07: 1 alert(s) sent"
    st = TestClient(webapp.app).get("/internal/status", headers=H).json()
    assert st["last_alerts"]["emails_sent"] == 1 and st["last_alerts"]["sent"][0]["resend_id"] == "re_1"
    with sqlite3.connect(cloud.db) as c:
        assert c.execute("SELECT COUNT(*) FROM alerts_sent WHERE email=?", (A,)).fetchone()[0] == 1


def _stored_notice(path, url, text, date="2026-10-12T18:00:00-06:00", city="ch"):
    import aw_store
    with sqlite3.connect(path) as c:
        aw_store.upsert_notices(c, [dict(notice_url=url, city_key=city, metro="Salt Lake metro", meeting_date=date,
                                         body="City Council", title="Regular Meeting", pdf_urls=[], text=text)])


def test_seed_route_marks_existing_hits_and_history_without_sending(cloud, monkeypatch, sandbox):
    monkeypatch.setattr(webapp, "datetime", types.SimpleNamespace(now=lambda tz=None: FROZEN_NOW,
                                                                 fromisoformat=dt.datetime.fromisoformat))
    add_watch(cloud.db, OWNER, {"sandy": (1, "")})
    add_watch(cloud.db, A, {"ch": (1, "")}, alerts_on=0)
    _stored_notice(cloud.db, "https://x/n1", STR_AGENDA)
    (sandbox.state / "sent_history.json").write_text(json.dumps([
        {"date": "2026-10-06", "city": "Cottonwood Heights", "item": "B. ADUs shall not be used as short-term rentals.",
         "link": "https://x/2.pdf", "meeting": "October 21, 2026 05:00 PM", "key": "k"}]))
    c = TestClient(webapp.app)
    r = c.post("/internal/seed-alerts", headers=H, json={"dry_run": True}).json()
    assert r["dry_run"] and rows(cloud.db) == []
    r = c.post("/internal/seed-alerts", headers=H, json={}).json()
    assert r["seeded"] == {"t***@example.invalid": 1, "o***@example.com": 0} and r["seeded_from_history"] == 1
    assert cloud.calls == []
    assert {(x["email"], x["status"]) for x in rows(cloud.db)} == {(A, "seeded"), (OWNER, "seeded")}
    with sqlite3.connect(cloud.db) as con:
        assert con.execute("SELECT 1 FROM schema_migrations WHERE id='alert_seed_v1'").fetchone()


def test_test_alert_route(cloud, monkeypatch, sandbox):
    monkeypatch.setattr(webapp, "datetime", types.SimpleNamespace(now=lambda tz=None: FROZEN_NOW,
                                                                 fromisoformat=dt.datetime.fromisoformat))
    add_watch(cloud.db, OWNER, {"ch": (1, "")})
    add_watch(cloud.db, A, {"ch": (1, "")}, alerts_on=0)
    _stored_notice(cloud.db, "https://x/n1", STR_AGENDA)
    c = TestClient(webapp.app)
    assert c.post("/internal/test-alert", json={"email": A}).status_code == 403
    assert c.post("/internal/test-alert", headers=H, json={"email": OWNER}).status_code == 400
    r = c.post("/internal/test-alert", headers=H, json={"email": A, "dry_run": True}).json()
    assert r["item"] == STR_ITEM and cloud.calls == []
    r = c.post("/internal/test-alert", headers=H, json={"email": A}).json()
    assert r["sent"] and r["resend_id"] == "re_1" and r["subject"].startswith("[Test] Agenda Watch: Cottonwood Heights")
    assert [x["to"] for x in cloud.calls] == [A] and "one-time test" in cloud.calls[0]["body"]
    assert "because alerts are on" not in cloud.calls[0]["body"]
    [row] = rows(cloud.db, A)
    assert row["status"] == "test"
    assert not aw_alerts.should_send(row)                    # the daily run never re-sends it


def test_same_wording_in_two_cities_is_two_hits(db, pmn, run_main, sandbox):
    add_watch(db.db, OWNER, {})
    add_watch(db.db, A, {"ch": (1, ""), "sandy": (1, "")})
    seeded(db.db)
    pmn.add_notice("Cottonwood Heights", SOON, STR_AGENDA)
    pmn.add_notice("Sandy", SOON, STR_AGENDA)
    run_main("run-owner")
    [u] = plan(sandbox)["users"]
    assert sorted(h["city"] for h in u["items"]) == ["Cottonwood Heights", "Sandy"]
