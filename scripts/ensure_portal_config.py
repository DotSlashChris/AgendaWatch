#!/usr/bin/env python3
"""Idempotently create/update the Agenda Watch Stripe Customer Portal configuration (TEST MODE ONLY).

    .venv/bin/python scripts/ensure_portal_config.py

* Finds the configuration tagged metadata app=agenda-watch, purpose=customer-portal (creates it once),
  then updates it to the desired settings every run (safe to re-run).
* Enables: invoice history (receipts), payment method update, cancel (immediately, no proration),
  quantity changes on the $5/month "Extra city" price (1..9, no proration).
* Stripe does not allow setting is_default via the API, so the app passes this configuration id
  on every portal session. The id is written to .env as STRIPE_PORTAL_CONFIG_ID.
Refuses live keys.
"""
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

PRODUCT_ID = "prod_VPGyLuKFcyGPMx"
PRICE_ID = "price_1UOSQcGXwdHYjhWrqwH6KNQG"
RETURN_URL = "https://agendawatch.app/billing/return"
META = {"app": "agenda-watch", "purpose": "customer-portal"}
# adjustable_quantity on portal products needs a newer API version than the pinned SDK default.
API_VERSION = "2025-10-29.clover"


def load_env():
    env = ROOT / ".env"
    if env.is_file():
        for line in env.read_text().splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())


def desired():
    return dict(
        business_profile={"headline": "Agenda Watch"},
        default_return_url=RETURN_URL,
        features={
            "invoice_history": {"enabled": True},
            "payment_method_update": {"enabled": True},
            "customer_update": {"enabled": False},
            "subscription_cancel": {
                "enabled": True,
                "mode": "immediately",
                "proration_behavior": "none",
            },
            "subscription_update": {
                "enabled": True,
                "default_allowed_updates": ["quantity"],
                "proration_behavior": "none",
                "products": [{
                    "product": PRODUCT_ID,
                    "prices": [PRICE_ID],
                    "adjustable_quantity": {"enabled": True, "minimum": 1, "maximum": 9},
                }],
            },
        },
        metadata=META,
    )


def write_env_id(config_id):
    env = ROOT / ".env"
    lines = env.read_text().splitlines() if env.is_file() else []
    lines = [l for l in lines if not l.startswith("STRIPE_PORTAL_CONFIG_ID=")]
    lines.append(f"STRIPE_PORTAL_CONFIG_ID={config_id}")
    mode = env.stat().st_mode & 0o777 if env.is_file() else 0o600
    env.write_text("\n".join(lines) + "\n")
    env.chmod(mode)


def main():
    load_env()
    from web import stripe_billing as billing
    stripe = billing._client()          # raises on missing / live / non-test key
    assert stripe.api_key.startswith("sk_test_"), "test mode only"
    opts = {"stripe_version": API_VERSION}
    found = None
    for c in stripe.billing_portal.Configuration.list(active=True, limit=100, **opts).auto_paging_iter():
        md = c.metadata or {}
        if md.get("app") == META["app"] and md.get("purpose") == META["purpose"]:
            found = c
            break
    params = desired()
    if found is None:
        cfg = stripe.billing_portal.Configuration.create(**params, **opts)
        action = "created"
    else:
        cfg = stripe.billing_portal.Configuration.modify(found.id, **params, **opts)
        action = "updated"
    # products is expandable on newer API versions; re-read to verify what Stripe stored.
    cfg = stripe.billing_portal.Configuration.retrieve(
        cfg.id, expand=["features.subscription_update.products"], **opts)
    write_env_id(cfg.id)
    f = cfg.features
    prod = f.subscription_update.products[0]
    print(f"{action} portal configuration {cfg.id} (livemode={cfg.livemode}, is_default={cfg.is_default})")
    print(f"  headline={cfg.business_profile.headline!r} default_return_url={cfg.default_return_url}")
    print(f"  invoice_history={f.invoice_history.enabled} payment_method_update={f.payment_method_update.enabled}")
    print(f"  subscription_cancel enabled={f.subscription_cancel.enabled} mode={f.subscription_cancel.mode} "
          f"proration={f.subscription_cancel.proration_behavior}")
    print(f"  subscription_update enabled={f.subscription_update.enabled} "
          f"allowed={f.subscription_update.default_allowed_updates} proration={f.subscription_update.proration_behavior}")
    print(f"  product={prod.product} prices={prod.prices} adjustable_quantity={dict(prod.adjustable_quantity)}")
    print(f"  wrote STRIPE_PORTAL_CONFIG_ID={cfg.id} to .env")
    if cfg.livemode:
        raise SystemExit("ERROR: livemode configuration — should be impossible with sk_test_")


if __name__ == "__main__":
    main()
