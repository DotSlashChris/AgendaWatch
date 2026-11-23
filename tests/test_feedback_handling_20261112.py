"""Nov 12 2026: feedback status values, internal list/status/reply endpoints. No live email."""
import sqlite3

from fastapi.testclient import TestClient

import web.app as webapp
from test_admin import web, ADMIN, USER  # noqa: F401
from test_feedback import rows
from test_web_watch import signed_in_client


SECRET = "s3cret"
H = {"x-aw-internal-secret": SECRET}


def cloud_env(monkeypatch):
    monkeypatch.setenv("AW_CLOUD", "1")
    monkeypatch.setenv("AW_INTERNAL_SECRET", SECRET)
    monkeypatch.setenv("RESEND_API_KEY", "re_test_key")


def seed_item(web, **kw):
    """Insert one feedback row; defaults: new, with a typed reply address, anonymous."""
    d = {"created_at": "2026-11-12T12:00:00-07:00", "user_id": None, "email": "reader@example.com",
         "category": "Bug", "city": "", "link": "", "message": "The billing typo is still there.",
         "status": "new"}
    d.update(kw)
    with sqlite3.connect(web) as c:
        cur = c.execute(
            "INSERT INTO feedback(created_at,user_id,email,category,city,link,message,status) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (d["created_at"], d["user_id"], d["email"], d["category"], d["city"], d["link"],
             d["message"], d["status"]))
        return cur.lastrowid


def test_migration_keeps_existing_rows_and_widens_status(tmp_path, monkeypatch):
    db = tmp_path / "old.db"
    monkeypatch.setattr(webapp, "DB", db)
    with sqlite3.connect(db) as c:
        c.executescript("""
            CREATE TABLE schema_migrations(id TEXT PRIMARY KEY, applied_at TEXT NOT NULL);
            CREATE TABLE feedback(
              id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT NOT NULL,
              user_id TEXT, email TEXT NOT NULL DEFAULT '', category TEXT NOT NULL,
              city TEXT NOT NULL DEFAULT '', link TEXT NOT NULL DEFAULT '', message TEXT NOT NULL,
              status TEXT NOT NULL DEFAULT 'new' CHECK(status IN ('new','reviewed')));
            INSERT INTO feedback(created_at,email,category,message,status)
              VALUES('2026-10-22T12:26:05-06:00','a@x.com','Other','hello','new');
            INSERT INTO feedback(created_at,email,category,message,status)
              VALUES('2026-10-22T22:15:42-06:00','b@x.com','Bug','fixed','reviewed');
        """)
    webapp.init_db()
    with sqlite3.connect(db) as c:
        c.row_factory = sqlite3.Row
        got = [dict(r) for r in c.execute("SELECT id,status,email,reply_text,replied_at,owner_note FROM feedback ORDER BY id")]
        sql = c.execute("SELECT sql FROM sqlite_master WHERE name='feedback'").fetchone()[0]
        mig = c.execute("SELECT 1 FROM schema_migrations WHERE id='feedback_handling_20261112'").fetchone()
    assert mig
    assert "needs-owner" in sql and "replied" in sql
    assert [(r["id"], r["status"], r["email"], r["reply_text"], r["replied_at"], r["owner_note"]) for r in got] == [
        (1, "new", "a@x.com", None, None, None),
        (2, "reviewed", "b@x.com", None, None, None),
    ]
    # new statuses are now legal
    with sqlite3.connect(db) as c:
        c.execute("UPDATE feedback SET status='needs-owner' WHERE id=1")
        c.execute("UPDATE feedback SET status='replied', replied_at='t', reply_text='ok' WHERE id=2")
        assert [r[0] for r in c.execute("SELECT status FROM feedback ORDER BY id")] == ["needs-owner", "replied"]


def test_internal_requires_cloud_and_secret(web, monkeypatch):
    monkeypatch.delenv("AW_CLOUD", raising=False)
    c = TestClient(webapp.app)
    assert c.get("/internal/feedback").status_code == 404
    assert c.post("/internal/feedback/1/status", json={"status": "reviewed"}).status_code == 404
    assert c.post("/internal/feedback/1/reply", json={"text": "Hi"}).status_code == 404
    cloud_env(monkeypatch)
    assert c.get("/internal/feedback").status_code == 403
    assert c.get("/internal/feedback", headers={"x-aw-internal-secret": "nope"}).status_code == 403
    assert c.post("/internal/feedback/1/status", json={"status": "reviewed"}).status_code == 403
    assert c.post("/internal/feedback/1/reply", json={"text": "Hi"}).status_code == 403


