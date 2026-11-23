"""New subscriber: is current_period_end stored on every Stripe path? (throwaway DB, fake Stripe, no network)."""
import sqlite3
import types

import pytest
from fastapi.testclient import TestClient

from web import app as webapp
from web import stripe_billing as billing
from tests.test_billing_portal_sync import web  # noqa: F401  (fixture)
from tests.test_web_watch import seed, signed_in_client

NEW = "test+newsub@example.invalid"
CUST = "cus_fake_newsub"
SUB = "sub_fake_newsub"
PE = 1794707703  # Nov 14 2026 18:55:03 MST


def sub_obj(shape, status="active", qty=1, cancel_at_period_end=False):
    """Subscription as Stripe sends it. 'old' (<= 2025-02-24.acacia): current_period_end on the
    subscription. 'new' (2025-03-31.basil+ / clover): only on items.data[]."""
    item = {"id": "si_fake", "quantity": qty, "price": {"id": "price_fake"}}
    s = {"id": SUB, "object": "subscription", "status": status, "customer": CUST,
         "metadata": {"email": NEW}, "cancel_at_period_end": cancel_at_period_end, "cancel_at": None,
         "items": {"data": [item]}}
    if shape == "old":
        s["current_period_end"] = PE
    else:
        item["current_period_end"] = PE
    return s


def period_end():
    with sqlite3.connect(webapp.DB) as c:
        return c.execute("SELECT period_end FROM users WHERE email=?", (NEW,)).fetchone()[0]


@pytest.fixture
def stripe_fake(monkeypatch, web):
    seed(web, email=NEW, status=None, cities={"ch": (1, "")})
    with sqlite3.connect(web) as c:
        c.execute("UPDATE users SET stripe_customer_id=? WHERE email=?", (CUST, NEW))
    state = {"sub": None, "event": None}
    monkeypatch.setattr(billing, "configured", lambda: True)
    monkeypatch.setattr(billing, "construct_event", lambda payload, sig: state["event"])
    monkeypatch.setattr(billing, "fetch_current_subscription", lambda cust, pref=None: state["sub"])
    fake_stripe = types.SimpleNamespace(
        Subscription=types.SimpleNamespace(retrieve=lambda sid: state["sub"]),
        Customer=types.SimpleNamespace(retrieve=lambda cid: types.SimpleNamespace(email=NEW)))
    monkeypatch.setattr(billing, "_client", lambda: fake_stripe)
    return state


def post(state, etype, obj):
    state["event"] = {"id": f"evt_{etype}", "type": etype, "data": {"object": obj}}
    r = TestClient(webapp.app).post("/stripe/webhook", content=b"{}", headers={"stripe-signature": "t"})
    assert r.status_code == 200, r.text


@pytest.mark.parametrize("shape", ["old", "new"])
def test_parser_reads_period_end_in_both_api_shapes(shape):
    assert billing.subscription_fields_from_obj(sub_obj(shape))["period_end"] == PE


@pytest.mark.parametrize("shape", ["old", "new"])
@pytest.mark.parametrize("etype", ["customer.subscription.created", "customer.subscription.updated"])
def test_subscription_webhooks_store_period_end(stripe_fake, shape, etype):
    stripe_fake["sub"] = sub_obj(shape)
    post(stripe_fake, etype, sub_obj(shape))
    assert period_end() == PE


@pytest.mark.parametrize("shape", ["old", "new"])
def test_checkout_session_completed_stores_period_end(stripe_fake, shape):
    stripe_fake["sub"] = sub_obj(shape)
    session = {"id": "cs_fake", "object": "checkout.session", "client_reference_id": NEW,
               "customer": CUST, "subscription": SUB, "payment_status": "paid", "metadata": {"email": NEW}}
    post(stripe_fake, "checkout.session.completed", session)
    assert period_end() == PE


@pytest.mark.parametrize("shape", ["old", "new"])
def test_new_subscriber_any_event_order_keeps_period_end(stripe_fake, shape):
    """Stripe does not guarantee order: subscription.created then checkout.session.completed must not wipe it."""
    stripe_fake["sub"] = sub_obj(shape)
    post(stripe_fake, "customer.subscription.created", sub_obj(shape))
    assert period_end() == PE
    session = {"id": "cs_fake", "object": "checkout.session", "client_reference_id": NEW,
               "customer": CUST, "subscription": SUB, "payment_status": "paid", "metadata": {"email": NEW}}
    post(stripe_fake, "checkout.session.completed", session)
    assert period_end() == PE
    # and the lock date follows the billing date, not the 1st of the month
    assert webapp.fmt_day(webapp.city_locks(NEW)[1].timestamp()) == "November 14, 2026"


def test_checkout_completed_keeps_pending_cancel(stripe_fake):
    stripe_fake["sub"] = sub_obj("new", cancel_at_period_end=True)
    session = {"id": "cs_fake", "client_reference_id": NEW, "customer": CUST, "subscription": SUB,
               "payment_status": "paid"}
    post(stripe_fake, "checkout.session.completed", session)
    with sqlite3.connect(webapp.DB) as c:
        assert c.execute("SELECT cancel_at FROM users WHERE email=?", (NEW,)).fetchone()[0] == PE


@pytest.mark.parametrize("shape", ["old", "new"])
def test_checkout_success_page_stores_period_end(stripe_fake, monkeypatch, shape):
    stripe_fake["sub"] = sub_obj(shape)
    sess = types.SimpleNamespace(id="cs_fake", client_reference_id=NEW, metadata={"email": NEW},
                                 customer_details=types.SimpleNamespace(email=NEW), customer=CUST,
                                 subscription=sub_obj(shape), payment_status="paid", amount_total=500,
                                 currency="usd")
    monkeypatch.setattr(billing, "retrieve_checkout_session", lambda sid: sess)
    c, _, _ = signed_in_client(webapp.DB, NEW)
    r = c.get("/billing/success?session_id=cs_fake")
    assert r.status_code == 200
    assert period_end() == PE


@pytest.mark.parametrize("shape", ["old", "new"])
def test_page_load_sync_stores_period_end(stripe_fake, shape):
    stripe_fake["sub"] = sub_obj(shape)
    c, _, _ = signed_in_client(webapp.DB, NEW)   # /watch load runs sync_billing
    assert period_end() == PE
