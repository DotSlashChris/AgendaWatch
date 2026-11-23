"""2026-10-22 admin swap: admin@agendawatch.app is the admin (unlimited free cities), the owner
address owner@example.com is a standard user, owner-only mail routing is unchanged. Throwaway DBs only."""
import sqlite3

from fastapi.testclient import TestClient

from test_admin import ADMIN, OWNER, USER, web  # noqa: F401  (fixture)
from test_web_watch import seed, signed_in_client, webapp


def _form(csrf, keys):
    d = {"csrf": csrf, "cities_submitted": "1", "keywords_submitted": "1", "keywords": "STR",
         "alerts_submitted": "1", "alerts_on": "1"}
    for k in keys:
        d[f"city_{k}"] = "1"
        d[f"street_{k}"] = f"1 {k} St"
    return d


def test_config_and_flags(web):
    assert webapp.ADMIN_EMAILS == ["admin@agendawatch.app"]
    assert webapp.OWNER == "owner@example.com"
    assert webapp.is_admin(ADMIN) and not webapp.is_admin(OWNER)


def test_admin_row_created_without_signin(sandbox, monkeypatch):
    db = sandbox.root / "data" / "fresh.db"
    monkeypatch.setattr(webapp, "DB", db)
    webapp.init_db()
    with sqlite3.connect(db) as c:
        assert c.execute("SELECT is_admin FROM users WHERE email=?", (ADMIN,)).fetchone() == (1,)


def test_swap_migration_demotes_owner_once(sandbox, monkeypatch):
    """Production shape: owner already admin from owner_admin_v1 -> demoted; never re-granted."""
    db = sandbox.root / "data" / "prod.db"
    monkeypatch.setattr(webapp, "DB", db)
    webapp.init_db()
    with sqlite3.connect(db) as c:
        c.execute("DELETE FROM schema_migrations WHERE id='admin_swap_20261022'")
        c.execute("INSERT INTO users(email, created_at, is_admin) VALUES(?, 'x', 1)", (OWNER,))
        c.execute("INSERT OR IGNORE INTO schema_migrations(id, applied_at) VALUES('owner_admin_v1','x')")
    webapp.init_db()
    webapp.init_db()
    with sqlite3.connect(db) as c:
        rows = dict(c.execute("SELECT email, is_admin FROM users"))
    assert rows == {ADMIN: 1, OWNER: 0}


def test_owner_keeps_watch_data_through_swap(web):
    seed(web, email=OWNER, status="active", extra_cities=1,
         cities={"ch": (1, "100 Example St"), "sandy": (1, "")})
    before = sqlite3.connect(web).execute(
        "SELECT city_key, enabled, street FROM watch_cities WHERE email=? ORDER BY city_key", (OWNER,)).fetchall()
    webapp.init_db()
    after = sqlite3.connect(web).execute(
        "SELECT city_key, enabled, street FROM watch_cities WHERE email=? ORDER BY city_key", (OWNER,)).fetchall()
    assert before == after and webapp.allowed_cities(OWNER) == 2


def test_admin_can_save_all_17_cities_with_streets(web):
    keys = webapp.CITY_KEYS
    assert len(keys) == 17 and webapp.MAX_CITIES == 10
    assert webapp.allowed_cities(ADMIN) == 17
    client, csrf, page = signed_in_client(web, ADMIN)
    assert "Admin account" in page
    r = client.post("/watch", data=_form(csrf, keys), follow_redirects=False)
    assert r.status_code == 303
    with sqlite3.connect(web) as c:
        on = c.execute("SELECT COUNT(*) FROM watch_cities WHERE email=? AND enabled=1 AND street<>''",
                       (ADMIN,)).fetchone()[0]
    assert on == 17
    assert webapp.enforce_city_limit(ADMIN) == []
    assert webapp.city_locks(ADMIN)[0] == {}


def test_standard_users_cap_unchanged(web):
    assert webapp.allowed_cities(USER) == 3       # 1 free + 2 extra
    seed(web, email="free@example.invalid", status=None, cities={"ch": (1, "")})
    assert webapp.allowed_cities("free@example.invalid") == 1
    seed(web, email="big@example.invalid", status="active", extra_cities=40, cities={"ch": (1, "")})
    assert webapp.allowed_cities("big@example.invalid") == 10


def test_admin_never_sent_to_checkout(web, monkeypatch):
    monkeypatch.setattr(webapp.billing, "configured", lambda: True)
    called = []
    monkeypatch.setattr(webapp.billing, "get_or_create_customer", lambda *a, **k: called.append(1))
    client, csrf, _ = signed_in_client(web, ADMIN)
    r = client.post("/billing/checkout", data={"csrf": csrf, "quantity": "1"}, follow_redirects=False)
    assert r.status_code == 303 and "checkout=admin" in r.headers["location"] and called == []
    assert "Admin: all 17 cities free" in client.get("/billing").text


def test_admin_page_access(web):
    a, _, _ = signed_in_client(web, ADMIN)
    assert a.get("/admin").status_code == 200
    o, _, _ = signed_in_client(web, OWNER)
    assert o.get("/admin").status_code == 404
    assert TestClient(webapp.app).get("/admin").status_code == 404


