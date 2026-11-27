"""Nov 27 2026: per-post ?ref= signup attribution. First-touch cookie, carried through the email sign-in
flow, recorded once on a NEW account; counts exposed to the morning report and the admin page.
Throwaway DBs and fake example.invalid addresses only; nothing is sent."""
import json
import re
import sqlite3
import time

from fastapi.testclient import TestClient

from test_admin import ADMIN, OWNER, USER, web  # noqa: F401
from test_web_watch import seed, signed_in_client, webapp

SECRET = "s3cret"
H = {"x-aw-internal-secret": SECRET}
BROWSER = {"user-agent": "Mozilla/5.0 (Macintosh) AppleWebKit/537.36 Chrome/130.0 Safari/537.36"}
NEW = "test+newref@example.invalid"


def csrf_of(client):
    page = client.get("/signin", headers=BROWSER).text
    return re.search(r'name="csrf" value="([^"]+)"', page).group(1)


def request_link(client, email):
    r = client.post("/signin", data={"email": email, "csrf": csrf_of(client)}, headers=BROWSER)
    assert r.status_code == 200


def latest_token(db, email):
    with sqlite3.connect(db) as c:
        nonce = c.execute("SELECT nonce FROM magic_nonces WHERE email=? ORDER BY created_at DESC LIMIT 1",
                          (email,)).fetchone()[0]
    return webapp.signer.dumps({"e": email, "n": nonce})


def user_ref(db, email):
    with sqlite3.connect(db) as c:
        row = c.execute("SELECT ref FROM users WHERE email=?", (email,)).fetchone()
    return row[0] if row else "NO-USER"


def test_clean_ref_sanitizes():
    assert webapp.clean_ref("IG-CH-1126") == "ig-ch-1126"
    assert webapp.clean_ref("  Rd_Provo 1126 ") == "rd-provo-1126"
    assert webapp.clean_ref("<script>alert(1)</script>") == "scriptalert1script"
    assert webapp.clean_ref("../../etc/passwd") == "etcpasswd"
    assert webapp.clean_ref("!!!") is None and webapp.clean_ref("") is None and webapp.clean_ref(None) is None
    assert len(webapp.clean_ref("a" * 200)) == 40
    assert webapp.clean_ref("a" * 39 + "-b") == "a" * 39      # no trailing hyphen after the cut


def test_columns_are_additive(web):
    with sqlite3.connect(web) as c:
        assert "ref" in {r[1] for r in c.execute("PRAGMA table_info(users)")}
        assert "ref" in {r[1] for r in c.execute("PRAGMA table_info(magic_nonces)")}
    webapp.init_db()   # idempotent: a second run must not fail or lose a value
    seed(web, email=NEW, status=None)
    with sqlite3.connect(web) as c:
        c.execute("UPDATE users SET ref='x-1' WHERE email=?", (NEW,))
    webapp.init_db()
    assert user_ref(web, NEW) == "x-1"


def test_ref_on_public_pages_sets_first_touch_cookie(web):
    for path in ("/?ref=IG-CH-1126", "/signin?ref=ig-ch-1126", "/cities/cottonwood-heights?ref=ig-ch-1126"):
        c = TestClient(webapp.app)
        r = c.get(path, headers=BROWSER)
        assert r.status_code == 200, path
        sc = [v for k, v in r.headers.multi_items() if k == "set-cookie" and v.startswith("aw_ref=")]
        assert len(sc) == 1, path
        assert sc[0].startswith("aw_ref=ig-ch-1126;") and "Max-Age=2592000" in sc[0]
        assert "SameSite=lax" in sc[0] and "HttpOnly" in sc[0] and "Path=/" in sc[0]
        assert c.cookies.get("aw_ref") == "ig-ch-1126"


def test_cookie_not_set_without_ref_for_bots_junk_or_errors(web):
    c = TestClient(webapp.app)
    assert "aw_ref" not in c.get("/", headers=BROWSER).headers.get("set-cookie", "")
    assert "aw_ref" not in c.get("/?ref=%21%21%21", headers=BROWSER).headers.get("set-cookie", "")
    assert "aw_ref" not in c.get("/?ref=x", headers={"user-agent": "facebookexternalhit/1.1"}).headers.get("set-cookie", "")
    assert "aw_ref" not in c.get("/nope?ref=x", headers=BROWSER).headers.get("set-cookie", "")
    assert "aw_ref" not in c.get("/watch?ref=x", headers=BROWSER, follow_redirects=False).headers.get("set-cookie", "")


def test_first_touch_wins(web):
    c = TestClient(webapp.app)
    c.get("/?ref=fb-provo-1126", headers=BROWSER)
    r = c.get("/?ref=x-provo-1126", headers=BROWSER)
    assert "aw_ref" not in r.headers.get("set-cookie", "")
    assert c.cookies.get("aw_ref") == "fb-provo-1126"


