#!/usr/bin/env python3
"""Ensure the Stripe TEST catalog for Agenda Watch; print the price id.

  - Product "Extra city", price $5.00 USD / month, used with quantity (1..9 extra cities).
  - --archive-legacy : deactivate (archive, never delete) the old $15/mo "Extra city or address" price.

Requires STRIPE_SECRET_KEY=sk_test_... in the environment (never paste in chat). Live keys refused.
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from web import stripe_billing as billing


def main():
    if not billing.configured():
        print("NEED_KEYS: set STRIPE_SECRET_KEY (sk_test_...) via Secure Form / env.")
        return 2
    price_id = billing.ensure_price_id()
    stripe = billing._client()
    price = stripe.Price.retrieve(price_id, expand=["product"])
    print(f"PRODUCT={price.product.name} ({price.product.id})")
    print(f"PRICE_ID={price_id}")
    print(f"AMOUNT={price.unit_amount} {price.currency.upper()}/{price.recurring['interval']} "
          f"active={price.active} livemode={price.livemode} lookup_key={price.lookup_key}")
    if "--archive-legacy" in sys.argv:
        archived = billing.archive_legacy_prices()
        print(f"ARCHIVED_LEGACY={','.join(archived) or '(none active)'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
