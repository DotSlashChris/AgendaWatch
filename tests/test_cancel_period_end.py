"""Cancel at period end: stays paid until Stripe ends the sub; webhook uses the CURRENT sub; copy."""
from fastapi.testclient import TestClient

from web import app as webapp
from web import stripe_billing as billing
from tests.test_billing_portal_sync import (  # noqa: F401  (fixtures)
    web, stripe_api, seed_paid, user, on, streets, EMAIL, CUST, STREETS)
from tests.test_web_watch import signed_in_client

PERIOD_END = 1794700800  # 2026-11-14 ~ America/Denver


def webhook(monkeypatch, etype, obj):
    event = {"id": "evt_x", "type": etype, "data": {"object": obj}}
    monkeypatch.setattr(billing, "construct_event", lambda payload, sig: event)
    r = TestClient(webapp.app).post("/stripe/webhook", content=b"{}", headers={"stripe-signature": "t"})
    assert r.status_code == 200


def pending(stripe_api, quantity=1, sub_id="sub_fake_sync"):
    stripe_api.set("active", quantity, sub_id)
    stripe_api.sub.update(cancel_at_period_end=True, cancel_at=PERIOD_END, current_period_end=PERIOD_END)
    return stripe_api.sub


def test_cancel_pending_stays_paid_and_billing_page_says_so(web, stripe_api, monkeypatch):
    seed_paid(web, extra=1, cities={"ch": (1, "100 Test St"), "slc": (1, "")})
    webhook(monkeypatch, "customer.subscription.updated", pending(stripe_api))
    u = user(web)
    assert u["subscription_status"] == "active" and u["extra_cities"] == 1 and u["cancel_at"] == PERIOD_END
    assert on(web) == ["ch", "slc"]
    client, csrf, _ = signed_in_client(web, EMAIL)
    html = client.get("/billing").text
    date = webapp.fmt_day(PERIOD_END)
    assert f"Cancels on {date}." in html and "Your extra city stays active until then." in html
    assert "Keep my subscription" in html and "Renew subscription" in html
    assert "Renews on" not in html


def test_period_end_deleted_downgrades_keeps_earliest_and_streets(web, stripe_api, monkeypatch):
    seed_paid(web, extra=3, enabled_at={"ch": "2026-10-01", "sandy": "2026-10-02",
                                         "draper": "2026-10-03", "provo": "2026-10-04"})
    webhook(monkeypatch, "customer.subscription.updated", pending(stripe_api, 3))
    assert user(web)["extra_cities"] == 3 and len(on(web)) == 4
    stripe_api.set("canceled", 3)
    webhook(monkeypatch, "customer.subscription.deleted", stripe_api.sub)
    u = user(web)
    assert u["extra_cities"] == 0 and u["cancel_at"] is None and u["subscription_status"] == "canceled"
    assert on(web) == ["ch"] and streets(web) == STREETS


def test_resume_clears_pending(web, stripe_api, monkeypatch):
    seed_paid(web, extra=1)
    webhook(monkeypatch, "customer.subscription.updated", pending(stripe_api))
    stripe_api.set("active", 1)
    stripe_api.sub.update(cancel_at_period_end=False, cancel_at=None, current_period_end=PERIOD_END)
    webhook(monkeypatch, "customer.subscription.updated", stripe_api.sub)
    u = user(web)
    assert u["cancel_at"] is None and u["period_end"] == PERIOD_END and u["extra_cities"] == 1


def test_deleted_event_for_old_sub_does_not_downgrade_current_active(web, stripe_api, monkeypatch):
    seed_paid(web, extra=1, cities={"ch": (1, ""), "slc": (1, "")})
    stripe_api.set("active", 1, sub_id="sub_second")          # customer's current sub
    old = {"id": "sub_first", "status": "canceled", "customer": CUST, "metadata": {"email": EMAIL},
           "items": {"data": [{"id": "si_old", "quantity": 1}]}}
    webhook(monkeypatch, "customer.subscription.deleted", old)
    u = user(web)
    assert u["subscription_status"] == "active" and u["extra_cities"] == 1
    assert u["stripe_subscription_id"] == "sub_second" and on(web) == ["ch", "slc"]


def test_scheduled_quantity_decrease_keeps_current_quantity_until_period_end(web, stripe_api, monkeypatch):
    # Portal schedules the decrease (subscription schedule); the sub keeps quantity 3 until renewal.
    seed_paid(web, extra=3)
    stripe_api.set("active", 3)
    stripe_api.sub.update(schedule="sub_sched_1", current_period_end=PERIOD_END)
    webhook(monkeypatch, "customer.subscription.updated", stripe_api.sub)
    assert user(web)["extra_cities"] == 3 and len(on(web)) == 4
    stripe_api.set("active", 1)                                # renewal applied the schedule
    webhook(monkeypatch, "customer.subscription.updated", stripe_api.sub)
    assert user(web)["extra_cities"] == 1 and len(on(web)) == 2 and streets(web) == STREETS


def test_landing_and_pages_copy(web):
    c = TestClient(webapp.app)
    html = c.get("/").text
    assert "<h2>Your first city is free.</h2>" in html
    assert ("Watch one city and one street at no cost, with no card needed. Want more cities? Add them for "
            "$5/month each, up to 10 cities total. Cancel anytime; extra cities stay active through the month you paid for.") in html


def test_paid_billing_page_explains_proration_and_period_end(web, stripe_api, monkeypatch):
    # throwaway test user only (never the owner's watch)
    seed_paid(web, extra=1, cities={"ch": (1, ""), "slc": (1, "")})
    stripe_api.set("active", 1)
    stripe_api.sub.update(current_period_end=PERIOD_END)
    client, csrf, _ = signed_in_client(web, EMAIL)
    html = client.get("/billing").text
    assert "Adding a city charges the prorated amount for the rest of this month now." in html
    assert "Canceling or lowering the number of extra cities takes effect at the end of the month you've paid for." in html
    assert f"Renews on {webapp.fmt_day(PERIOD_END)}." in html and "Cancels on" not in html