def test_signup_records_ref_even_when_link_opened_in_another_browser(web):
    c1 = TestClient(webapp.app)
    c1.get("/?ref=rd-provo-1126", headers=BROWSER)
    request_link(c1, NEW)
    token = latest_token(web, NEW)
    c2 = TestClient(webapp.app)                       # mail app opens the link: no cookie there
    assert c2.cookies.get("aw_ref") is None
    r = c2.get(f"/auth/verify?token={token}", follow_redirects=False)
    assert r.status_code == 303
    assert user_ref(web, NEW) == "rd-provo-1126"


def test_signup_falls_back_to_cookie_on_the_verify_browser(web):
    c = TestClient(webapp.app)
    request_link(c, NEW)                              # requested with no ref
    token = latest_token(web, NEW)
    c.cookies.set("aw_ref", "nd-ch-1126")
    assert c.get(f"/auth/verify?token={token}", follow_redirects=False).status_code == 303
    assert user_ref(web, NEW) == "nd-ch-1126"


def test_direct_signup_has_no_ref(web):
    c = TestClient(webapp.app)
    request_link(c, NEW)
    c.get(f"/auth/verify?token={latest_token(web, NEW)}", follow_redirects=False)
    assert user_ref(web, NEW) is None


def test_existing_user_ref_is_never_overwritten(web):
    # an existing account with no ref, and one with a ref, both sign in again from a ?ref= visit
    with sqlite3.connect(web) as c:
        c.execute("UPDATE users SET ref='ig-ch-1126' WHERE email=?", (USER,))
    for email, before in ((USER, "ig-ch-1126"), (OWNER, None)):
        c = TestClient(webapp.app)
        c.get("/?ref=x-provo-1126", headers=BROWSER)
        request_link(c, email)
        c.get(f"/auth/verify?token={latest_token(web, email)}", follow_redirects=False)
        assert user_ref(web, email) == before, email


def test_ref_is_stored_only_as_a_code_no_other_data(web):
    c = TestClient(webapp.app)
    c.get("/?ref=ig-ch-1126", headers={**BROWSER, "x-forwarded-for": "203.0.113.9"})
    request_link(c, NEW)
    with sqlite3.connect(web) as db:
        cols = {r[1] for r in db.execute("PRAGMA table_info(magic_nonces)")}
        assert not (cols & {"ip", "ip_hash", "user_agent", "ua"})
        cols = {r[1] for r in db.execute("PRAGMA table_info(users)")}
        assert not (cols & {"ip", "ip_hash", "user_agent", "ua"})
        dump = json.dumps([list(r) for r in db.execute("SELECT * FROM magic_nonces")])
    assert "203.0.113.9" not in dump and "Mozilla" not in dump


def test_report_counts_by_ref_and_direct(web, monkeypatch):
    for email, ref in ((NEW, "ig-ch-1126"), ("test+b@example.invalid", "ig-ch-1126"),
                       ("test+c@example.invalid", "x-provo-1126")):
        seed(web, email=email, status=None)
        with sqlite3.connect(web) as c:
            c.execute("UPDATE users SET ref=? WHERE email=?", (ref, email))
    monkeypatch.setenv("AW_CLOUD", "1")
    monkeypatch.setenv("AW_INTERNAL_SECRET", SECRET)
    c = TestClient(webapp.app)
    wl = c.get("/internal/watch-list", headers=H).json()
    assert wl["ref_signups"] == {"direct": 3, "ig-ch-1126": 2, "x-provo-1126": 1}
    assert sum(wl["ref_signups"].values()) == len(wl["accounts"]) == 6
    by = {a["email"]: a["ref"] for a in wl["accounts"]}
    assert by[NEW] == "ig-ch-1126" and by[USER] == "direct"
    assert c.get("/internal/watch-list", headers={"x-aw-internal-secret": "bad"}).status_code == 403
    one = c.get("/internal/watch-list", params={"email": NEW}, headers=H).json()
    assert one["accounts"][0]["ref"] == "ig-ch-1126"
    st = c.get("/internal/status", headers=H).json()
    assert st["ref_signups"] == wl["ref_signups"]


def test_admin_page_shows_signups_by_source(web):
    with sqlite3.connect(web) as c:
        c.execute("UPDATE users SET ref='fb-provo-1126' WHERE email=?", (USER,))
    client, _, _ = signed_in_client(web, ADMIN)
    r = client.get("/admin")
    assert r.status_code == 200
    assert "Signups by source" in r.text and "fb-provo-1126" in r.text and "direct" in r.text
