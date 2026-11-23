"""Nov 13 2026: read-only /internal/watch-list for the morning report (enabled cities only)."""
import sqlite3

from fastapi.testclient import TestClient

from test_admin import ADMIN, OWNER, USER, web  # noqa: F401
from test_web_watch import seed, webapp

SECRET = "s3cret"
H = {"x-aw-internal-secret": SECRET}


def cloud(monkeypatch):
    monkeypatch.setenv("AW_CLOUD", "1")
    monkeypatch.setenv("AW_INTERNAL_SECRET", SECRET)
    return TestClient(webapp.app)


def _seed_typical(db):
    """OWNER: two cities on (plus leftover off rows), paid 1 extra. USER: one city, alerts off. ADMIN: two on."""
    seed(db, email=OWNER, status="active", extra_cities=1, alerts_on=1,
         cities={"ch": (1, "100 Example St"), "sandy": (1, ""), "slc": (0, "451 S State St")})
    seed(db, email=USER, status=None, extra_cities=None, alerts_on=0,
         cities={"ch": (1, "")})
    seed(db, email=ADMIN, status=None, extra_cities=None, alerts_on=1,
         cities={"ch": (1, ""), "provo": (1, "")})
    with sqlite3.connect(db) as c:
        c.execute("UPDATE users SET is_admin=1 WHERE email=?", (ADMIN,))
        # leftover off rows like the live 10-city catalog leftover must not be counted
        c.execute("INSERT OR REPLACE INTO watch_cities(email,city_key,enabled,street,updated_at) "
                  "VALUES(?,?,0,'','2026-10-01T00:00:00-06:00')", (OWNER, "draper"))


def test_watch_list_requires_cloud_and_secret(web, monkeypatch):
    monkeypatch.delenv("AW_CLOUD", raising=False)
    c = TestClient(webapp.app)
    assert c.get("/internal/watch-list").status_code == 404
    monkeypatch.setenv("AW_CLOUD", "1")
    monkeypatch.setenv("AW_INTERNAL_SECRET", SECRET)
    assert c.get("/internal/watch-list").status_code == 403
    assert c.get("/internal/watch-list", headers={"x-aw-internal-secret": "nope"}).status_code == 403


def test_watch_list_enabled_only_and_plan(web, monkeypatch):
    _seed_typical(web)
    before = list(sqlite3.connect(web).execute("SELECT email,city_key,enabled,street FROM watch_cities ORDER BY 1,2"))
    c = cloud(monkeypatch)
    r = c.get("/internal/watch-list", headers=H)
    assert r.status_code == 200
    accounts = {a["email"]: a for a in r.json()["accounts"]}
    assert set(accounts) == {OWNER, USER, ADMIN}

    o = accounts[OWNER]
    assert o["alerts_on"] is True and o["is_admin"] is False
    assert o["subscription_status"] == "active" and o["extra_cities"] == 1 and o["allowed"] == 2
    assert o["enabled"] == [
        {"key": "ch", "name": "Cottonwood Heights", "street": "100 Example St"},
        {"key": "sandy", "name": "Sandy", "street": ""},
    ]
    assert o["created_at"]
    # disabled leftover rows (slc, draper) are omitted
    assert [e["key"] for e in o["enabled"]] == ["ch", "sandy"]

    u = accounts[USER]
    assert u["alerts_on"] is False and u["extra_cities"] == 0 and u["allowed"] == 1
    assert u["subscription_status"] is None and u["enabled"] == [
        {"key": "ch", "name": "Cottonwood Heights", "street": ""},
    ]

    a = accounts[ADMIN]
    assert a["is_admin"] is True and a["allowed"] == len(webapp.CITY_KEYS)
    assert [e["key"] for e in a["enabled"]] == ["ch", "provo"] and a["alerts_on"] is True

    # strictly read-only
    after = list(sqlite3.connect(web).execute("SELECT email,city_key,enabled,street FROM watch_cities ORDER BY 1,2"))
    assert after == before


def test_watch_list_email_filter_and_admin_watch_unchanged(web, monkeypatch):
    _seed_typical(web)
    c = cloud(monkeypatch)
    r = c.get("/internal/watch-list", params={"email": OWNER}, headers=H)
    assert r.status_code == 200
    acc = r.json()["accounts"]
    assert len(acc) == 1 and acc[0]["email"] == OWNER
    assert [e["key"] for e in acc[0]["enabled"]] == ["ch", "sandy"]
    assert c.get("/internal/watch-list", params={"email": "nobody@example.invalid"}, headers=H).status_code == 404
    # admin-watch still refuses non-admin accounts
    assert c.get(f"/internal/admin-watch?email={OWNER}", headers=H).status_code == 403
    assert c.get(f"/internal/admin-watch?email={ADMIN}", headers=H).status_code == 200
