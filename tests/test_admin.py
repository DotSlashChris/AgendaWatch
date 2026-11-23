"""Admin page, run log / notice archive, retention purge, /internal/status. Throwaway DBs only."""
import datetime as dt
import json
import sqlite3

import pytest

import aw_store
from test_web_watch import seed, signed_in_client, webapp  # noqa: F401  (also sets AW_DB)
from test_daily_run import SOON, STR_AGENDA

ADMIN = "admin@agendawatch.app"    # config admin_emails -> admin (2026-10-22 admin swap)
OWNER = "owner@example.com"           # config owner: standard user, NOT an admin
USER = "test+user@example.invalid"


@pytest.fixture
def web(sandbox, monkeypatch):
    db = sandbox.root / "data" / "web.db"
    monkeypatch.setattr(webapp, "DB", db)
    monkeypatch.setattr(webapp, "OUTBOX", sandbox.outbox / "magic")
    monkeypatch.setattr(webapp, "HISTORY_JSON", sandbox.state / "sent_history.json")
    webapp.init_db()
    seed(db, email=USER, status="active", extra_cities=2)
    seed(db, email=ADMIN, status=None)
    seed(db, email=OWNER, status=None)
    webapp.init_db()               # admin_emails rows are (re)marked admin; the owner stays standard
    return db


def add_notice(db, url, city="ch", metro="Salt Lake", date="2026-10-20T18:00:00-06:00", title="Council",
               terms=(), text="agenda text", fetched_at=None):
    with sqlite3.connect(db) as c:
        aw_store.upsert_notices(c, [dict(notice_url=url, city_key=city, metro=metro, meeting_date=date,
                                         title=title, body="City Council", matched_terms=list(terms), text=text)],
                                fetched_at=fetched_at)


# ---------------------------------------------------------------- access control
def test_admin_column_and_owner_migration(web):
    with sqlite3.connect(web) as c:
        rows = dict(c.execute("SELECT email, is_admin FROM users"))
    assert rows == {USER: 0, ADMIN: 1, OWNER: 0}


def test_admin_404_signed_out(web):
    from fastapi.testclient import TestClient
    assert TestClient(webapp.app).get("/admin").status_code == 404


def test_admin_404_non_admin_and_no_nav_link(web):
    client, _, page = signed_in_client(web, USER)
    assert client.get("/admin").status_code == 404
    assert 'href="/admin"' not in page


def test_admin_ok_for_admin_with_nav_and_stats(web):
    client, _, page = signed_in_client(web, ADMIN)
    assert 'href="/admin"' in page
    r = client.get("/admin")
    assert r.status_code == 200
    s = webapp.admin_stats()
    assert (s["total_users"], s["paid_users"], s["extra_cities"]) == (3, 1, 2)
    assert dict(s["users_per_city"])["Cottonwood Heights"] == 3
    assert "Fetch runs" in r.text and "Notices" in r.text


def test_mark_admin_script(web, monkeypatch):
    import runpy, sys, sqlite3
    monkeypatch.setenv("AW_DB", str(web))
    monkeypatch.setattr(sys, "argv", ["mark_admin.py", USER])
    runpy.run_path(str(webapp.ROOT / "scripts" / "mark_admin.py"))
    with sqlite3.connect(web) as c:
        assert c.execute("SELECT is_admin FROM users WHERE email=?", (USER,)).fetchone()[0] == 1
    # The DB flag alone is not enough; admin access requires AW_ADMIN_EMAILS.
    assert webapp.is_admin(USER) is False
    monkeypatch.setenv("AW_ADMIN_EMAILS", USER)
    assert webapp.is_admin(USER) is True


# ---------------------------------------------------------------- notices search
def test_search_filters_and_pagination(web):
    add_notice(web, "u1", city="ch", date="2026-10-20T18:00:00-06:00", terms=["short-term rental"], text="STR rules")
    add_notice(web, "u2", city="slc", date="2026-11-02T18:00:00-06:00", text="budget hearing")
    add_notice(web, "u3", city="provo", metro="Utah County", date="2026-09-01T18:00:00-06:00", text="zoning map")
    add_notice(web, "u1", city="ch", date="2026-10-20T18:00:00-06:00", terms=["short-term rental"], text="STR rules v2")  # dedupe
    s = webapp.search_notices
    assert s()["total"] == 3
    assert [r["notice_url"] for r in s(city="slc")["rows"]] == ["u2"]
    assert [r["notice_url"] for r in s(metro="Utah County")["rows"]] == ["u3"]
    assert [r["notice_url"] for r in s(matched=True)["rows"]] == ["u1"]
    assert [r["notice_url"] for r in s(q="budget")["rows"]] == ["u2"]
    assert [r["notice_url"] for r in s(date_from="2026-10-01", date_to="2026-10-31")["rows"]] == ["u1"]
    assert s(q="v2")["rows"][0]["text"] == "STR rules v2"
    p = s(page=2, page_size=2)
    assert (p["pages"], len(p["rows"])) == (2, 1)
    client, _, _ = signed_in_client(web, ADMIN)
    r = client.get("/admin", params={"city": "slc"})
    assert "budget hearing" in r.text and "STR rules" not in r.text


