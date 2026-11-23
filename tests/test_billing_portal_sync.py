"""Customer Portal + sync-without-webhooks + downgrade rule.

Fake accounts only, throwaway SQLite DB (sandbox), Stripe API mocked (no network)."""
import re
import sqlite3
import time
import types

import pytest
from fastapi.testclient import TestClient

from web import app as webapp
from web import stripe_billing as billing
from tests.test_web_watch import seed, row, signed_in_client

EMAIL = "test+sync@example.invalid"
CUST = "cus_fake_sync"
STREETS = {"ch": "100 Test St", "sandy": "200 Test Ave", "draper": "", "provo": "300 Fake Blvd"}


@pytest.fixture
def web(sandbox, monkeypatch):
    db = sandbox.root / "data" / "web.db"
    monkeypatch.setattr(webapp, "DB", db)
    monkeypatch.setattr(webapp, "OUTBOX", sandbox.outbox / "magic")
    monkeypatch.setattr(webapp, "HISTORY_JSON", sandbox.state / "sent_history.json")
    webapp.init_db()
    return db


class FakeStripe:
    """Stands in for the live Stripe API: one 'current' subscription per test."""

    def __init__(self):
        self.sub = None
        self.calls = 0

    def set(self, status, quantity=1, sub_id="sub_fake_sync"):
        self.sub = None if status is None else {
            "id": sub_id, "status": status, "customer": CUST,
            "items": {"data": [{"id": "si_fake", "quantity": quantity}]}}

    def fetch(self, customer_id, preferred_id=None):
        assert customer_id == CUST
        self.calls += 1
        return self.sub


@pytest.fixture
def stripe_api(monkeypatch):
    fake = FakeStripe()
    monkeypatch.setattr(billing, "configured", lambda: True)
    monkeypatch.setattr(billing, "fetch_current_subscription", fake.fetch)
    return fake


def seed_paid(db, *, status="active", extra=3, cities=None, enabled_at=None):
    cities = cities or {k: (1, s) for k, s in STREETS.items()}
    seed(db, email=EMAIL, status=status, extra_cities=extra, cities=cities)
    with sqlite3.connect(db) as c:
        c.execute("UPDATE users SET stripe_customer_id=?, stripe_subscription_id='sub_fake_sync' WHERE email=?",
                  (CUST, EMAIL))
        for k, ts in (enabled_at or {}).items():
            c.execute("UPDATE watch_cities SET enabled_at=? WHERE email=? AND city_key=?", (ts, EMAIL, k))


def user(db):
    with sqlite3.connect(db) as c:
        c.row_factory = sqlite3.Row
        return dict(c.execute("SELECT * FROM users WHERE email=?", (EMAIL,)).fetchone())


def on(db):
    w = row(db, EMAIL)
    return [k for k in webapp.CITY_KEYS if w[f"city_{k}"]]


def streets(db):
    w = row(db, EMAIL)
    return {k: w[f"street_{k}"] for k in STREETS}


def form(csrf, cities):
    """Browser-style full form: checked boxes; street fields only for cities that are on
    (off cities' street inputs are disabled in the browser and not posted)."""
    d = {"csrf": csrf, "cities_submitted": "1", "keywords_submitted": "1", "keywords": "STR",
         "alerts_submitted": "1"}
    for k, street in cities.items():
        d[f"city_{k}"] = "1"
        d[f"street_{k}"] = street
    return d


