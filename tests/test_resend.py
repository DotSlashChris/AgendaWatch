"""Resend outbound email (mocked HTTP only — nothing is ever sent)."""
import io
import json
import urllib.error
import urllib.request

import pytest
from fastapi.testclient import TestClient

from web import app as webapp
OWNER = webapp.OWNER

KEY = "re_FAKE_test_key_123456"


@pytest.fixture
def web(sandbox, monkeypatch):
    db = sandbox.root / "data" / "web.db"
    monkeypatch.setattr(webapp, "DB", db)
    monkeypatch.setattr(webapp, "OUTBOX", sandbox.outbox / "magic")
    monkeypatch.setattr(webapp, "HISTORY_JSON", sandbox.state / "sent_history.json")
    monkeypatch.setattr(webapp, "_signin_sends", {})
    webapp.init_db()
    return db


class FakeHTTP:
    def __init__(self, status=200, body=None):
        self.calls, self.status, self.body = [], status, body or {"id": "em_123"}

    def __call__(self, req, timeout=None):
        self.calls.append({"url": req.full_url, "headers": dict(req.header_items()),
                           "data": json.loads(req.data) if req.data else None, "timeout": timeout})
        if self.status != 200:
            raise urllib.error.HTTPError(req.full_url, self.status, "err", {},
                                         io.BytesIO(json.dumps(self.body).encode()))
        r = io.BytesIO(json.dumps(self.body).encode())
        r.__enter__ = lambda *a: r
        r.__exit__ = lambda *a: None
        return r


class Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


@pytest.fixture
def http(monkeypatch):
    fake = FakeHTTP()
    orig = fake.__call__

    def call(req, timeout=None):
        out = orig(req, timeout)
        return Resp(out.getvalue())
    monkeypatch.setattr(urllib.request, "urlopen", call)
    return fake


def resend_on(monkeypatch):
    monkeypatch.setenv("RESEND_API_KEY", KEY)
    monkeypatch.setenv("NOTIFY_TO", "inbox@example.invalid")


def test_provider_flag(monkeypatch):
    monkeypatch.delenv("RESEND_API_KEY", raising=False)
    monkeypatch.delenv("AW_EMAIL_RELAY_URL", raising=False)
    assert webapp.email_provider() == "outbox"
    monkeypatch.setenv("AW_EMAIL_RELAY_URL", "https://relay.invalid/__aw/email")
    assert webapp.email_provider() == "cloudflare"
    monkeypatch.setenv("RESEND_API_KEY", KEY)
    assert webapp.email_provider() == "resend"


def test_healthz_reports_provider_without_secret(web, monkeypatch):
    resend_on(monkeypatch)
    h = TestClient(webapp.app).get("/healthz").json()
    assert h["email_provider"] == "resend" and KEY not in json.dumps(h)


def test_signin_any_address_via_resend(web, monkeypatch, http):
    resend_on(monkeypatch)
    link, status = webapp.issue_magic_link("someone@example.invalid")
    assert status.startswith("sent via Resend") and len(http.calls) == 1
    c = http.calls[0]
    assert c["url"] == "https://api.resend.com/emails"
    assert c["data"]["from"] == "Agenda Watch <alerts@agendawatch.app>"
    assert c["data"]["reply_to"] == "hello@agendawatch.app"
    assert c["data"]["to"] == ["someone@example.invalid"] and link in c["data"]["text"]
    assert c["headers"]["Authorization"] == f"Bearer {KEY}"


def test_owner_mail_goes_to_project_inbox(web, monkeypatch, http):
    resend_on(monkeypatch)
    sent, how = webapp.send_owner_email(OWNER, "s", "b")
    assert sent and http.calls[0]["data"]["to"] == ["inbox@example.invalid"]


def test_failure_reported_once_no_retry_no_key_leak(web, monkeypatch, http, caplog):
    resend_on(monkeypatch)
    http.status, http.body = 403, {"message": f"bad key {KEY}"}
    link, status = webapp.issue_magic_link("someone@example.invalid")
    assert status.startswith("send FAILED via Resend: HTTP 403") and KEY not in status
    assert len(http.calls) == 1
    assert KEY not in caplog.text and link not in caplog.text


def test_rate_limit_per_address(web, monkeypatch, http):
    resend_on(monkeypatch)
    for _ in range(3):
        webapp.issue_magic_link("a@example.invalid")
    _, status = webapp.issue_magic_link("a@example.invalid")
    assert status.startswith("not sent") and len(http.calls) == 3


def test_without_resend_cloudflare_path_unchanged(web, monkeypatch, http):
    monkeypatch.delenv("RESEND_API_KEY", raising=False)
    monkeypatch.setenv("AW_EMAIL_RELAY_URL", "https://relay.invalid/__aw/email")
    monkeypatch.setenv("AW_INTERNAL_SECRET", "s")
    _, status = webapp.issue_magic_link("someone@example.invalid")
    assert status.startswith("stored only") and http.calls == []          # non-owner: no mail
    http.body = {"messageId": "m1"}
    _, status = webapp.issue_magic_link(OWNER)
    assert status.startswith("sent via Cloudflare") and http.calls[0]["url"].startswith("https://relay.invalid")
    assert http.calls[0]["data"]["to"] == OWNER


def test_test_signin_route_is_cloud_and_secret_gated(web, monkeypatch, http):
    c = TestClient(webapp.app)
    assert c.post("/internal/test-signin", json={"email": "a@example.invalid"}).status_code == 404
    monkeypatch.setenv("AW_CLOUD", "1")
    monkeypatch.setenv("AW_INTERNAL_SECRET", "sek")
    assert c.post("/internal/test-signin", json={"email": "a@example.invalid"}).status_code == 403
    resend_on(monkeypatch)
    r = c.post("/internal/test-signin", json={"email": "a@example.invalid"},
               headers={"x-aw-internal-secret": "sek"}).json()
    assert r["provider"] == "resend" and r["sent"] and "token=" not in json.dumps(r)


def test_email_check_readonly_and_last_email_recorded(web, monkeypatch, http):
    monkeypatch.setenv("AW_CLOUD", "1")
    monkeypatch.setenv("AW_INTERNAL_SECRET", "sek")
    resend_on(monkeypatch)
    http.status, http.body = 403, {"message": "The agendawatch.app domain is not verified"}
    webapp.issue_magic_link("x@example.invalid")
    http.status, http.body = 200, {"data": [{"name": "agendawatch.app", "status": "verified", "region": "us-east-1"}]}
    r = TestClient(webapp.app).get("/internal/email-check", headers={"x-aw-internal-secret": "sek"}).json()
    assert r["domains_http"] == 200 and r["domains"][0]["status"] == "verified"
    assert r["last_email"]["http"] == 403 and "not verified" in r["last_email"]["detail"]
    assert "x@example.invalid" not in json.dumps(r) and KEY not in json.dumps(r)
    assert http.calls[-1]["url"] == "https://api.resend.com/domains" and http.calls[-1]["data"] is None