def test_list_and_status_filter_and_reply_address(web, monkeypatch):
    cloud_env(monkeypatch)
    seed_item(web, status="new", email="anon@example.com", user_id=None, message="A")
    seed_item(web, status="needs-owner", email="other@example.com", user_id=USER, message="B")
    seed_item(web, status="reviewed", email="c@example.com", message="C")
    c = TestClient(webapp.app)
    all_ = c.get("/internal/feedback", headers=H)
    assert all_.status_code == 200
    body = all_.json()
    assert body["counts"]["total"] == 3
    assert body["counts"]["new"] == 1 and body["counts"]["needs-owner"] == 1 and body["counts"]["reviewed"] == 1
    by_id = {i["id"]: i for i in body["items"]}
    assert by_id[1]["signed_in"] is False and by_id[1]["reply_address"] == "anon@example.com"
    assert by_id[2]["signed_in"] is True and by_id[2]["reply_address"] == USER   # session account, not typed
    only = c.get("/internal/feedback", params={"status": "needs-owner"}, headers=H).json()
    assert [i["id"] for i in only["items"]] == [2]
    alias = c.get("/internal/feedback", params={"status": "needs-you"}, headers=H).json()
    assert [i["id"] for i in alias["items"]] == [2]
    assert alias["counts"]["needs-owner"] == 1 and "needs-you" not in alias["counts"]
    assert c.get("/internal/feedback", params={"status": "nope"}, headers=H).status_code == 400


def test_status_transitions_and_optional_note(web, monkeypatch):
    cloud_env(monkeypatch)
    fid = seed_item(web)
    c = TestClient(webapp.app)
    r = c.post(f"/internal/feedback/{fid}/status", json={"status": "needs-you", "note": "ask about pricing"}, headers=H)
    assert r.status_code == 200
    item = r.json()["item"]
    assert item["status"] == "needs-owner" and item["owner_note"] == "ask about pricing"  # alias stored as needs-owner
    r = c.post(f"/internal/feedback/{fid}/status", json={"status": "needs-owner"}, headers=H)
    assert r.status_code == 200 and r.json()["item"]["status"] == "needs-owner"
    r = c.post(f"/internal/feedback/{fid}/status", json={"status": "reviewed"}, headers=H)
    assert r.status_code == 200 and r.json()["item"]["status"] == "reviewed"
    assert r.json()["item"]["owner_note"] == "ask about pricing"           # note kept when omitted
    assert c.post("/internal/feedback/999/status", json={"status": "reviewed"}, headers=H).status_code == 404
    assert c.post(f"/internal/feedback/{fid}/status", json={"status": "replied"}, headers=H).status_code == 400


def test_reply_sends_once_and_is_idempotent(web, monkeypatch):
    cloud_env(monkeypatch)
    sent = []
    monkeypatch.setattr(webapp, "send_email", lambda to, sub, body, mail_from=None: sent.append((to, sub, body, mail_from)) or (True, "sent via Resend (id-1)"))
    fid = seed_item(web, email="reader@example.com")
    c = TestClient(webapp.app)
    r = c.post(f"/internal/feedback/{fid}/reply", json={"text": "Thanks, we fixed it."}, headers=H)
    assert r.status_code == 200 and r.json()["already"] is False
    item = r.json()["item"]
    assert item["status"] == "replied" and item["reply_text"] == "Thanks, we fixed it." and item["replied_at"]
    assert sent == [("reader@example.com", "Re: your Agenda Watch feedback", "Thanks, we fixed it.",
                     "Agenda Watch <hello@agendawatch.app>")]
    # second call: no second email, no change
    r2 = c.post(f"/internal/feedback/{fid}/reply", json={"text": "another"}, headers=H)
    assert r2.status_code == 200 and r2.json()["already"] is True
    assert sent == [("reader@example.com", "Re: your Agenda Watch feedback", "Thanks, we fixed it.",
                     "Agenda Watch <hello@agendawatch.app>")]
    assert rows(web)[0]["reply_text"] == "Thanks, we fixed it."
    # status endpoint will not overwrite a reply
    assert c.post(f"/internal/feedback/{fid}/status", json={"status": "reviewed"}, headers=H).status_code == 409