# ------------------------------------------------------------ return sync
def test_return_sync_quantity_up_updates_db_and_does_not_auto_enable(web, stripe_api):
    seed_paid(web, extra=1, cities={"ch": (1, STREETS["ch"]), "sandy": (0, STREETS["sandy"])})
    stripe_api.set("active", 1)                    # Stripe matches the DB at sign-in
    client, _, _ = signed_in_client(web, EMAIL)
    stripe_api.set("active", 3)
    r = client.get("/billing/return", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/billing"
    u = user(web)
    assert (u["subscription_status"], u["extra_cities"]) == ("active", 3)
    assert webapp.allowed_cities(EMAIL) == 4
    assert on(web) == ["ch"]                       # upgrade never switches cities on
    bpage = client.get("/billing").text
    assert "Billing updated: 3 extra cities · up to 4 cities." in bpage
    assert "3 extra cities · up to 4 cities" in bpage and 'id="manage-billing"' in bpage
    assert "Billing updated" not in client.get("/billing").text   # notice shows once
    page = client.get("/watch").text
    assert "Billing updated" not in page
    assert "Your plan allows <strong>4 cities</strong>" in page
    # Save now works up to the new cap (4), and the stored Sandy street comes back with it.
    csrf = re.search(r'name="csrf" value="([^"]+)"', page).group(1)
    assert 'name="street_sandy" id="street_sandy" value="200 Test Ave"' in page
    r = client.post("/watch", data=form(csrf, {"ch": STREETS["ch"], "sandy": STREETS["sandy"],
                                               "draper": "", "provo": STREETS["provo"]}),
                    follow_redirects=False)
    assert r.status_code == 303 and set(on(web)) == {"ch", "sandy", "draper", "provo"}
    assert streets(web) == STREETS


def test_return_sync_quantity_down_switches_off_latest_cities_keeps_all_streets(web, stripe_api):
    # Switched on in this order: provo, sandy, ch, draper (not config order).
    seed_paid(web, extra=3, enabled_at={"provo": "2026-10-01T08:00:00-06:00", "sandy": "2026-10-02T08:00:00-06:00",
                                        "ch": "2026-10-03T08:00:00-06:00", "draper": "2026-10-04T08:00:00-06:00"})
    stripe_api.set("active", 3)
    client, _, _ = signed_in_client(web, EMAIL)
    stripe_api.set("active", 1)
    r = client.get("/billing/return")
    assert r.status_code == 200 and str(r.url).endswith("/billing")
    assert user(web)["extra_cities"] == 1
    assert set(on(web)) == {"provo", "sandy"}      # the two switched on earliest
    assert streets(web) == STREETS                 # nothing deleted, incl. switched-off cities
    assert "Billing updated: 1 extra city · up to 2 cities." in r.text
    assert "Switched off: Cottonwood Heights, Draper (their streets are kept)." in r.text


def test_downgrade_without_enabled_at_falls_back_to_config_order(web, stripe_api):
    seed_paid(web, extra=3)
    with sqlite3.connect(web) as c:
        c.execute("UPDATE watch_cities SET enabled_at=NULL WHERE email=?", (EMAIL,))
    stripe_api.set("active", 1)
    webapp.sync_billing(EMAIL, force=True)
    order = [k for k in webapp.CITY_KEYS if k in STREETS]
    assert on(web) == order[:2]
    assert streets(web) == STREETS


def test_cancel_returns_to_one_city_with_its_street_and_blocks_second_city(web, stripe_api):
    seed_paid(web, extra=3, enabled_at={"ch": "2026-10-01T08:00:00-06:00", "sandy": "2026-10-02T08:00:00-06:00",
                                        "draper": "2026-10-03T08:00:00-06:00", "provo": "2026-10-04T08:00:00-06:00"})
    stripe_api.set("active", 3)
    client, _, _ = signed_in_client(web, EMAIL)
    stripe_api.set("canceled", 3)
    resp = client.get("/billing/return")
    assert str(resp.url).endswith("/billing")
    page = resp.text
    u = user(web)
    assert (u["subscription_status"], u["extra_cities"]) == ("canceled", 0)
    assert on(web) == ["ch"] and row(web, EMAIL)["street_ch"] == "100 Test St"
    assert streets(web) == STREETS
    assert "Subscription canceled: back to 1 city." in page
    assert "Free: 1 city" in page and 'id="manage-billing"' in page   # receipts still reachable
    # Canceled/free cannot enable a second city: 400, DB unchanged.
    page = client.get("/watch").text
    assert 'id="manage-billing"' not in page and 'href="/billing"' in page
    csrf = re.search(r'name="csrf" value="([^"]+)"', page).group(1)
    before = row(web, EMAIL)
    r = client.post("/watch", data=form(csrf, {"ch": STREETS["ch"], "sandy": STREETS["sandy"]}),
                    follow_redirects=False)
    assert r.status_code == 400 and "free plan watches one city" in r.text
    assert row(web, EMAIL) == before
    # A normal browser save (off cities' street inputs not posted) keeps every stored street.
    r = client.post("/watch", data=form(csrf, {"ch": STREETS["ch"]}), follow_redirects=False)
    assert r.status_code == 303 and streets(web) == STREETS


def test_free_account_cannot_enable_second_city(web, stripe_api):
    seed(web, email=EMAIL, status=None, cities={"ch": (1, "100 Test St")})
    client, csrf, _ = signed_in_client(web, EMAIL)
    before = row(web, EMAIL)
    r = client.post("/watch", data=form(csrf, {"ch": "100 Test St", "sandy": ""}), follow_redirects=False)
    assert r.status_code == 400 and row(web, EMAIL) == before


def test_no_subscription_at_all_is_free(web, stripe_api):
    seed_paid(web, extra=2)
    stripe_api.set(None)
    res = webapp.sync_billing(EMAIL, force=True)
    assert res["status"] == "none" and user(web)["extra_cities"] == 0
    assert len(on(web)) == 1 and streets(web) == STREETS


def test_watch_page_load_syncs_at_most_once_a_minute(web, stripe_api):
    seed_paid(web, extra=3)
    stripe_api.set("active", 3)
    client, _, _ = signed_in_client(web, EMAIL)    # first /watch load syncs
    assert stripe_api.calls == 1
    stripe_api.set("active", 1)
    client.get("/watch")
    assert stripe_api.calls == 1 and user(web)["extra_cities"] == 3     # throttled
    with sqlite3.connect(web) as c:
        c.execute("UPDATE users SET billing_synced_at=? WHERE email=?", (time.time() - 61, EMAIL))
    page = client.get("/watch").text                # stale -> re-read (portal tab closed)
    assert stripe_api.calls == 2 and user(web)["extra_cities"] == 1
    assert len(on(web)) == 2 and streets(web) == STREETS
    assert "Billing updated: 1 extra city" in page


def test_stripe_error_leaves_db_unchanged(web, monkeypatch):
    seed_paid(web, extra=3)
    monkeypatch.setattr(billing, "configured", lambda: True)

    def boom(*a, **k):
        raise RuntimeError("stripe down")
    monkeypatch.setattr(billing, "fetch_current_subscription", boom)
    before = (user(web)["extra_cities"], row(web, EMAIL))
    assert webapp.sync_billing(EMAIL, force=True) is None
    assert (user(web)["extra_cities"], row(web, EMAIL)) == before


def test_webhook_deleted_uses_same_sync_and_downgrades(web, monkeypatch):
    seed_paid(web, extra=3)
    event = {"id": "evt_x", "type": "customer.subscription.deleted", "data": {"object": {
        "id": "sub_fake_sync", "status": "canceled", "customer": CUST, "metadata": {"email": EMAIL},
        "items": {"data": [{"id": "si_fake", "quantity": 3}]}}}}
    monkeypatch.setattr(billing, "configured", lambda: True)
    monkeypatch.setattr(billing, "construct_event", lambda payload, sig: event)
    r = TestClient(webapp.app).post("/stripe/webhook", content=b"{}", headers={"stripe-signature": "t"})
    assert r.status_code == 200
    assert user(web)["extra_cities"] == 0 and len(on(web)) == 1 and streets(web) == STREETS


# ------------------------------------------------------------ billing page
def test_billing_page_requires_sign_in(web):
    r = TestClient(webapp.app).get("/billing", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/signin"


def test_billing_page_paid_shows_manage_billing_plan_and_count(web, stripe_api):
    seed_paid(web, extra=2, cities={"ch": (1, "100 Test St"), "sandy": (1, "200 Test Ave"), "draper": (1, "")})
    stripe_api.set("active", 2)
    client, _, _ = signed_in_client(web, EMAIL)
    r = client.get("/billing")
    assert r.status_code == 200
    html = r.text
    assert "<h1>Billing</h1>" in html and "Plan &amp; billing" in html
    assert "2 extra cities · up to 3 cities" in html
    assert "3 of 3 cities on." in html
    m = re.search(r'<form method="post" action="/billing/portal" target="_blank"[^>]*>.*?</form>', html, re.S)
    assert m and 'id="manage-billing"' in m.group(0) and "Manage billing" in m.group(0)
    assert re.search(r'name="csrf" value="[^"]+"', m.group(0))
    assert 'action="/billing/checkout"' not in html
    assert 'action="/watch"' not in html           # no watch form on Billing


def test_billing_page_free_shows_plan_checkout_and_no_portal_without_customer(web, monkeypatch):
    monkeypatch.setattr(billing, "configured", lambda: True)
    seed(web, email=EMAIL, status=None, cities={"ch": (1, "")})
    client, _, _ = signed_in_client(web, EMAIL)
    html = client.get("/billing").text
    assert "Free: 1 city" in html and "1 of 1 city on." in html
    assert 'action="/billing/checkout"' in html and 'min="1"' in html and 'max="9"' in html
    assert 'id="manage-billing"' not in html


def test_billing_page_free_with_customer_shows_checkout_and_manage_billing(web, monkeypatch):
    monkeypatch.setattr(billing, "configured", lambda: True)
    monkeypatch.setattr(billing, "fetch_current_subscription", lambda c, p=None: None)
    seed(web, email=EMAIL, status="canceled", cities={"ch": (1, "")})
    with sqlite3.connect(web) as c:
        c.execute("UPDATE users SET stripe_customer_id=? WHERE email=?", (CUST, EMAIL))
    client, _, _ = signed_in_client(web, EMAIL)
    html = client.get("/billing").text
    assert 'action="/billing/checkout"' in html and 'max="9"' in html
    assert 'id="manage-billing"' in html and 'target="_blank"' in html
    assert "Past receipts and your card are in the Stripe Customer Portal" in html


def test_billing_page_load_syncs_like_watch(web, stripe_api):
    seed_paid(web, extra=3)
    stripe_api.set("active", 3)
    client, _, _ = signed_in_client(web, EMAIL)    # /watch load syncs once
    assert stripe_api.calls == 1
    with sqlite3.connect(web) as c:
        c.execute("UPDATE users SET billing_synced_at=? WHERE email=?", (time.time() - 61, EMAIL))
    stripe_api.set("active", 1)
    html = client.get("/billing").text
    assert stripe_api.calls == 2 and user(web)["extra_cities"] == 1
    assert "Billing updated: 1 extra city · up to 2 cities." in html and "Switched off:" in html


def test_nav_has_billing_link_in_order_when_signed_in(web):
    seed(web, email=EMAIL, status=None, cities={"ch": (1, "")})
    client, _, html = signed_in_client(web, EMAIL)
    for page in (html, client.get("/billing").text):
        nav = re.search(r"<nav>.*?</nav>", page, re.S).group(0)
        assert '<a href="/billing" id="nav-billing">Billing</a>' in nav
        i = [nav.index(x) for x in ('href="/watch">My watch', 'href="/watch#history">History',
                                    'href="/billing" id="nav-billing">Billing', "Sign out")]
        assert i == sorted(i)
    anon = TestClient(webapp.app).get("/").text
    assert 'href="/billing"' not in re.search(r"<nav>.*?</nav>", anon, re.S).group(0)


def test_watch_page_has_no_billing_box_but_links_to_billing(web, stripe_api):
    seed_paid(web, extra=2, cities={"ch": (1, "100 Test St"), "sandy": (1, "200 Test Ave"), "draper": (1, "")})
    stripe_api.set("active", 2)
    _, _, html = signed_in_client(web, EMAIL)
    assert 'id="manage-billing"' not in html and "Manage billing</button>" not in html
    assert 'action="/billing/portal"' not in html and 'action="/billing/checkout"' not in html
    assert "Plan &amp; billing" not in html and 'id="billing"' not in html
    assert 'Your plan allows <strong>3 cities</strong>. Change it on <a href="/billing">Billing</a>.' in html
    assert 'action="/watch"' in html and 'id="history"' in html     # rest of My watch intact


def test_watch_page_free_plan_line_links_to_billing(web):
    seed(web, email=EMAIL, status=None, cities={"ch": (1, "")})
    _, _, html = signed_in_client(web, EMAIL)
    assert 'Your plan allows <strong>1 city</strong>. Change it on <a href="/billing">Billing</a>.' in html
    assert "Other cities stay disabled." in html
    assert 'id="manage-billing"' not in html and "Free: 1 city" not in html


def test_checkout_cancel_and_success_link_back_to_billing(web):
    seed(web, email=EMAIL, status=None, cities={"ch": (1, "")})
    client, _, _ = signed_in_client(web, EMAIL)
    for url in ("/billing/cancel", "/billing/success"):
        html = client.get(url).text
        assert '<a class="btn" href="/billing">Back to Billing</a>' in html
        assert "#billing" not in html and 'href="/watch">Back' not in html


# ------------------------------------------------------------ portal session
def test_portal_session_created_with_return_url_and_configuration(web, monkeypatch):
    seed_paid(web, extra=2)
    created = {}

    class Session:
        @staticmethod
        def create(**params):
            created.update(params)
            return types.SimpleNamespace(url="https://billing.stripe.test/p/session_fake")

    fake_stripe = types.SimpleNamespace(billing_portal=types.SimpleNamespace(Session=Session))
    monkeypatch.setattr(billing, "configured", lambda: True)
    monkeypatch.setattr(billing, "_client", lambda: fake_stripe)
    monkeypatch.setattr(billing, "fetch_current_subscription",
                        lambda c, p=None: {"id": "sub_fake_sync", "status": "active", "customer": CUST,
                                           "items": {"data": [{"quantity": 2}]}})
    monkeypatch.setenv("STRIPE_PORTAL_CONFIG_ID", "bpc_fake_config")
    client, csrf, _ = signed_in_client(web, EMAIL)
    r = client.post("/billing/portal", data={"csrf": csrf}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "https://billing.stripe.test/p/session_fake"
    assert created["customer"] == CUST
    assert created["configuration"] == "bpc_fake_config"
    assert created["return_url"].endswith("/billing/return")
    assert created["return_url"] == f"{webapp.base_url()}/billing/return"


def test_billing_return_requires_sign_in(web):
    r = TestClient(webapp.app).get("/billing/return", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/signin"
