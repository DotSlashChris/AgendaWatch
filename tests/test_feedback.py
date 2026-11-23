"""Feedback form + admin feedback section. Throwaway DB; no network/email."""
import re
import sqlite3

import pytest
from fastapi.testclient import TestClient

import aw_store
from test_web_watch import seed, signed_in_client, webapp  # noqa: F401
from test_admin import web, ADMIN, USER  # noqa: F401  (fixture)


@pytest.fixture(autouse=True)
def _reset_rate(monkeypatch):
    monkeypatch.setattr(webapp, "FEEDBACK_MIN_AGE_S", 0)      # tests submit instantly; time-trap has its own tests
    monkeypatch.delenv("AW_EMAIL_RELAY_URL", raising=False)
    monkeypatch.delenv("TURNSTILE_SITE_KEY", raising=False)
    monkeypatch.delenv("TURNSTILE_SECRET", raising=False)


class Tok(str):
    """csrf string that also carries the form's time-trap token (.ft)."""
    ft = ""


def tok(c, page=None):
    page = page or c.get("/feedback").text
    t = Tok(re.search(r'name="csrf" value="([^"]+)"', page).group(1))
    t.ft = re.search(r'name="ft" value="([^"]+)"', page).group(1)
    return t


def fb(web, email=USER):
    """Signed-in feedback client with a fresh form token."""
    c, _, _ = signed_in_client(web, email)
    page = c.get("/feedback")
    assert page.status_code == 200
    return c, tok(c, page.text), page.text


def rows(db):
    with sqlite3.connect(db) as c:
        c.row_factory = sqlite3.Row
        return [dict(r) for r in c.execute("SELECT * FROM feedback ORDER BY id")]


def ok_form(csrf, **kw):
    d = {"csrf": csrf, "ft": getattr(csrf, "ft", ""), "category": "Missed alert", "message": "Sandy council STR item was not flagged.",
         "city": "sandy", "link": "https://www.utah.gov/pmn/sitemap/notice/1.html", "email": "", "website": ""}
    d.update(kw)
    return d


def test_nav_feedback_only_when_signed_in(web):
    home = TestClient(webapp.app).get("/").text
    assert 'id="nav-feedback"' not in home and 'href="/feedback"' not in home
    _, _, page = signed_in_client(web, USER)
    assert 'id="nav-feedback"' in page and 'href="/feedback"' in page