def test_owner_routing_does_not_need_admin(web, monkeypatch):
    """Owner-only mail is keyed to the owner address (not the admin flag) and lands in the project inbox."""
    import pytest
    monkeypatch.setenv("NOTIFY_TO", "inbox@example.invalid")
    monkeypatch.setenv("RESEND_API_KEY", "x")
    monkeypatch.setattr(webapp, "email_provider", lambda: "resend")
    assert webapp.owner_inbox() == "inbox@example.invalid"
    with pytest.raises(PermissionError):
        webapp.send_owner_email(ADMIN, "s", "b")
    sent = []
    monkeypatch.setattr(webapp, "_send_via_resend",
                        lambda to, s, b, redirect_owner=True, **k: (sent.append(
                            webapp.owner_inbox() if redirect_owner and to == webapp.OWNER else to) or True, "sent"))
    assert not webapp.is_admin(OWNER)
    webapp.send_owner_email(OWNER, "s", "b")
    assert sent == ["inbox@example.invalid"]


# ---------------------------------------------------------------- account deletion
class _L:
    def __init__(self, data): self.data = data


class _Obj:
    def __init__(self, **k): self.__dict__.update(k)


def _fake_stripe(monkeypatch, customers=(), subs=()):
    calls = []

    class Customer:
        @staticmethod
        def list(**k): calls.append(("Customer.list", k)); return _L([_Obj(id=c) for c in customers])

    class Subscription:
        @staticmethod
        def list(**k): calls.append(("Subscription.list", k)); return _L([_Obj(**s) for s in subs])

        @staticmethod
        def retrieve(i): calls.append(("Subscription.retrieve", i)); return _Obj(id=i, customer="c", status="canceled", livemode=True)

    monkeypatch.setattr(webapp.billing, "_client", lambda: _Obj(Customer=Customer, Subscription=Subscription))
    return calls


SPY = "spyke@example.invalid"
SECRET = "s3cret-test"


def _cloud(monkeypatch):
    monkeypatch.setenv("AW_CLOUD", "1")
    monkeypatch.setenv("AW_INTERNAL_SECRET", SECRET)
    return TestClient(webapp.app)


def _seed_spy(web):
    seed(web, email=SPY, status=None, cities={"ch": (1, "")})
    with sqlite3.connect(web) as c:
        c.execute("INSERT INTO magic_nonces(nonce,email,created_at) VALUES('n-spy', ?, 1)", (SPY,))


def _count(web, email):
    with sqlite3.connect(web) as c:
        return {t: c.execute(f"SELECT COUNT(*) FROM {t} WHERE email=?", (email,)).fetchone()[0]
                for t in ("users", "watches", "watch_cities", "magic_nonces")}


def test_delete_user_gated_and_owner_admin_refused(web, monkeypatch):
    c = _cloud(monkeypatch)
    _fake_stripe(monkeypatch)
    assert c.post("/internal/delete-user", json={"email": SPY, "confirm": SPY}).status_code == 403
    h = {"x-aw-internal-secret": SECRET}
    for e in (OWNER, ADMIN):
        assert c.post("/internal/delete-user", json={"email": e, "confirm": e}, headers=h).status_code == 403
    assert c.post("/internal/delete-user", json={"email": USER, "confirm": "x"}, headers=h).status_code == 400


def test_delete_user_stops_on_active_subscription(web, monkeypatch):
    _seed_spy(web)
    c = _cloud(monkeypatch)
    calls = _fake_stripe(monkeypatch, customers=["cus_1"], subs=[dict(id="sub_1", status="active", livemode=True)])
    r = c.post("/internal/delete-user", json={"email": SPY, "confirm": SPY}, headers={"x-aw-internal-secret": SECRET})
    assert r.status_code == 409 and _count(web, SPY)["users"] == 1
    assert all(name.endswith((".list", ".retrieve")) for name, _ in calls)   # read-only Stripe calls


def test_delete_user_customer_only_deletes_app_rows(web, monkeypatch):
    _seed_spy(web)
    before_other = _count(web, USER)
    c = _cloud(monkeypatch)
    calls = _fake_stripe(monkeypatch, customers=["cus_9"], subs=[dict(id="sub_9", status="canceled", livemode=True)])
    client, _, _ = signed_in_client(web, SPY)
    r = c.post("/internal/delete-user", json={"email": SPY, "confirm": SPY}, headers={"x-aw-internal-secret": SECRET})
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["stripe"]["customers"] == ["cus_9"] and not j["stripe"]["paid_active"]
    assert _count(web, SPY) == {"users": 0, "watches": 0, "watch_cities": 0, "magic_nonces": 0}
    assert _count(web, USER) == before_other and webapp.is_admin(ADMIN)
    assert all(name.endswith((".list", ".retrieve")) for name, _ in calls)
    # the deleted account's old session cookie no longer works
    assert client.get("/watch", follow_redirects=False).status_code in (303, 302, 307)


def test_user_check_read_only(web, monkeypatch):
    _seed_spy(web)
    c = _cloud(monkeypatch)
    _fake_stripe(monkeypatch)
    assert c.get(f"/internal/user-check?email={SPY}").status_code == 403
    j = c.get(f"/internal/user-check?email={SPY}", headers={"x-aw-internal-secret": SECRET}).json()
    assert j["rows"]["users"] == 1 and j["stripe"]["customers"] == [] and not j["stripe"]["paid_active"]
    assert _count(web, SPY)["users"] == 1
