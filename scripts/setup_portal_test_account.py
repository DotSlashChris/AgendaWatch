#!/usr/bin/env python3
"""Idempotently prepare the FAKE Stripe-TEST account used for the Customer Portal walkthrough.

    .venv/bin/python scripts/setup_portal_test_account.py

* Stripe TEST mode only (refuses live keys). Never touches the owner account.
* Customer test+portal@example.invalid with pm_card_visa attached as default payment method,
  one active subscription on the $5/month extra-city price, quantity 2 (reused if it exists).
* Live DB (data/agenda_watch.db): users row (customer/sub, active, extra_cities=2), watch with
  alerts OFF and 3 cities on: Cottonwood Heights '100 Test St', Sandy '200 Test Ave', Draper ''.
Sends no email.
"""
import os
import sqlite3
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

EMAIL = "test+portal@example.invalid"
PRICE_ID = "price_1UOSQcGXwdHYjhWrqwH6KNQG"
QUANTITY = 2
CITIES = [("ch", "100 Test St"), ("sandy", "200 Test Ave"), ("draper", "")]  # switch-on order


def load_env():
    env = ROOT / ".env"
    if env.is_file():
        for line in env.read_text().splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())


def main():
    load_env()
    from web import app as webapp
    from web import stripe_billing as billing
    assert EMAIL != webapp.OWNER
    stripe = billing._client()                      # refuses live / non-test keys
    assert stripe.api_key.startswith("sk_test_")

    found = stripe.Customer.list(email=EMAIL, limit=10).data
    cust = next((c for c in found if (c.metadata or {}).get("app") == "agenda-watch"), None)
    if cust is None:
        cust = stripe.Customer.create(email=EMAIL, name="Portal Test (fake)",
                                      metadata={"app": "agenda-watch", "purpose": "portal-walkthrough"})
    pm_id = (cust.invoice_settings or {}).get("default_payment_method")
    if not pm_id:
        pm = stripe.PaymentMethod.attach("pm_card_visa", customer=cust.id)
        stripe.Customer.modify(cust.id, invoice_settings={"default_payment_method": pm.id})
        pm_id = pm.id
    subs = [s for s in stripe.Subscription.list(customer=cust.id, status="all", limit=20).data
            if s.status in ("active", "trialing")]
    sub = subs[0] if subs else stripe.Subscription.create(
        customer=cust.id, items=[{"price": PRICE_ID, "quantity": QUANTITY}],
        default_payment_method=pm_id,
        metadata={"app": "agenda-watch", "email": EMAIL, "purpose": "portal-walkthrough"})
    f = billing.subscription_fields_from_obj(sub)
    assert not sub.livemode

    now = datetime.now(webapp.TZ)
    ts = now.isoformat(timespec="seconds")
    with sqlite3.connect(webapp.DB) as c:
        c.execute("INSERT OR IGNORE INTO users(email,created_at) VALUES(?,?)", (EMAIL, ts))
        c.execute("UPDATE users SET stripe_customer_id=?, stripe_subscription_id=?, subscription_status=?, "
                  "extra_cities=?, paid_updated_at=?, billing_synced_at=? WHERE email=?",
                  (cust.id, f["stripe_subscription_id"], f["subscription_status"], f["extra_cities"], ts,
                   time.time(), EMAIL))
        c.execute("INSERT INTO watches(email,keywords,alerts_on,updated_at) VALUES(?,?,0,?) "
                  "ON CONFLICT(email) DO UPDATE SET alerts_on=0, updated_at=excluded.updated_at",
                  (EMAIL, "\n".join(webapp.DEFAULT_KEYWORDS), ts))
        for i, (key, street) in enumerate(CITIES):
            on_at = (now - timedelta(minutes=10 - i)).isoformat(timespec="seconds")
            c.execute("INSERT INTO watch_cities(email,city_key,enabled,street,updated_at,enabled_at) "
                      "VALUES(?,?,1,?,?,?) ON CONFLICT(email,city_key) DO UPDATE SET enabled=1, "
                      "street=excluded.street, updated_at=excluded.updated_at, enabled_at=excluded.enabled_at",
                      (EMAIL, key, street, ts, on_at))
        keep = tuple(k for k, _ in CITIES)
        c.execute(f"UPDATE watch_cities SET enabled=0, enabled_at=NULL WHERE email=? AND city_key NOT IN "
                  f"({','.join('?' * len(keep))})", (EMAIL, *keep))
    print(f"customer={cust.id} subscription={f['stripe_subscription_id']} status={f['subscription_status']} "
          f"quantity={f['extra_cities']} default_pm={pm_id} livemode={sub.livemode}")
    w = webapp.load_watch(EMAIL)
    print("watch alerts_on=%s cities on: %s" % (w["alerts_on"], ", ".join(
        f"{webapp.CITY_NAME[k]} [{v['street']!r}]" for k, v in w["cities"].items() if v["enabled"])))


if __name__ == "__main__":
    main()
