"""Stripe billing helpers for Agenda Watch.

Key mode is decided by where the app runs:
  * Cloud (AW_CLOUD=1, Cloudflare Containers): LIVE keys only (sk_live_/rk_live_, pk_live_); test keys refused.
  * Box / local / tests: TEST keys only (sk_test_/rk_test_, pk_test_); live keys refused.
In live mode the catalog (product, $5/mo price, portal configuration) is resolved by
ensure_live_catalog() and the ids are kept in the app DB (settings table) — STRIPE_PRICE_ID and
STRIPE_PORTAL_CONFIG_ID env values are ignored.

Env:
  STRIPE_SECRET_KEY       sk_test_... (box) | sk_live_/rk_live_... (cloud)
  STRIPE_PUBLISHABLE_KEY  pk_test_... (box) | pk_live_... (cloud)
  STRIPE_WEBHOOK_SECRET   whsec_...    (from `stripe listen` or Dashboard)
  STRIPE_PRICE_ID         price_...    (optional; must be the $5/mo price; auto-found/created if missing)

Product: "Extra city" — $5.00 USD / month per extra city, sold by subscription QUANTITY
(1..9 extra cities; free plan includes 1 city, so 1 + quantity cities, capped at 10).
Street is included with each city; never charged per street.
The legacy $15/month "Extra city or address" price is archived (inactive) and treated as
quantity-1 if any old subscription still uses it.
"""
from __future__ import annotations

import logging
import os
from typing import Any, Optional

log = logging.getLogger("agenda_watch.stripe")

PRODUCT_NAME = "Extra city"
PRODUCT_DESCRIPTION = "Agenda Watch: $5/month per extra city (street included with each city)."
PRICE_AMOUNT_CENTS = 500
PRICE_CURRENCY = "usd"
PRICE_INTERVAL = "month"
PRICE_LOOKUP_KEY = "agenda_watch_extra_city_monthly_500"
PRICE_LABEL = "$5/month per extra city"
MAX_EXTRA_CITIES = 9          # 1 free city + 9 extra = all 10 supported cities
LEGACY_PRODUCT_NAME = "Extra city or address"
LEGACY_PRICE_AMOUNT_CENTS = 1500

# Statuses that unlock the paid watch features
ACTIVE_STATUSES = frozenset({"active", "trialing"})


# Live catalog (resolved at startup in cloud; ids stored in the DB settings table, not env).
LIVE_PRODUCT_NAME = "Agenda Watch extra city"
LIVE_PRICE_LOOKUP_KEY = "aw_extra_city_monthly_v1"
# Card statement suffix for subscription charges: shortened descriptor + "* " + this, e.g. "QUAZAR16* AGWATCH".
LIVE_STATEMENT_DESCRIPTOR = "AGWATCH"
LIVE_RETURN_URL = "https://agendawatch.app/billing/return"
PORTAL_META = {"app": "agenda-watch", "purpose": "customer-portal"}
PORTAL_API_VERSION = "2025-10-29.clover"   # adjustable_quantity on portal products
# Populated by the app from settings (live mode only): {"price_id", "product_id", "portal_config_id"}
LIVE_IDS: dict = {}


class StripeConfigError(RuntimeError):
    pass


def required_mode() -> str:
    """'live' on Cloudflare (AW_CLOUD=1), 'test' everywhere else (box, local, tests)."""
    return "live" if os.environ.get("AW_CLOUD") == "1" else "test"


def is_live() -> bool:
    return required_mode() == "live"


def _secret() -> str:
    mode = required_mode()
    key = (os.environ.get("STRIPE_SECRET_KEY") or "").strip()
    if not key:
        raise StripeConfigError(f"STRIPE_SECRET_KEY is not set ({mode} key required).")
    if not (key.startswith(f"sk_{mode}_") or key.startswith(f"rk_{mode}_")):
        other = "test" if mode == "live" else "live"
        if key.startswith((f"sk_{other}_", f"rk_{other}_")):
            raise StripeConfigError(f"Stripe {other} secret key refused: this server requires {mode} mode.")
        raise StripeConfigError(f"STRIPE_SECRET_KEY must start with sk_{mode}_ or rk_{mode}_.")
    return key


