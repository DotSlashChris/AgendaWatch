"""Background /internal/run-owner (Cloudflare only): 202 + 409 while running, job does email/mark-sent,
records result, snapshots, calls back. No network/email: subprocess + send + callback are faked."""
import json
import threading
import types

import pytest
from fastapi.testclient import TestClient

from test_web_watch import webapp  # noqa: F401
from test_admin import web  # noqa: F401

H = {"x-aw-internal-secret": "s3cret"}


@pytest.fixture
def cloud(web, monkeypatch, sandbox):
    monkeypatch.setenv("AW_CLOUD", "1")
    monkeypatch.setenv("AW_INTERNAL_SECRET", "s3cret")
    monkeypatch.setattr(webapp, "_RUN", {"running": False, "started_at": None, "finished_at": None, "date": None, "result": None})
    monkeypatch.setattr(webapp.time, "sleep", lambda *_: None)
    root = sandbox.root
    (root / "web").mkdir(exist_ok=True)
    monkeypatch.setattr(webapp, "__file__", str(root / "web" / "app.py"))
    calls = types.SimpleNamespace(sub=[], sent=[], cb=[], gate=threading.Event(), code=0)

    def fake_run(argv, **kw):
        calls.sub.append(argv[2:])
        if argv[2] == "run-owner":
            calls.gate.wait(5)
            if calls.code == 4:
                (root / "outbox" / "email-1.json").write_text(json.dumps({"to": webapp.OWNER, "subject": "Agenda Watch: 1 item", "body": "b", "items": []}))
        return types.SimpleNamespace(returncode=calls.code if argv[2] == "run-owner" else 0, stdout="out", stderr="")

    monkeypatch.setattr(webapp._sp, "run", fake_run)
    monkeypatch.setattr(webapp, "send_owner_email", lambda to, s, b: calls.sent.append(s) or (True, "sent via Cloudflare send_email (msg-1)"))
    monkeypatch.setattr(webapp, "_callback", lambda path, payload: calls.cb.append((path, payload)) or "callback 200")
    return calls


def wait_done(timeout=5):
    for t in list(threading.enumerate()):
        if t.name == "run-owner":
            t.join(timeout)


def test_inert_on_box(web, monkeypatch):
    monkeypatch.delenv("AW_CLOUD", raising=False)
    assert TestClient(webapp.app).post("/internal/run-owner", headers=H).status_code == 404


def test_auth_required(cloud):
    assert TestClient(webapp.app).post("/internal/run-owner").status_code == 403


def test_202_then_409_while_running_then_done(cloud):
    c = TestClient(webapp.app)
    r = c.post("/internal/run-owner?date=2026-10-14", headers=H)
    assert r.status_code == 202 and r.json()["date"] == "2026-10-14"
    assert c.post("/internal/run-owner", headers=H).status_code == 409
    st = c.get("/internal/status", headers=H).json()
    assert st["in_progress"]["running"] is True and st["in_progress"]["date"] == "2026-10-14"
    cloud.gate.set(); wait_done()
    st = c.get("/internal/status", headers=H).json()
    assert st["in_progress"] is None and st["last_job"]["ok"] is True and st["last_job"]["exit_code"] == 0
    assert cloud.sent == [] and [p for p, _ in cloud.cb] == ["/run-done"]
    assert cloud.cb[0][1]["date"] == "2026-10-14"


def test_exit4_no_longer_emails_project_inbox_but_marks_owner_items_handled(cloud, sandbox):
    """Per-user alerts replaced the owner-only hit email: exit 4 with no per-user plan sends nothing,
    and the owner-watch items are still recorded in history (tomorrow counts only new hits)."""
    cloud.code = 4; cloud.gate.set()
    TestClient(webapp.app).post("/internal/run-owner", headers=H)
    wait_done()
    assert cloud.sent == []
    assert ["mark-sent", str(sandbox.root / "outbox" / "email-1.json"), "per-user"] in cloud.sub
    res = cloud.cb[0][1]
    assert res["ok"] is True and res["alerts"]["emails_sent"] == 0
    assert (sandbox.state / ".persist-now").exists()


def test_failure_reported(cloud):
    cloud.code = 2; cloud.gate.set()
    TestClient(webapp.app).post("/internal/run-owner", headers=H)
    wait_done()
    assert cloud.cb[0][1]["ok"] is False and cloud.sent == []
