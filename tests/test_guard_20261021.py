"""Cost and safety guardrails (2026-10-21): settings validation, partial save, budgets, run lock, kill
switch, frugal snapshot loop. Throwaway sandbox DBs, fake PMN, mocked Resend; no network, no real email."""
import importlib.util
import json
import sqlite3
import types

import pytest
from fastapi.testclient import TestClient

import aw_guard
import aw_store
from conftest import OWNER
from test_admin import ADMIN, web  # noqa: F401
from test_daily_run import SOON, STR_AGENDA
from test_user_alerts import A, B, H, add_watch, cloud, db, STR_ITEM  # noqa: F401
from test_web_watch import signed_in_client, webapp


def mem():
    c = sqlite3.connect(":memory:")
    aw_guard.ensure_schema(c)
    return c


# ---------------------------------------------------------------- settings
def test_defaults_fetch_on_and_frugal_interval():
    v = aw_guard.get_all(mem())
    assert v["fetch_enabled"] == 1 and v["snapshot_interval_s"] == 600
    assert (v["cap_pmn_requests"], v["cap_alert_emails"], v["cap_resend_sends"],
            v["cap_snapshot_uploads"], v["cap_container_wakes"]) == (3000, 50, 100, 60, 60)


@pytest.mark.parametrize("raw,ok", [("60", True), ("3600", True), ("600", True), ("59", False), ("3601", False),
                                    ("abc", False), ("", False), ("10.5", False), ("-1", False)])
def test_interval_validation(raw, ok):
    v, err = aw_guard.parse_value("snapshot_interval_s", raw)
    assert (err is None) == ok
    if ok:
        assert v == int(raw)


def test_partial_save_keeps_other_values_and_invalid_saves_nothing():
    c = mem()
    aw_guard.update(c, {"cap_alert_emails": "80", "snapshot_interval_s": "900"})
    vals, errs = aw_guard.update(c, {"fetch_enabled": "0"})
    assert not errs and vals["fetch_enabled"] == 0 and vals["cap_alert_emails"] == 80 and vals["snapshot_interval_s"] == 900
    vals, errs = aw_guard.update(c, {"snapshot_interval_s": "30", "cap_alert_emails": "70"})
    assert errs and vals["snapshot_interval_s"] == 900 and vals["cap_alert_emails"] == 80  # all-or-nothing
    c.execute("UPDATE settings SET value='garbage' WHERE key='guard_cap_alert_emails'")
    assert aw_guard.get(c, "cap_alert_emails") == 50  # bad stored value -> default


# ---------------------------------------------------------------- lock
def test_run_lock_exclusive_and_stale_safe():
    c = mem()
    assert aw_guard.acquire_lock(c, "run-owner:2026-10-21", now=1000)
    assert not aw_guard.acquire_lock(c, "test-alert", now=1000 + 60)
    assert not aw_guard.acquire_lock(c, "test-alert", now=1000 + 1799)
    aw_guard.refresh_lock(c, "run-owner:2026-10-21", now=2000)       # heartbeat keeps a live run's lock
    assert not aw_guard.acquire_lock(c, "test-alert", now=2000 + 1799)
    assert aw_guard.acquire_lock(c, "test-alert", now=2000 + 1801)    # dead holder: taken over after 30 min
    assert aw_guard.lock_info(c)["holder"] == "test-alert"
    aw_guard.release_lock(c, "someone-else")
    assert aw_guard.lock_info(c)
    aw_guard.release_lock(c, "test-alert")
    assert aw_guard.lock_info(c) is None


def test_trip_and_notice_once_per_day():
    c = mem()
    assert aw_guard.trip(c, "cap_alert_emails", "x", day="2026-10-21")
    assert not aw_guard.trip(c, "cap_alert_emails", "again", day="2026-10-21")
    assert aw_guard.claim_notice(c, "cap_alert_emails", day="2026-10-21")
    assert not aw_guard.claim_notice(c, "cap_alert_emails", day="2026-10-21")
    assert aw_guard.trip(c, "cap_alert_emails", "next day", day="2026-10-22")