def publishable_key() -> Optional[str]:
    mode = required_mode()
    key = (os.environ.get("STRIPE_PUBLISHABLE_KEY") or "").strip()
    if not key:
        return None
    if not key.startswith(f"pk_{mode}_"):
        raise StripeConfigError(f"STRIPE_PUBLISHABLE_KEY must start with pk_{mode}_ ({mode} mode).")
    return key


def webhook_secret() -> Optional[str]:
    return (os.environ.get("STRIPE_WEBHOOK_SECRET") or "").strip() or None


def configured() -> bool:
    try:
        _secret()
        return True
    except StripeConfigError:
        return False


def _client():
    import stripe
    stripe.api_key = _secret()
    return stripe


def _is_extra_city_price(price) -> bool:
    return bool(
        price is not None
        and getattr(price, "active", False)
        and price.recurring
        and price.recurring.get("interval") == PRICE_INTERVAL
        and price.unit_amount == PRICE_AMOUNT_CENTS
        and price.currency == PRICE_CURRENCY
    )


def ensure_live_catalog() -> dict:
    """LIVE mode: idempotently ensure product + $5/mo per-quantity price (by lookup_key) + Customer
    Portal configuration (by metadata). Safe to call on every startup. Returns the ids."""
    if not is_live():
        raise StripeConfigError("ensure_live_catalog is for live (cloud) mode only")
    stripe = _client()
    price = None
    found = stripe.Price.list(lookup_keys=[LIVE_PRICE_LOOKUP_KEY], active=True, limit=1, expand=["data.product"])
    if found.data and _is_extra_city_price(found.data[0]):
        price = found.data[0]
        product_id = price.product if isinstance(price.product, str) else price.product.id
    else:
        product = None
        for p in stripe.Product.list(active=True, limit=100).auto_paging_iter():
            md = p.metadata or {}
            if md.get("app") == "agenda-watch" and md.get("plan") == "extra_city":
                product = p
                break
        if product is None:
            product = stripe.Product.create(
                name=LIVE_PRODUCT_NAME, description=PRODUCT_DESCRIPTION,
                statement_descriptor=LIVE_STATEMENT_DESCRIPTOR,
                metadata={"app": "agenda-watch", "plan": "extra_city"})
            log.info("Created live Stripe product %s", product.id)
        product_id = product.id
        for pr in stripe.Price.list(product=product_id, active=True, limit=20).data:
            if _is_extra_city_price(pr):
                price = pr
                break
        if price is None:
            price = stripe.Price.create(
                product=product_id, unit_amount=PRICE_AMOUNT_CENTS, currency=PRICE_CURRENCY,
                recurring={"interval": PRICE_INTERVAL, "usage_type": "licensed"},
                lookup_key=LIVE_PRICE_LOOKUP_KEY, transfer_lookup_key=True, nickname=PRICE_LABEL,
                metadata={"app": "agenda-watch", "plan": "extra_city"})
            log.info("Created live Stripe price %s", price.id)
        elif price.lookup_key != LIVE_PRICE_LOOKUP_KEY:
            stripe.Price.modify(price.id, lookup_key=LIVE_PRICE_LOOKUP_KEY, transfer_lookup_key=True)
    ensure_product_statement_descriptor(product_id)
    opts = {"stripe_version": PORTAL_API_VERSION}
    params = dict(
        business_profile={"headline": "Agenda Watch"},
        default_return_url=LIVE_RETURN_URL,
        features={
            "invoice_history": {"enabled": True},
            "payment_method_update": {"enabled": True},
            "customer_update": {"enabled": False},
            # Cancel at the end of the paid period; no refunds.
            "subscription_cancel": {"enabled": True, "mode": "at_period_end", "proration_behavior": "none"},
            # Increases: prorated and invoiced/charged immediately (always_invoice).
            # Decreases: scheduled for the end of the paid period (no proration, no refund).
            "subscription_update": {
                "enabled": True, "default_allowed_updates": ["quantity"], "proration_behavior": "always_invoice",
                "schedule_at_period_end": {"conditions": [{"type": "decreasing_item_amount"}]},
                "products": [{"product": product_id, "prices": [price.id],
                              "adjustable_quantity": {"enabled": True, "minimum": 1,
                                                      "maximum": MAX_EXTRA_CITIES}}],
            },
        },
        metadata=PORTAL_META,
    )
    cfg = None
    for c in stripe.billing_portal.Configuration.list(active=True, limit=100, **opts).auto_paging_iter():
        md = c.metadata or {}
        if md.get("app") == PORTAL_META["app"] and md.get("purpose") == PORTAL_META["purpose"]:
            cfg = c
            break
    if cfg is None:
        cfg = stripe.billing_portal.Configuration.create(**params, **opts)
        log.info("Created live portal configuration %s", cfg.id)
    else:
        cfg = stripe.billing_portal.Configuration.modify(cfg.id, **params, **opts)
    ids = {"product_id": product_id, "price_id": price.id, "portal_config_id": cfg.id}
    LIVE_IDS.update(ids)
    return ids