# ---------------------------------------------------------------- purge
def test_purge_only_old_notices_and_runs(web):
    old = (dt.datetime.now(aw_store.TZ) - dt.timedelta(days=400)).isoformat(timespec="seconds")
    add_notice(web, "old", fetched_at=old)
    add_notice(web, "new")
    with sqlite3.connect(web) as c:
        c.execute("INSERT INTO fetch_runs(started_at,outcome) VALUES(?, 'silence')", (old,))
        c.execute("INSERT INTO fetch_runs(started_at,outcome) VALUES(?, 'silence')", (aw_store.now_iso(),))
        c.execute("INSERT INTO alerts_sent(email,sent_date,city,item,link) VALUES(?,?,?,?,?)",
                  (USER, old[:10], "CH", "x", "y"))
        before = {t: c.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                  for t in ("users", "watches", "watch_cities", "alerts_sent")}
        assert aw_store.purge(c) == (1, 1)
        after = {t: c.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in before}
        assert [r[0] for r in c.execute("SELECT notice_url FROM notices")] == ["new"]
    assert before == after


# ---------------------------------------------------------------- run logging
def test_run_owner_writes_fetch_run_and_notices(pmn, owner_watch, run_main, sandbox):
    owner_watch(cities={"provo": (1, "")})
    pmn.add_notice("Provo", SOON, STR_AGENDA, title="Provo Council")
    pmn.add_notice("Draper", SOON, "1. Call to Order\n2. Budget\n3. Adjourn", title="Draper Council")
    with sqlite3.connect(sandbox.db) as c:
        aw_store.ensure_schema(c)
        old = (dt.datetime.now(aw_store.TZ) - dt.timedelta(days=500)).isoformat()
        c.execute("INSERT INTO fetch_runs(started_at,outcome) VALUES(?, 'silence')", (old,))
    code, out, err = run_main("run-owner")
    assert code == 4, (out, err)
    with sqlite3.connect(sandbox.db) as c:
        c.row_factory = sqlite3.Row
        [run] = [dict(r) for r in c.execute("SELECT * FROM fetch_runs")]
        notices = {r["title"]: dict(r) for r in c.execute("SELECT * FROM notices")}
    assert (run["outcome"], run["exit_code"], run["total_notices"], run["purged_runs"]) == ("hit", 4, 2, 1)
    pc = json.loads(run["per_city_json"])
    assert pc["provo"]["hits"] == 1 and pc["draper"]["notices"] == 1 and len(pc) == 17
    assert notices["Provo Council"]["matched"] == 1 and "short-term rental" in notices["Provo Council"]["matched_terms"].lower()
    assert notices["Draper Council"]["matched"] == 0 and "Budget" in notices["Draper Council"]["text"]
    # second run: notices deduped by URL
    run_main("run-owner")
    with sqlite3.connect(sandbox.db) as c:
        assert c.execute("SELECT COUNT(*) FROM notices").fetchone()[0] == 2
        assert c.execute("SELECT COUNT(*) FROM fetch_runs").fetchone()[0] == 2


def test_outcome_and_late_minutes():
    assert [aw_store.outcome_for(c) for c in (0, 4, 3, 2, 1)] == ["silence", "hit", "partial", "failure", "failure"]
    t = dt.datetime(2026, 10, 13, 8, 47, tzinfo=aw_store.TZ)
    assert aw_store.late_minutes(t) == 26
    assert aw_store.late_minutes(t.replace(hour=8, minute=0)) == 0


# ---------------------------------------------------------------- /internal/status
def test_status_endpoint_auth(web, monkeypatch):
    from fastapi.testclient import TestClient
    client = TestClient(webapp.app)
    monkeypatch.delenv("AW_CLOUD", raising=False)
    assert client.get("/internal/status").status_code == 404            # box: route inert
    monkeypatch.setenv("AW_CLOUD", "1")
    monkeypatch.setenv("AW_INTERNAL_SECRET", "s3cret")
    assert client.get("/internal/status").status_code == 403
    assert client.get("/internal/status", headers={"x-aw-internal-secret": "nope"}).status_code == 403
    r = client.get("/internal/status", headers={"x-aw-internal-secret": "s3cret"})
    assert r.status_code == 200 and r.json()["latest_run"] is None and r.json()["in_progress"] is None
    with sqlite3.connect(web) as c:
        aw_store.record_run(c, started=dt.datetime.now(aw_store.TZ), code=3, total_notices=7,
                            per_city={"ch": {"hits": 0}}, errors="Sandy: timeout")
    j = client.get("/internal/status", headers={"x-aw-internal-secret": "s3cret"}).json()["latest_run"]
    assert (j["outcome"], j["total_notices"], j["errors"], j["per_city"]) == ("partial", 7, "Sandy: timeout", {"ch": {"hits": 0}})
