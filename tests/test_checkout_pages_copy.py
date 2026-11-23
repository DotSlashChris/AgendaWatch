"""Checkout success / cancel page copy matches the current plan (Billing page wording).

Fake accounts, throwaway SQLite DB (sandbox), Stripe Checkout session mocked (no network)."""
import types

import pytest

from web import app as webapp
from web import stripe_billing as billing
from tests.test_web_watch import seed, signed_in_client

EMAIL = "test+checkout@example.invalid"
FORBIDDEN = ("test mode", "TEST", "sandbox", "4242", "second address", "one city + one address", "$15",
             "second city", "Test receipt", "Stripe Dashboard")


@pytest.fixture
def web(sandbox, monkeypatch):
    db = sandbox.root / "data" / "web.db"
    monkeypatch.setattr(webapp, "DB", db)
    monkeypatch.setattr(webapp, "ROOT", sandbox.root)          # proof file lands in the sandbox
    monkeypatch.setattr(webapp, "OUTBOX", sandbox.outbox / "magic")
    monkeypatch.setattr(webapp, "HISTORY_JSON", sandbox.state / "sent_history.json")
    webapp.init_db()
    return db


def visible(html):
    """Page content only (between header and footer)."""
    return html[html.index("</header>"):html.index("<footer>")]


def assert_clean(html):
    body = visible(html)
    for bad in FORBIDDEN:
        assert bad not in body, bad
    assert "test mode" not in body.lower() and "address" not in body.lower()


def fake_session(quantity):
    sub = {"id": "sub_fake_co", "status": "active", "customer": "cus_fake_co",
           "items": {"data": [{"id": "si_fake", "quantity": quantity}]}}
    return types.SimpleNamespace(
        id="cs_fake_co", client_reference_id=EMAIL, metadata={"email": EMAIL},
        customer_details=types.SimpleNamespace(email=EMAIL), customer="cus_fake_co",
        subscription=sub, payment_status="paid", amount_total=500 * quantity, currency="usd")


@pytest.mark.parametrize("n", [1, 3, 9])
def test_success_page_confirms_quantity_cities_price_and_links(web, monkeypatch, n):
    seed(web, email=EMAIL, status=None, cities={"ch": (1, "")})
    client, _, _ = signed_in_client(web, EMAIL)
    monkeypatch.setattr(billing, "configured", lambda: True)
    monkeypatch.setattr(billing, "retrieve_checkout_session", lambda sid: fake_session(n))
    monkeypatch.setattr(billing, "fetch_current_subscription", lambda c, p=None: fake_session(n).subscription)
    r = client.get("/billing/success?session_id=cs_fake_co")
    assert r.status_code == 200
    html = r.text
    noun = "city" if n == 1 else "cities"
    assert "<h1>Checkout complete</h1>" in html and "Payment confirmed." in html
    assert f"<strong>{n} extra {noun} · up to {1 + n} cities</strong>" in html
    assert "Each city includes its own street." in html
    assert f"<li>Extra cities: {n}</li>" in html
    assert f"<li>Cities allowed: up to {1 + n} (1 free + {n} extra)</li>" in html
    assert f"<li>Price: $5/month per extra city · {n} × $5/month</li>" in html
    assert "<li>Payment: paid</li>" in html
    assert '<a class="btn" href="/billing">Back to Billing</a>' in html
    assert 'href="/watch"' in visible(html) and "turn on your new cities" in html
    assert "Manage billing" in html
    assert_clean(html)
    assert webapp.allowed_cities(EMAIL) == 1 + n                # existing logic unchanged


def test_success_page_without_session_id_is_clean(web):
    seed(web, email=EMAIL, status=None, cities={"ch": (1, "")})
    client, _, _ = signed_in_client(web, EMAIL)
    html = client.get("/billing/success").text
    assert "We couldn't find this checkout." in html and "session_id" not in visible(html)
    assert '<a class="btn" href="/billing">Back to Billing</a>' in html
    assert_clean(html)


def test_cancel_page_copy(web):
    seed(web, email=EMAIL, status=None, cities={"ch": (1, "")})
    client, _, _ = signed_in_client(web, EMAIL)
    html = client.get("/billing/cancel").text
    assert "<h1>Checkout canceled</h1>" in html
    assert "No charge was made." in html
    assert "Your account stays on <strong>Free: 1 city</strong> — one city and one optional street on that city." in html
    assert "Extra cities are $5/month each, up to 9 extra (10 cities total). Each city includes its own street." in html
    assert '<a class="btn" href="/billing">Back to Billing</a>' in html
    assert_clean(html)


def test_success_and_cancel_require_sign_in(web):
    from fastapi.testclient import TestClient
    c = TestClient(webapp.app)
    for url in ("/billing/success", "/billing/cancel"):
        r = c.get(url, follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/signin"