# ---------------------------------------------------------------- PMN budget in the daily job
def test_pmn_budget_stops_fetch_and_withholds_alerts(pmn, owner_watch, run_main, sandbox):
    import agenda_watch as aw
    owner_watch(cities={"provo": (1, "")})
    pmn.add_notice("Provo", SOON, STR_AGENDA, title="Provo Council")
    with sqlite3.connect(sandbox.db) as c:
        aw_guard.ensure_schema(c)
        aw_guard.add(c, "pmn_requests", 2995)
    calls = {"n": 0}
    real_get = aw.S.get

    def counting(*a, **k):
        calls["n"] += 1
        return real_get(*a, **k)
    import conftest  # noqa: F401
    aw.S.get = counting
    try:
        code, out, err = run_main("run-owner")
    finally:
        aw.S.get = real_get
    assert calls["n"] == 5                       # exactly the 5 requests left in today's budget
    assert code not in (0, 4), (code, out, err)  # failed run -> no user alerts from it
    assert "budget" in (out + err).lower()
    with sqlite3.connect(sandbox.db) as c:
        assert aw_guard.usage(c)["pmn_requests"] == 3000
        assert [t["key"] for t in aw_guard.trips(c)] == ["cap_pmn_requests"]
    plan = sandbox.state / "user_alert_plan.json"
    if plan.exists():
        assert json.loads(plan.read_text()).get("mode") != "send"


def test_pmn_budget_counts_normal_runs(pmn, owner_watch, run_main, sandbox):
    owner_watch(cities={"provo": (1, "")})
    pmn.add_notice("Provo", SOON, STR_AGENDA, title="Provo Council")
    code, out, err = run_main("run-owner")
    assert code == 4, (out, err)
    with sqlite3.connect(sandbox.db) as c:
        used = aw_guard.usage(c)["pmn_requests"]
        assert 0 < used < 3000 and not aw_guard.trips(c)


# ---------------------------------------------------------------- alert + Resend budgets
ITEM = {"fingerprint": "fp1", "city": "Cottonwood Heights", "city_key": "ch", "meeting_date": "2026-10-12",
        "meeting_datetime": "October 12, 2026 06:00 PM", "item_text": STR_ITEM, "matched_terms": ["short-term rental"],
        "link": "https://x/1.pdf", "notice_url": "https://x/n"}


def plan_for(*emails):
    return {"mode": "send", "held_cities_off": {},
            "users": [{"email": e, "items": [dict(ITEM, fingerprint=f"fp-{e}")]} for e in emails]}


def test_alert_batch_held_whole_when_budget_short(db, monkeypatch):
    for e in (A, B):
        add_watch(db.db, e, {"ch": (1, "")})
    notices = []
    monkeypatch.setattr(webapp, "send_owner_email", lambda to, s, b, budget_exempt=False: notices.append((to, s, budget_exempt)) or (True, "sent"))
    with webapp.db() as c:
        aw_guard.update(c, {"cap_alert_emails": "1"})
    out = webapp.deliver_user_alerts(plan_for(A, B))
    assert out["mode"] == "held_budget" and out["emails_sent"] == 0
    assert db.calls == []                                   # nobody emailed: no partial batch
    assert len(notices) == 1 and notices[0][0] == OWNER and notices[0][2] is True
    out2 = webapp.deliver_user_alerts(plan_for(A, B))        # same day: still held, no second notice
    assert out2["mode"] == "held_budget" and db.calls == [] and len(notices) == 1
    with webapp.db() as c:
        aw_guard.update(c, {"cap_alert_emails": "5"})
    out3 = webapp.deliver_user_alerts(plan_for(A, B))
    assert out3["emails_sent"] == 2 and len(db.calls) == 2
    with webapp.db() as c:
        assert aw_guard.usage(c)["alert_emails"] == 2