def ensure_product_statement_descriptor(product_id: str):
    """Set/keep product.statement_descriptor = AGWATCH (live). Returns the product as read back."""
    stripe = _client()
    prod = stripe.Product.retrieve(product_id)
    if getattr(prod, "statement_descriptor", None) != LIVE_STATEMENT_DESCRIPTOR:
        stripe.Product.modify(product_id, statement_descriptor=LIVE_STATEMENT_DESCRIPTOR)
        prod = stripe.Product.retrieve(product_id)
        log.info("Set statement_descriptor on %s -> %s", product_id, prod.statement_descriptor)
    return prod


def ensure_price_id() -> str:
    """Return the active $5/mo extra-city price id. Live: the catalog id (settings), never env.
    Test: STRIPE_PRICE_ID / lookup / create in the test account."""
    if is_live():
        if not LIVE_IDS.get("price_id"):
            ensure_live_catalog()
        return LIVE_IDS["price_id"]
    stripe = _client()
    existing = (os.environ.get("STRIPE_PRICE_ID") or "").strip()
    if existing:
        try:
            if _is_extra_city_price(stripe.Price.retrieve(existing)):
                return existing
            log.warning("STRIPE_PRICE_ID %s is not an active $5/mo price; ignoring it", existing)
        except Exception as ex:  # noqa: BLE001
            log.warning("STRIPE_PRICE_ID %s not retrievable (%s); ignoring it", existing, ex)
    found = stripe.Price.list(lookup_keys=[PRICE_LOOKUP_KEY], active=True, limit=1)
    if found.data and _is_extra_city_price(found.data[0]):
        os.environ["STRIPE_PRICE_ID"] = found.data[0].id
        return found.data[0].id
    product = None
    for p in stripe.Product.list(active=True, limit=100).auto_paging_iter():
        if p.name == PRODUCT_NAME and (p.metadata or {}).get("app") == "agenda-watch":
            product = p
            break
    if product is None:
        product = stripe.Product.create(
            name=PRODUCT_NAME,
            description=PRODUCT_DESCRIPTION,
            metadata={"app": "agenda-watch", "plan": "extra_city"},
        )
        log.info("Created Stripe product %s (%s)", product.id, PRODUCT_NAME)
    for price in stripe.Price.list(product=product.id, active=True, limit=20).data:
        if _is_extra_city_price(price):
            os.environ["STRIPE_PRICE_ID"] = price.id
            return price.id
    price = stripe.Price.create(
        product=product.id,
        unit_amount=PRICE_AMOUNT_CENTS,
        currency=PRICE_CURRENCY,
        recurring={"interval": PRICE_INTERVAL, "usage_type": "licensed"},
        lookup_key=PRICE_LOOKUP_KEY,
        nickname=PRICE_LABEL,
        metadata={"app": "agenda-watch", "plan": "extra_city"},
    )
    os.environ["STRIPE_PRICE_ID"] = price.id
    log.info("Created Stripe price %s ($5/mo per extra city)", price.id)
    return price.id


