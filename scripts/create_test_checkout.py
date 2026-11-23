#!/usr/bin/env python3
"""Create a Stripe TEST Checkout session URL for an existing user email.

Usage:
  STRIPE_SECRET_KEY=sk_test_… python3 scripts/create_test_checkout.py owner@example.com

Does not charge until the card is entered on the Checkout page (use 4242…).
"""
import os, sys, sqlite3
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from web import stripe_billing as billing

DB = ROOT / "data" / "agenda_watch.db"
BASE = os.environ.get("AW_BASE_URL", "http://localhost:8080").rstrip("/")

def main():
    if len(sys.argv) < 2:
        print("usage: create_test_checkout.py <email>")
        return 2
    email = sys.argv[1].strip().lower()
    if not billing.configured():
        print("NEED_KEYS: STRIPE_SECRET_KEY missing (need sk_test_… via Secure Form).")
        return 2
    con = sqlite3.connect(DB); con.row_factory = sqlite3.Row
    u = con.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
    if not u:
        print(f"No user row for {email}. Sign in once via magic link first.")
        return 1
    cust = billing.get_or_create_customer(email, u["stripe_customer_id"] if u else None)
    con.execute("UPDATE users SET stripe_customer_id=? WHERE email=?", (cust, email))
    con.commit(); con.close()
    sess = billing.create_checkout_session(
        email=email, customer_id=cust,
        success_url=f"{BASE}/billing/success?session_id={{CHECKOUT_SESSION_ID}}",
        cancel_url=f"{BASE}/billing/cancel",
    )
    print(f"CUSTOMER={cust}")
    print(f"SESSION={sess.id}")
    print(f"URL={sess.url}")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