def test_logged_out_get_redirects_to_signin(web):
    r = TestClient(webapp.app).get("/feedback", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/signin"


def test_logged_out_post_redirects_and_stores_nothing(web):
    r = TestClient(webapp.app).post("/feedback", data=ok_form("x"), follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/signin"
    assert rows(web) == []


def test_form_has_no_email_field_and_stores_account(web):
    c, csrf, page = fb(web)
    assert 'name="email"' not in page and "fb-email" not in page and "Reply-to" not in page
    assert "Your email" not in page
    assert f"Signed in as <strong>{USER}</strong>" in page
    assert "We'll reply to this address." in page
    r = c.post("/feedback", data=ok_form(csrf, email="owner@example.com", city=""))
    assert r.status_code == 200 and "Thanks for the feedback" in r.text
    [f] = rows(web)
    assert (f["user_id"], f["email"], f["category"], f["city"], f["status"]) == (USER, USER, "Missed alert", "", "new")


@pytest.mark.parametrize("bad,msg", [
    ({"category": "Spam"}, "Pick a category"),
    ({"message": "   "}, "write a message"),
    ({"message": "x" * 2001}, "under 2000"),
    ({"city": "atlantis"}, "Pick a city"),
    ({"link": "javascript:alert(1)"}, "http"),
])
def test_validation(web, bad, msg):
    c, csrf, _ = fb(web)
    r = c.post("/feedback", data=ok_form(csrf, **bad))
    assert r.status_code == 400 and msg in r.text
    assert rows(web) == []


def test_csrf_required(web):
    c, _, _ = fb(web)
    r = c.post("/feedback", data=ok_form("wrong"))
    assert r.status_code == 400 and rows(web) == []


def test_honeypot_silently_drops(web):
    c, csrf, _ = fb(web)
    r = c.post("/feedback", data=ok_form(csrf, website="http://spam.example"))
    assert r.status_code == 200 and "Thanks" in r.text
    assert rows(web) == []


def test_rate_limit_per_ip(web):
    c, csrf, _ = fb(web)
    for _ in range(5):
        assert c.post("/feedback", data=ok_form(csrf)).status_code == 200
    r = c.post("/feedback", data=ok_form(csrf))
    assert r.status_code == 429 and len(rows(web)) == 5
    # a different client IP is not affected
    assert c.post("/feedback", data=ok_form(csrf), headers={"cf-connecting-ip": "203.0.113.9"}).status_code == 200


def test_no_email_locally_and_one_capped_notice_on_cloud(web, monkeypatch):
    sent = []
    monkeypatch.setattr(webapp, "send_owner_email", lambda to, s, b: sent.append((to, s, b)) or (True, "sent"))
    c, csrf, _ = fb(web)
    c.post("/feedback", data=ok_form(csrf))
    assert sent == []                                    # box/local: never emails
    monkeypatch.setenv("AW_EMAIL_RELAY_URL", "https://relay.invalid/__aw/email")
    monkeypatch.setattr(webapp, "FEEDBACK_EMAIL_DAILY_CAP", 2)
    for ip in ("1.1.1.1", "2.2.2.2", "3.3.3.3"):
        c.post("/feedback", data=ok_form(csrf), headers={"cf-connecting-ip": ip})
    assert len(sent) == 2 and all(to == webapp.OWNER for to, _, _ in sent)  # relay delivers to project inbox
    assert "Missed alert" in sent[0][1] and len(rows(web)) == 4


def test_admin_feedback_list_filter_and_review(web):
    c, csrf, _ = fb(web)
    c.post("/feedback", data=ok_form(csrf, category="Bug", message="Billing page typo"))
    c.post("/feedback", data=ok_form(csrf, category="Other", message="Love it"))
    # non-admin / signed out
    assert TestClient(webapp.app).post("/admin/feedback/1/reviewed").status_code == 404
    u, ucsrf, _ = signed_in_client(web, USER)
    assert u.post("/admin/feedback/1/reviewed", data={"csrf": ucsrf}).status_code == 404
    a, acsrf, _ = signed_in_client(web, ADMIN)
    page = a.get("/admin").text
    assert page.index("Love it") < page.index("Billing page typo")      # newest first
    assert "2 new" in page
    only_bug = a.get("/admin", params={"fb_category": "Bug"}).text
    assert "Billing page typo" in only_bug and "Love it" not in only_bug
    r = a.post("/admin/feedback/1/reviewed", data={"csrf": acsrf}, follow_redirects=False)
    assert r.status_code == 303
    assert [f["status"] for f in rows(web)] == ["reviewed", "new"]


def test_purge_never_touches_feedback(web):
    c, csrf, _ = fb(web)
    c.post("/feedback", data=ok_form(csrf))
    with sqlite3.connect(web) as con:
        con.execute("UPDATE feedback SET created_at='2020-01-01T00:00:00-07:00'")
        aw_store.purge(con)
    assert len(rows(web)) == 1


# ---------------- anti-spam layers (2026-10-22) ----------------
SPAM = ("Dear Sir/Madam\r\n\r\nSubmit agendawatch.app in Google's Search Index and have it appear in Google "
        "search results!\r\n\r\nAdd agendawatch.app today: searchregister.live")


_n = iter(range(1, 10_000))


def fresh_ip():
    n = next(_n)
    return {"cf-connecting-ip": f"10.1.{n // 250}.{n % 250 + 1}"}


def spam_rows(db):
    with sqlite3.connect(db) as c:
        c.row_factory = sqlite3.Row
        return [dict(r) for r in c.execute("SELECT * FROM feedback_spam ORDER BY id")]


def usage(db):
    with sqlite3.connect(db) as c:
        c.row_factory = sqlite3.Row
        return {r["key"]: r["count"] for r in c.execute("SELECT key, count FROM guard_usage")}


@pytest.fixture
def cloud(monkeypatch):
    """Cloudflare-like: relay set, owner notice captured."""
    sent = []
    monkeypatch.setenv("AW_EMAIL_RELAY_URL", "https://relay.invalid/__aw/email")
    monkeypatch.setattr(webapp, "send_owner_email", lambda to, s, b: sent.append((to, s, b)) or (True, "sent"))
    return sent


def test_actual_spam_text_is_dropped_silently(web, cloud):
    c, csrf, _ = fb(web)
    r = c.post("/feedback", data=ok_form(csrf, category="Suggest a source to monitor", city="draper",
                                         message=SPAM, link="", email="domains@search-agendawatch.app"))
    assert r.status_code == 200 and "Thanks for the feedback" in r.text      # bot sees a normal success page
    assert rows(web) == [] and cloud == []                                    # not stored, owner not emailed
    [d] = spam_rows(web)
    assert d["reason"] == "spam_phrase" and d["snippet"].startswith("Dear Sir/Madam")
    u = usage(web)
    assert u["fb_dropped"] == 1 and u["fb_drop_spam_phrase"] == 1 and "fb_accepted" not in u


def test_honeypot_counts_and_no_email(web, cloud):
    c, csrf, _ = fb(web)
    r = c.post("/feedback", data=ok_form(csrf, website="x"))
    assert r.status_code == 200 and "Thanks" in r.text
    assert rows(web) == [] and cloud == [] and usage(web)["fb_drop_honeypot"] == 1


def test_time_trap_too_fast_missing_forged_expired(web, cloud, monkeypatch):
    c, csrf, _ = fb(web)
    monkeypatch.setattr(webapp, "FEEDBACK_MIN_AGE_S", 3)
    r = c.post("/feedback", data=ok_form(csrf), headers=fresh_ip())                              # submitted instantly
    assert r.status_code == 200 and rows(web) == [] and usage(web)["fb_drop_too_fast"] == 1
    monkeypatch.setattr(webapp, "FEEDBACK_MIN_AGE_S", 0)
    for bad in ("", "garbage", csrf.ft[:-3] + "xyz"):
        r = c.post("/feedback", data=ok_form(csrf, ft=bad), headers=fresh_ip())
        assert r.status_code == 200 and "Thanks" in r.text
    assert rows(web) == [] and usage(web)["fb_drop_no_token"] == 3 and cloud == []
    # token from another visitor's session is rejected too
    other, ocsrf, _ = fb(web, ADMIN)
    assert other.post("/feedback", data=ok_form(ocsrf, ft=csrf.ft), headers=fresh_ip()).status_code == 200
    assert rows(web) == [] and usage(web)["fb_drop_no_token"] == 4
    # expired form: honest user gets a visible "timed out" error and can resend (nothing dropped/stored)
    monkeypatch.setattr(webapp, "FEEDBACK_MAX_AGE_S", -1)
    r = c.post("/feedback", data=ok_form(csrf), headers=fresh_ip())
    assert r.status_code == 400 and "timed out" in r.text and rows(web) == []
    monkeypatch.setattr(webapp, "FEEDBACK_MAX_AGE_S", 3600)
    assert c.post("/feedback", data=ok_form(csrf), headers=fresh_ip()).status_code == 200 and len(rows(web)) == 1


def test_time_trap_boundary(web, monkeypatch):
    """Form issued at T: 1 s later is too fast (min 3), 4 s later is accepted."""
    import time as _t

    class Shim:
        offset = 0.0
        def __getattr__(self, k): return getattr(_t, k)
        def time(self): return _t.time() + self.offset
    shim = Shim()
    monkeypatch.setattr(webapp, "time", shim)
    monkeypatch.setattr(webapp, "FEEDBACK_MIN_AGE_S", 3)
    c, csrf, _ = fb(web)
    shim.offset = 1.0
    assert c.post("/feedback", data=ok_form(csrf), headers=fresh_ip()).status_code == 200 and rows(web) == []
    assert usage(web)["fb_drop_too_fast"] == 1
    shim.offset = 4.0
    assert c.post("/feedback", data=ok_form(csrf), headers=fresh_ip()).status_code == 200 and len(rows(web)) == 1


def test_link_filter_and_signed_in_exemption(web, cloud):
    c, csrf, _ = fb(web)
    many = "See https://a.example.org/x and https://b.example.org/y for the items."
    # signed-in users may paste a few links (2 is under the signed-in limit of 3)
    assert c.post("/feedback", data=ok_form(csrf, message=many, link=""), headers=fresh_ip()).status_code == 200
    assert len(rows(web)) == 1
    four = "See https://a.example.org/1 https://b.example.org/2 https://c.example.org/3 https://d.example.org/4"
    c.post("/feedback", data=ok_form(csrf, message=four, link=""), headers=fresh_ip())
    assert len(rows(web)) == 1 and usage(web)["fb_drop_many_links"] == 1
    c.post("/feedback", data=ok_form(csrf, message="buy cheap seo backlinks at foo.com", link=""), headers=fresh_ip())
    assert len(rows(web)) == 1 and usage(web)["fb_drop_spam_phrase"] == 1
    # ordinary text, own domain, one URL, a missing-space typo and a gov link are all fine
    for m in ["Sandy council STR item was not flagged.", "Why does agendawatch.app/watch say Sandy twice?",
              "One link only: https://www.utah.gov/pmn/x", "Nice.In my city the meetings are late.",
              "Dear Sir, my alert was wrong"]:
        assert c.post("/feedback", data=ok_form(csrf, message=m, link=""), headers=fresh_ip()).status_code == 200
    assert len(rows(web)) == 6 and len(cloud) == 6
    # spam phrases still apply when signed in
    c.post("/feedback", data=ok_form(csrf, message=SPAM, link=""), headers=fresh_ip())
    assert len(rows(web)) == 6 and usage(web)["fb_drop_spam_phrase"] == 2


def test_rate_limit_persists_in_db_and_uses_hashed_ip(web):
    c, csrf, _ = fb(web)
    for _ in range(5):
        assert c.post("/feedback", data=ok_form(csrf)).status_code == 200
    r = c.post("/feedback", data=ok_form(csrf))
    assert r.status_code == 429 and len(rows(web)) == 5 and usage(web)["fb_drop_rate_ip"] == 1
    with sqlite3.connect(web) as con:                       # survives a container restart; no raw IPs stored
        hits = con.execute("SELECT ip_hash FROM feedback_hits").fetchall()
    assert len(hits) == 5 and all(len(h[0]) == 16 and "." not in h[0] for h in hits)
    # spam attempts also burn the per-IP budget
    for _ in range(5):
        c.post("/feedback", data=ok_form(csrf, website="x"), headers={"cf-connecting-ip": "8.8.4.4"})
    assert c.post("/feedback", data=ok_form(csrf), headers={"cf-connecting-ip": "8.8.4.4"}).status_code == 429


def test_daily_store_cap(web, monkeypatch):
    monkeypatch.setattr(webapp, "FEEDBACK_STORE_DAILY_CAP", 2)
    c, csrf, _ = fb(web)
    codes = [c.post("/feedback", data=ok_form(csrf), headers={"cf-connecting-ip": f"7.7.7.{i}"}).status_code for i in range(3)]
    assert codes == [200, 200, 429] and len(rows(web)) == 2 and usage(web)["fb_drop_daily_cap"] == 1


def test_real_feedback_emails_once_and_counts_against_resend_cap(web, monkeypatch):
    sent = []
    monkeypatch.setenv("AW_EMAIL_RELAY_URL", "https://relay.invalid/__aw/email")
    monkeypatch.setenv("RESEND_API_KEY", "re_test_key")
    monkeypatch.setattr(webapp, "_send_via_resend", lambda to, s, b, **k: sent.append((to, s, b)) or (True, "sent"))
    c, csrf, _ = fb(web)
    assert c.post("/feedback", data=ok_form(csrf, message="Real report")).status_code == 200
    assert len(sent) == 1 and sent[0][0] == webapp.OWNER and "Real report" in sent[0][2]
    assert usage(web)["fb_accepted"] == 1 and usage(web)["fb_emailed"] == 1
    # near the Resend daily cap: still stored, notice held back so sign-in links keep working
    with sqlite3.connect(web) as con:
        con.execute("INSERT OR REPLACE INTO guard_usage(day,key,count) VALUES(?,?,?)",
                    (webapp.aw_guard.today(), "resend_sends", webapp.aw_guard.DEFAULTS["cap_resend_sends"] - 5)
                    if hasattr(webapp.aw_guard, "DEFAULTS") else (webapp.aw_guard.today(), "resend_sends", 96))
    assert c.post("/feedback", data=ok_form(csrf, message="Second report"), headers={"cf-connecting-ip": "5.5.5.5"}).status_code == 200
    assert len(rows(web)) == 2 and len(sent) == 1 and usage(web)["fb_email_suppressed"] == 1


def test_turnstile_off_by_default_and_on_when_configured(web, monkeypatch):
    c, csrf, page = fb(web)
    assert "turnstile" not in page.lower()
    monkeypatch.setenv("TURNSTILE_SITE_KEY", "1x00000000000000000000AA")
    assert "turnstile" not in c.get("/feedback").text.lower()          # secret missing -> still off
    monkeypatch.setenv("TURNSTILE_SECRET", "secret-not-real")
    page = c.get("/feedback").text
    assert 'data-sitekey="1x00000000000000000000AA"' in page and "challenges.cloudflare.com/turnstile/v0/api.js" in page
    assert "secret-not-real" not in page
    csrf = tok(c, page)
    calls = []
    verdict = {"v": True}
    monkeypatch.setattr(webapp, "turnstile_verify", lambda t, ip: calls.append(t) or verdict["v"])
    f = ok_form(csrf); f["cf-turnstile-response"] = "tok1"
    assert c.post("/feedback", data=f).status_code == 200 and len(rows(web)) == 1 and calls == ["tok1"]
    verdict["v"] = False                                                   # failed challenge: visible error, nothing stored
    r = c.post("/feedback", data=f)
    assert r.status_code == 400 and "verify" in r.text and len(rows(web)) == 1 and usage(web)["fb_drop_turnstile"] == 1
    verdict["v"] = None                                                    # Cloudflare unreachable: fail open, other layers apply
    assert c.post("/feedback", data=f).status_code == 200 and len(rows(web)) == 2
    assert usage(web)["fb_turnstile_unreachable"] == 1


def test_turnstile_verify_posts_secret_and_parses(monkeypatch):
    monkeypatch.setenv("TURNSTILE_SITE_KEY", "k"); monkeypatch.setenv("TURNSTILE_SECRET", "s3")
    seen = {}

    class R:
        def __init__(self, body): self.body = body
        def read(self): return self.body
        def __enter__(self): return self
        def __exit__(self, *a): return False

    def fake_open(req, timeout=0):
        seen["url"], seen["data"] = req.full_url, req.data.decode()
        return R(b'{"success": true}')
    import urllib.request
    monkeypatch.setattr(urllib.request, "urlopen", fake_open)
    assert webapp.turnstile_verify("tok", "1.2.3.4") is True
    assert seen["url"] == "https://challenges.cloudflare.com/turnstile/v0/siteverify"
    assert "secret=s3" in seen["data"] and "response=tok" in seen["data"] and "remoteip=1.2.3.4" in seen["data"]
    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: (_ for _ in ()).throw(OSError("down")))
    assert webapp.turnstile_verify("tok", "1.2.3.4") is None
    assert webapp.turnstile_verify("", "1.2.3.4") is False


def test_admin_shows_spam_filter_counts(web, cloud):
    c, csrf, _ = fb(web)
    c.post("/feedback", data=ok_form(csrf, message=SPAM, link=""))
    c.post("/feedback", data=ok_form(csrf, message="Real one"))
    a, _, _ = signed_in_client(web, ADMIN)
    page = a.get("/admin").text
    assert "Feedback form spam filter" in page
    assert re.search(r"<strong>1</strong> accepted, <strong>1</strong> dropped as spam \(spam_phrase: 1\)", page)
    assert "Dear Sir/Madam" in page and "Turnstile: <strong>off</strong>" in page
    assert len(rows(web)) == 1


# ---- 2026-10-23: submitter shown in owner notice + admin list ------------------------------
def test_signed_in_notice_names_account_from_session_not_form(web, cloud):
    c, csrf, _ = fb(web)
    assert c.post("/feedback", data=ok_form(csrf, email="other@example.com", user_id="forged@example.com")).status_code == 200
    [f] = rows(web)
    assert f["user_id"] == USER and f["email"] == USER          # posted email is ignored
    [(to, subj, body)] = cloud
    assert to == webapp.OWNER and subj == "Agenda Watch feedback: Missed alert"
    assert re.search(rf"^From: {re.escape(USER)} \(signed in, user #\d+\)$", body, re.M)
    assert "Reply-to" not in body and "forged@" not in body and "other@example.com" not in body
    assert body.index("From:") < body.index("Sandy council STR item")


def test_signed_in_blank_or_same_email_has_no_reply_to_line(web, cloud):
    c, _, _ = signed_in_client(web, USER)
    c.post("/feedback", data=ok_form(tok(c), email=""))
    c.post("/feedback", data=ok_form(tok(c), email=USER), headers={"cf-connecting-ip": "9.9.9.9"})
    assert [f["user_id"] for f in rows(web)] == [USER, USER]
    assert [f["email"] for f in rows(web)] == [USER, USER]
    assert len(cloud) == 2 and all("Reply-to" not in b and f"From: {USER} (signed in" in b for _, _, b in cloud)


def test_admin_list_shows_submitter_and_escapes(web):
    c, csrf, _ = fb(web)
    c.post("/feedback", data=ok_form(csrf, email="x@example.com", message="<b>hi</b>"))
    # existing/legacy anonymous row still renders (the form no longer creates these)
    with sqlite3.connect(web) as con:
        con.execute("INSERT INTO feedback(created_at,user_id,email,category,city,link,message,status) "
                    "VALUES(?,?,?,?,?,?,?,?)",
                    ("2026-10-22T12:00:00-06:00", None, "", "Other", "", "", "old anonymous note", "new"))
    ac, _, _ = signed_in_client(web, ADMIN)
    sec = ac.get("/admin").text
    sec = sec[sec.index('id="feedback"'):]
    assert re.search(rf"From: {re.escape(USER)} \(signed in, user #\d+\)", sec)
    assert "reply-to x@example.com" not in sec
    assert "From: not signed in · no email" in sec
    assert "<b>hi</b>" not in sec and "&lt;b&gt;hi&lt;/b&gt;" in sec


def test_from_line_helper():
    assert webapp.feedback_from_line(None) == "From: not signed in (anonymous)\nReply-to: none given"
    assert webapp.feedback_from_line("a@b.co", "a@b.co", 5) == "From: a@b.co (signed in, user #5)"
    assert webapp.feedback_from_line("a@b.co", "c@d.co", None) == "From: a@b.co (signed in)\nReply-to (typed on the form): c@d.co"