def test_reply_refuses_no_address_and_empty_text(web, monkeypatch):
    cloud_env(monkeypatch)
    sent = []
    monkeypatch.setattr(webapp, "send_email", lambda *a, **k: sent.append((a, k)) or (True, "sent"))
    fid = seed_item(web, email="", user_id=None)
    c = TestClient(webapp.app)
    r = c.post(f"/internal/feedback/{fid}/reply", json={"text": "Hi"}, headers=H)
    assert r.status_code == 409 and r.json()["error"] == "no reply address" and sent == []
    assert rows(web)[0]["status"] == "new"
    fid2 = seed_item(web, email="ok@example.com")
    r = c.post(f"/internal/feedback/{fid2}/reply", json={"text": "  "}, headers=H)
    assert r.status_code == 400 and sent == [] and rows(web)[1]["status"] == "new"


def test_reply_uses_session_account_and_resend_headers(web, monkeypatch):
    cloud_env(monkeypatch)
    captured = []
    def fake_send(to, subject, body, redirect_owner=True, budget_exempt=False, mail_from=None, **kwargs):
        captured.append({"to": to, "subject": subject, "body": body,
                         "redirect_owner": redirect_owner, "from": mail_from or webapp.RESEND_FROM,
                         "reply_to": webapp.RESEND_REPLY_TO})
        return True, "sent via Resend (abc)"
    monkeypatch.setattr(webapp, "_send_via_resend", fake_send)
    fid = seed_item(web, user_id=USER, email="other@example.com")
    r = TestClient(webapp.app).post(f"/internal/feedback/{fid}/reply", json={"text": "On it."}, headers=H)
    assert r.status_code == 200
    assert captured[0]["to"] == USER
    assert captured[0]["subject"] == "Re: your Agenda Watch feedback"
    assert captured[0]["redirect_owner"] is False
    assert captured[0]["from"] == "Agenda Watch <hello@agendawatch.app>"
    assert captured[0]["reply_to"] == "hello@agendawatch.app"
    assert webapp.RESEND_FROM == "Agenda Watch <alerts@agendawatch.app>"  # alerts/sign-in unchanged


def test_reply_respects_resend_reserve(web, monkeypatch):
    cloud_env(monkeypatch)
    sent = []
    monkeypatch.setattr(webapp, "send_email", lambda *a, **k: sent.append((a, k)) or (True, "sent"))
    monkeypatch.setattr(webapp, "FEEDBACK_RESEND_RESERVE", 10)
    monkeypatch.setattr(webapp.aw_guard, "remaining", lambda c, k: 10 if k == "cap_resend_sends" else 99)
    fid = seed_item(web)
    r = TestClient(webapp.app).post(f"/internal/feedback/{fid}/reply", json={"text": "Hi"}, headers=H)
    assert r.status_code == 429 and "reserve" in r.json()["error"] and sent == []
    assert rows(web)[0]["status"] == "new"


def test_admin_page_shows_new_statuses_and_reply_text(web):
    seed_item(web, status="needs-owner", message="Need a call on this.")
    seed_item(web, status="replied", email="z@example.com", message="Fixed now?", )
    with sqlite3.connect(web) as c:
        c.execute("UPDATE feedback SET reply_text=?, replied_at=?, owner_note=? WHERE status='replied'",
                  ("We shipped the fix.", "2026-11-12T13:00:00-07:00", "ok to close"))
    a, _, _ = signed_in_client(web, ADMIN)
    page = a.get("/admin").text
    sec = page[page.index('id="feedback"'):]
    assert "Needs you" in sec and "Need a call on this." in sec
    assert "needs-owner" not in sec  # stored value is not shown raw
    assert "replied" in sec and "We shipped the fix." in sec and "Owner note: ok to close" in sec
    assert "1 needs you" in sec
