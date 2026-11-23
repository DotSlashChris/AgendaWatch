"""Regression 2026-10-22: the owner's sign-in link must go to the owner's own address (it had been
redirected to the project inbox, so chris@ never got it). Mocked Resend HTTP; no real email."""
import json
import urllib.request

from fastapi.testclient import TestClient

from test_resend import KEY, http, web  # noqa: F401
from test_web_watch import webapp

OWNER = webapp.OWNER


def test_owner_signin_link_goes_to_owner_address_not_project_inbox(web, monkeypatch, http):
    monkeypatch.setenv("RESEND_API_KEY", KEY)
    monkeypatch.setenv("NOTIFY_TO", "inbox@example.invalid")
    c = TestClient(webapp.app)
    page = c.get("/signin").text
    csrf = page.split('name="csrf" value="')[1].split('"')[0]
    r = c.post("/signin", data={"email": OWNER, "csrf": csrf})
    assert r.status_code == 200 and "Check your inbox" in r.text and OWNER in r.text
    assert [x["data"]["to"] for x in http.calls] == [[OWNER]]
    # owner-bound notices still go to the project inbox
    webapp.send_owner_email(OWNER, "run notice", "b")
    assert http.calls[-1]["data"]["to"] == ["inbox@example.invalid"]


def test_resend_lookup_is_cloud_and_secret_gated(web, monkeypatch, http):
    c = TestClient(webapp.app)
    assert c.get("/internal/resend-lookup?id=abcd1234").status_code == 404
    monkeypatch.setenv("AW_CLOUD", "1")
    monkeypatch.setenv("AW_INTERNAL_SECRET", "sek")
    monkeypatch.setenv("RESEND_API_KEY", KEY)
    assert c.get("/internal/resend-lookup?id=abcd1234").status_code == 403
    h = {"x-aw-internal-secret": "sek"}
    assert c.get("/internal/resend-lookup?id=../x", headers=h).status_code == 400
    http.body = {"id": "abcd1234", "to": [OWNER], "subject": "s", "created_at": "t", "last_event": "delivered", "html": "x"}
    r = c.get("/internal/resend-lookup?id=abcd1234", headers=h)
    assert r.json() == {"id": "abcd1234", "to": [OWNER], "subject": "s", "created_at": "t", "last_event": "delivered"}
    assert KEY not in r.text