def archive_legacy_prices() -> list:
    """Deactivate (archive, not delete) the old $15/mo 'Extra city or address' price(s)."""
    stripe = _client()
    archived = []
    for price in stripe.Price.list(active=True, limit=100).auto_paging_iter():
        if price.unit_amount != LEGACY_PRICE_AMOUNT_CENTS or not price.recurring:
            continue
        prod = stripe.Product.retrieve(price.product) if isinstance(price.product, str) else price.product
        if prod.name == LEGACY_PRODUCT_NAME or (price.metadata or {}).get("app") == "agenda-watch":
            stripe.Price.modify(price.id, active=False)
            archived.append(price.id)
    return archived


def move_subscription_to_extra_city_price(subscription_id: str, quantity: int = 1) -> Any:
    """Move an existing subscription's single item to the $5 price (no proration)."""
    stripe = _client()
    price_id = ensure_price_id()
    sub = stripe.Subscription.retrieve(subscription_id)
    items = sub["items"]["data"]
    if len(items) != 1:
        raise RuntimeError(f"expected 1 subscription item, found {len(items)}")
    return stripe.Subscription.modify(
        subscription_id,
        items=[{"id": items[0].id, "price": price_id, "quantity": int(quantity)}],
        proration_behavior="none",
    )


def clamp_quantity(q) -> int:
    try:
        q = int(q)
    except (TypeError, ValueError):
        q = 1
    return max(1, min(MAX_EXTRA_CITIES, q))


def extra_cities_from_subscription(sub: Any) -> Optional[int]:
    """Number of paid extra cities = total item quantity on the subscription.
    Legacy $15 items count as quantity 1 each (they were one extra city)."""
    if sub is None:
        return None
    try:
        items = sub["items"]["data"]
    except (KeyError, TypeError):
        items = getattr(getattr(sub, "items", None), "data", None)
    if not items:
        return None
    total = 0
    for it in items:
        q = it.get("quantity") if isinstance(it, dict) else getattr(it, "quantity", None)
        total += int(q or 1)
    return total


def is_paid_status(status: Optional[str]) -> bool:
    return bool(status and status.lower() in ACTIVE_STATUSES)


def get_or_create_customer(email: str, existing_customer_id: Optional[str] = None) -> str:
    stripe = _client()
    if existing_customer_id:
        try:
            c = stripe.Customer.retrieve(existing_customer_id)
            if not getattr(c, "deleted", False):
                return c.id
        except Exception:
            pass
    found = stripe.Customer.list(email=email, limit=1)
    if found.data:
        return found.data[0].id
    c = stripe.Customer.create(email=email, metadata={"app": "agenda-watch"})
    return c.id


def create_checkout_session(
    *,
    email: str,
    customer_id: str,
    success_url: str,
    cancel_url: str,
    quantity: int = 1,
) -> Any:
    stripe = _client()
    price_id = ensure_price_id()
    return stripe.checkout.Session.create(
        mode="subscription",
        customer=customer_id,
        client_reference_id=email,
        success_url=success_url,
        cancel_url=cancel_url,
        line_items=[{
            "price": price_id,
            "quantity": clamp_quantity(quantity),
            "adjustable_quantity": {"enabled": True, "minimum": 1, "maximum": MAX_EXTRA_CITIES},
        }],
        allow_promotion_codes=False,
        billing_address_collection="auto",
        metadata={"app": "agenda-watch", "email": email},
        subscription_data={"metadata": {"app": "agenda-watch", "email": email}},
    )