def test_resend_cap_blocks_sends_and_notifies_owner_once(sandbox, monkeypatch, web):
    monkeypatch.setenv("RESEND_API_KEY", "re_test_dummy")
    monkeypatch.setenv("NOTIFY_TO", "project@example.invalid")
    sent = []

    class R:
        def __init__(self, body): self.body = body
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self): return b'{"id": "re_x"}'
    import urllib.request
    monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout=None: sent.append(json.loads(req.data)) or R(b""))
    with webapp.db() as c:
        aw_guard.update(c, {"cap_resend_sends": "10"})
        aw_guard.add(c, "resend_sends", 9)
    ok, how = webapp._send_via_resend("test+u@example.invalid", "s", "b")
    assert ok and len(sent) == 1
    ok, how = webapp._send_via_resend("test+u@example.invalid", "s", "b")
    assert not ok and "limit" in how
    ok, how = webapp._send_via_resend("test+v@example.invalid", "s", "b")
    assert not ok
    to = [m["to"][0] for m in sent]
    assert to == ["test+u@example.invalid", "project@example.invalid"]   # one owner notice, nothing else
    assert "limit reached" in sent[1]["subject"]


# ---------------------------------------------------------------- kill switch + lock in the app
def test_kill_switch_refuses_run_owner(cloud, monkeypatch):
    with webapp.db() as c:
        aw_guard.update(c, {"fetch_enabled": "0"})
    started = []
    monkeypatch.setattr(webapp._th, "Thread", lambda **kw: types.SimpleNamespace(start=lambda: started.append(1)))
    r = TestClient(webapp.app).post("/internal/run-owner", headers=H)
    assert r.status_code == 423 and r.json()["disabled"] is True and started == []
    st = TestClient(webapp.app).get("/internal/status", headers=H).json()
    assert st["fetch_enabled"] is False


def test_run_lock_blocks_overlapping_runs(cloud, monkeypatch):
    started = []
    monkeypatch.setattr(webapp._th, "Thread", lambda **kw: types.SimpleNamespace(start=lambda: started.append(1)))
    webapp._RUN.update(running=False)
    with webapp.db() as c:
        assert aw_guard.acquire_lock(c, "test-alert")
    r = TestClient(webapp.app).post("/internal/run-owner", headers=H)
    assert r.status_code == 503 and r.json()["error"] == "run lock held" and started == []
    with webapp.db() as c:
        aw_guard.release_lock(c, "test-alert")
    r = TestClient(webapp.app).post("/internal/run-owner", headers=H)
    assert r.status_code == 202 and started == [1]
    with webapp.db() as c:
        assert aw_guard.lock_info(c)["holder"].startswith("run-owner:")
        assert not aw_guard.acquire_lock(c, "test-alert")
    webapp._RUN.update(running=False)


def test_job_releases_lock(cloud, monkeypatch, sandbox):
    monkeypatch.setattr(webapp.time, "sleep", lambda *_: None)
    monkeypatch.setattr(webapp, "_callback", lambda path, payload: "callback 200")
    monkeypatch.setattr(webapp._sp, "run", lambda argv, **kw: types.SimpleNamespace(returncode=0, stdout="", stderr=""))
    with webapp.db() as c:
        aw_guard.acquire_lock(c, "run-owner:2026-10-07")
    webapp._RUN.update(running=True)
    webapp._run_owner_job("2026-10-07")
    with webapp.db() as c:
        assert aw_guard.lock_info(c) is None


# ---------------------------------------------------------------- admin section
def test_admin_cost_safety_section_and_partial_save(web):
    client, _, _ = signed_in_client(web, ADMIN)
    page = client.get("/admin").text
    assert 'id="cost-safety"' in page and "Cost and safety" in page
    assert 'name="snapshot_interval_s"' in page and 'value="600"' in page
    assert "Daily fetch: <strong>ON" in page
    csrf = page.split('name="csrf" value="')[1].split('"')[0]
    r = client.post("/admin/cost-safety", data={"csrf": csrf, "snapshot_interval_s": "1200"}, follow_redirects=False)
    assert r.status_code == 303
    with webapp.db() as c:
        v = aw_guard.get_all(c)
    assert v["snapshot_interval_s"] == 1200 and v["fetch_enabled"] == 1 and v["cap_alert_emails"] == 50
    r = client.post("/admin/cost-safety", data={"csrf": csrf, "snapshot_interval_s": "5", "cap_alert_emails": "70"})
    assert r.status_code == 400 and "between 60 and 3600" in r.text and "Nothing was saved" in r.text
    with webapp.db() as c:
        v = aw_guard.get_all(c)
    assert v["snapshot_interval_s"] == 1200 and v["cap_alert_emails"] == 50
    r = client.post("/admin/cost-safety", data={"csrf": "wrong", "fetch_enabled": "0"})
    assert r.status_code == 400
    with webapp.db() as c:
        assert aw_guard.get(c, "fetch_enabled") == 1