def portal_config_id() -> Optional[str]:
    """Customer Portal configuration id (scripts/ensure_portal_config.py writes it to .env).
    Read from the process env first, then .env, so a re-run takes effect without a restart."""
    if is_live():   # env holds the old TEST id; live uses the catalog id from settings only
        return LIVE_IDS.get("portal_config_id") or None
    v = (os.environ.get("STRIPE_PORTAL_CONFIG_ID") or "").strip()
    if v:
        return v
    from pathlib import Path
    env = Path(__file__).resolve().parents[1] / ".env"
    if env.is_file():
        for line in env.read_text().splitlines():
            if line.startswith("STRIPE_PORTAL_CONFIG_ID="):
                return line.split("=", 1)[1].strip() or None
    return None


def create_portal_session(*, customer_id: str, return_url: str, configuration: Optional[str] = None) -> Any:
    stripe = _client()
    params = {"customer": customer_id, "return_url": return_url}
    configuration = configuration or portal_config_id()
    if configuration:
        params["configuration"] = configuration
    return stripe.billing_portal.Session.create(**params)


# Subscription statuses that are "over" (no access; prefer any other subscription instead).
ENDED_STATUSES = frozenset({"canceled", "incomplete_expired"})


def fetch_current_subscription(customer_id: str, preferred_id: Optional[str] = None) -> Any:
    """The customer's subscription that decides access, read live from the Stripe API.
    Prefers an active/trialing one (newest first), then any not-ended one, then the stored
    subscription id, then the newest. Returns None when the customer has no subscriptions."""
    stripe = _client()
    subs = list(stripe.Subscription.list(customer=customer_id, status="all", limit=20).data)
    if not subs:
        return None
    subs.sort(key=lambda s: getattr(s, "created", 0) or 0, reverse=True)
    for pred in (lambda s: s.status in ACTIVE_STATUSES,
                 lambda s: s.status not in ENDED_STATUSES,
                 lambda s: preferred_id and s.id == preferred_id):
        for s in subs:
            if pred(s):
                return s
    return subs[0]


def retrieve_checkout_session(session_id: str) -> Any:
    stripe = _client()
    return stripe.checkout.Session.retrieve(
        session_id,
        expand=["subscription", "customer"],
    )


def construct_event(payload: bytes, sig_header: str):
    stripe = _client()
    secret = webhook_secret()
    if not secret:
        raise StripeConfigError(
            "STRIPE_WEBHOOK_SECRET is not set. Run `stripe listen --forward-to "
            "localhost:8080/stripe/webhook` and export the whsec_… value."
        )
    return stripe.Webhook.construct_event(payload, sig_header, secret)


def subscription_fields_from_obj(sub: Any) -> dict:
    """Normalize a Subscription object (or dict-like) into DB columns."""
    if sub is None:
        return {}
    if isinstance(sub, str):
        stripe = _client()
        sub = stripe.Subscription.retrieve(sub)
    status = getattr(sub, "status", None) or (sub.get("status") if isinstance(sub, dict) else None)
    sub_id = getattr(sub, "id", None) or (sub.get("id") if isinstance(sub, dict) else None)
    customer = getattr(sub, "customer", None) or (sub.get("customer") if isinstance(sub, dict) else None)
    if hasattr(customer, "id"):
        customer = customer.id
    def g(k):
        v = getattr(sub, k, None) if not isinstance(sub, dict) else sub.get(k)
        if v is None and not isinstance(sub, dict):
            try:
                v = sub.get(k)
            except Exception:  # noqa: BLE001
                v = None
        return v
    period_end = g("current_period_end")
    if period_end is None:   # newer API versions keep the period on the items
        try:
            items = sub["items"]["data"]
            period_end = max((it.get("current_period_end") or 0) for it in items) or None
        except Exception:  # noqa: BLE001
            period_end = None
    cancel_at = g("cancel_at")
    pending = bool(g("cancel_at_period_end")) or bool(cancel_at)
    return {
        "stripe_subscription_id": sub_id,
        "stripe_customer_id": customer,
        "subscription_status": status,
        "extra_cities": extra_cities_from_subscription(sub),
        "cancel_pending": pending,
        "cancel_at": (cancel_at or period_end) if pending else None,
        "period_end": period_end,
    }