def test_admin_cost_safety_admin_only(web):
    client, _, _ = signed_in_client(web, "test+user@example.invalid")
    assert client.post("/admin/cost-safety", data={"fetch_enabled": "0"}).status_code == 404
    assert TestClient(webapp.app).post("/admin/cost-safety", data={"fetch_enabled": "0"}).status_code == 404


def test_webhook_triggers_snapshot_flag(web, monkeypatch, sandbox):
    monkeypatch.setattr(webapp, "ROOT", sandbox.root)
    flag = sandbox.state / ".persist-now"
    r = TestClient(webapp.app).post("/stripe/webhook", content=b"{}", headers={"stripe-signature": "bad"})
    assert r.status_code >= 400 and not flag.exists()   # rejected webhook: nothing changed, no snapshot


# ---------------------------------------------------------------- persist.py loop decisions
def load_persist():
    spec = importlib.util.spec_from_file_location("persist", webapp.ROOT / "cloudflare" / "persist.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def test_snapshotter_debounce_flag_interval_final():
    p = load_persist()
    t = {"now": 0.0}
    sn = p.Snapshotter(600, clock=lambda: t["now"])
    sn.saved_sig, sn.last_digest = ("a",), "d0"
    assert sn.due(("a",), False, False) is None             # nothing written: no work
    t["now"] = 5; assert sn.due(("b",), False, False) is None  # write seen: wait for the 30 s debounce
    t["now"] = 34; assert sn.due(("b",), False, False) is None
    t["now"] = 36; assert sn.due(("b",), False, False) == "write"
    ups = []
    up = lambda blob, final=False: ups.append(final) or {"ok": True, "uploads_today": 1, "cap": 60}
    assert sn.attempt("write", ("b",), lambda: b"x", lambda b: "d1", up) == "uploaded"
    t["now"] = 40; assert sn.due(("b",), False, False) is None
    t["now"] = 41; assert sn.due(("b",), True, False) == "flag"      # daily run / alerts / webhook: now
    assert sn.attempt("flag", ("b",), lambda: b"x", lambda b: "d1", up) == "unchanged"  # same content: no upload
    t["now"] = 700; assert sn.due(("b",), False, False) is None      # interval backstop, unchanged files: no build
    assert sn.due(("c",), False, True) == "final"                     # SIGTERM
    sn.attempt("final", ("c",), lambda: b"y", lambda b: "d2", up)
    assert ups == [False, True]


def test_snapshotter_throttles_over_budget():
    p = load_persist()
    t = {"now": 0.0}
    sn = p.Snapshotter(600, clock=lambda: t["now"])
    sn.saved_sig = ("a",)
    up = lambda blob, final=False: {"ok": True, "uploads_today": 60, "cap": 60}
    sn.attempt("write", ("b",), lambda: b"x", lambda b: "d1", up)
    t["now"] = 100
    assert sn.due(("c",), False, False) is None                       # throttled 30 min
    t["now"] = 100 + 1800
    assert sn.due(("c",), False, False) == "write"
    assert sn.due(("c",), False, True) == "final"                     # final always allowed


def test_read_interval_clamps(sandbox, monkeypatch):
    p = load_persist()
    dbp = sandbox.root / "data" / "agenda_watch.db"
    monkeypatch.setattr(p, "DB", dbp)
    monkeypatch.delenv("AW_PERSIST_INTERVAL", raising=False)
    assert p.read_interval() == 600                                    # no DB yet: default
    with sqlite3.connect(dbp) as c:
        aw_guard.ensure_schema(c)
        aw_guard.update(c, {"snapshot_interval_s": "1800"})
    assert p.read_interval() == 1800
    with sqlite3.connect(dbp) as c:
        c.execute("UPDATE settings SET value='5' WHERE key='guard_snapshot_interval_s'")
    assert p.read_interval() == 60
