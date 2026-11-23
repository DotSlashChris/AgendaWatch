"""Live-Stripe switch: key-mode guards, live catalog (idempotent), settings ids, test-id migration, copy."""
import re
import sqlite3
import types

import pytest
from fastapi.testclient import TestClient

from web import app as webapp
from web import stripe_billing as billing
from tests.test_web_watch import seed, row, signed_in_client, OWNER


@pytest.fixture
def web(sandbox, monkeypatch):
    db = sandbox.root / "data" / "web.db"
    monkeypatch.setattr(webapp, "DB", db)
    monkeypatch.setattr(webapp, "OUTBOX", sandbox.outbox / "magic")
    monkeypatch.setattr(webapp, "HISTORY_JSON", sandbox.state / "sent_history.json")
    monkeypatch.setattr(billing, "LIVE_IDS", {})
    webapp.init_db()
    return db


def keys(monkeypatch, sk, pk="", cloud=False):
    monkeypatch.setenv("STRIPE_SECRET_KEY", sk)
    monkeypatch.setenv("STRIPE_PUBLISHABLE_KEY", pk)
    if cloud:
        monkeypatch.setenv("AW_CLOUD", "1")
    else:
        monkeypatch.delenv("AW_CLOUD", raising=False)


# ------------------------------------------------------------------ key guards
@pytest.mark.parametrize("sk,pk", [("sk_live_x", "pk_live_y"), ("rk_live_x", "pk_live_y")])
def test_cloud_accepts_live_keys(monkeypatch, sk, pk):
    keys(monkeypatch, sk, pk, cloud=True)
    assert billing.required_mode() == "live" and billing.configured()
    assert billing._secret() == sk and billing.publishable_key() == pk


@pytest.mark.parametrize("sk", ["sk_test_x", "rk_test_x", "whatever"])
def test_cloud_refuses_test_secret(monkeypatch, sk):
    keys(monkeypatch, sk, "pk_live_y", cloud=True)
    assert not billing.configured()
    with pytest.raises(billing.StripeConfigError):
        billing._secret()


def test_cloud_refuses_test_publishable(monkeypatch):
    keys(monkeypatch, "sk_live_x", "pk_test_y", cloud=True)
    with pytest.raises(billing.StripeConfigError):
        billing.publishable_key()


@pytest.mark.parametrize("sk", ["sk_live_x", "rk_live_x"])
def test_box_still_refuses_live_and_accepts_test(monkeypatch, sk):
    keys(monkeypatch, sk, "pk_live_y")
    assert billing.required_mode() == "test" and not billing.configured()
    keys(monkeypatch, "sk_test_x", "pk_test_y")
    assert billing.configured() and billing.publishable_key() == "pk_test_y"


def test_live_ignores_env_portal_and_price_ids(monkeypatch):
    keys(monkeypatch, "sk_live_x", "pk_live_y", cloud=True)
    monkeypatch.setenv("STRIPE_PORTAL_CONFIG_ID", "bpc_TEST_old")
    monkeypatch.setenv("STRIPE_PRICE_ID", "price_TEST_old")
    monkeypatch.setattr(billing, "LIVE_IDS", {"price_id": "price_live_1", "portal_config_id": "bpc_live_1"})
    assert billing.portal_config_id() == "bpc_live_1"
    assert billing.ensure_price_id() == "price_live_1"
    monkeypatch.setattr(billing, "LIVE_IDS", {})
    assert billing.portal_config_id() is None


# ------------------------------------------------------------------ live catalog
class Obj(types.SimpleNamespace):
    def get(self, k, d=None):
        return getattr(self, k, d)


class Lst:
    def __init__(self, data):
        self.data = list(data)

    def auto_paging_iter(self):
        return iter(self.data)


class FakeStripeAccount:
    def __init__(self):
        self.products, self.prices, self.configs, self.creates = [], [], [], []
        acct = self

        class Product:
            @staticmethod
            def list(**kw):
                return Lst(p for p in acct.products if p.active)

            @staticmethod
            def create(**kw):
                acct.creates.append("product")
                p = Obj(id=f"prod_{len(acct.products)}", active=True, **kw)
                acct.products.append(p)
                return p

            @staticmethod
            def retrieve(pid):
                return next(p for p in acct.products if p.id == pid)

            @staticmethod
            def modify(pid, **kw):
                acct.creates.append("product_modify")
                p = next(p for p in acct.products if p.id == pid)
                for k, v in kw.items():
                    setattr(p, k, v)
                return p

        class Price:
            @staticmethod
            def list(lookup_keys=None, product=None, active=True, limit=10, expand=None):
                out = [p for p in acct.prices if p.active]
                if lookup_keys:
                    out = [p for p in out if p.lookup_key in lookup_keys]
                if product:
                    out = [p for p in out if p.product == product]
                return Lst(out)

            @staticmethod
            def create(product, unit_amount, currency, recurring, lookup_key, **kw):
                acct.creates.append("price")
                p = Obj(id=f"price_{len(acct.prices)}", active=True, product=product, unit_amount=unit_amount,
                        currency=currency, recurring=recurring, lookup_key=lookup_key)
                acct.prices.append(p)
                return p

            @staticmethod
            def modify(pid, **kw):
                acct.creates.append("price_modify")

        class Configuration:
            @staticmethod
            def list(**kw):
                return Lst(acct.configs)

            @staticmethod
            def create(**kw):
                acct.creates.append("portal")
                c = Obj(id=f"bpc_{len(acct.configs)}", **{k: v for k, v in kw.items() if k != "stripe_version"})
                acct.configs.append(c)
                return c

            @staticmethod
            def modify(cid, **kw):
                c = next(c for c in acct.configs if c.id == cid)
                for k, v in kw.items():
                    setattr(c, k, v)
                return c

        self.Product, self.Price = Product, Price
        self.billing_portal = types.SimpleNamespace(Configuration=Configuration)


def test_live_catalog_created_once_and_ids_stored_in_settings(web, monkeypatch):
    keys(monkeypatch, "sk_live_x", "pk_live_y", cloud=True)
    acct = FakeStripeAccount()
    monkeypatch.setattr(billing, "_client", lambda: acct)
    ids = webapp.ensure_live_catalog_now()
    assert acct.creates == ["product", "price", "portal"]
    price = acct.prices[0]
    assert (price.unit_amount, price.currency, price.recurring["interval"], price.lookup_key) == \
        (500, "usd", "month", "aw_extra_city_monthly_v1")
    assert acct.products[0].name == "Agenda Watch extra city"
    f = acct.configs[0].features
    assert f["subscription_cancel"] == {"enabled": True, "mode": "at_period_end", "proration_behavior": "none"}
    assert f["subscription_update"]["proration_behavior"] == "always_invoice"   # increases charged now
    assert f["subscription_update"]["schedule_at_period_end"] == {"conditions": [{"type": "decreasing_item_amount"}]}
    assert f["payment_method_update"]["enabled"] and f["invoice_history"]["enabled"]
    prod = f["subscription_update"]["products"][0]
    assert prod["prices"] == [price.id] and prod["adjustable_quantity"] == {"enabled": True, "minimum": 1, "maximum": 9}
    assert f["subscription_update"]["default_allowed_updates"] == ["quantity"]
    assert acct.configs[0].default_return_url == "https://agendawatch.app/billing/return"
    assert webapp.get_setting("stripe_live_price_id") == price.id
    assert webapp.get_setting("stripe_live_portal_config_id") == acct.configs[0].id
    # second startup: nothing new created, same ids
    assert webapp.ensure_live_catalog_now() == ids
    assert acct.creates == ["product", "price", "portal"]
    # fresh process: ids come from settings, env ids ignored
    monkeypatch.setattr(billing, "LIVE_IDS", {})
    monkeypatch.setenv("STRIPE_PORTAL_CONFIG_ID", "bpc_TEST_old")
    webapp.load_live_catalog_ids()
    assert billing.portal_config_id() == ids["portal_config_id"] and billing.ensure_price_id() == ids["price_id"]


def test_checkout_uses_live_price(web, monkeypatch):
    keys(monkeypatch, "sk_live_x", "pk_live_y", cloud=True)
    monkeypatch.setattr(billing, "LIVE_IDS", {"price_id": "price_live_1", "portal_config_id": "bpc_live_1"})
    seen = {}
    fake = types.SimpleNamespace(checkout=types.SimpleNamespace(Session=types.SimpleNamespace(
        create=lambda **kw: seen.update(kw) or Obj(id="cs_1", url="https://checkout.stripe.com/x"))))
    monkeypatch.setattr(billing, "_client", lambda: fake)
    billing.create_checkout_session(email="a@b.invalid", customer_id="cus_live", success_url="s", cancel_url="c", quantity=2)
    assert seen["line_items"][0]["price"] == "price_live_1" and seen["line_items"][0]["quantity"] == 2


# ------------------------------------------------------------------ migration
def seed_test_paid(db):
    seed(db, status="active", extra_cities=1, cities={"ch": (1, "100 Main St"), "slc": (1, "")})
    with sqlite3.connect(db) as c:
        c.execute("UPDATE users SET stripe_customer_id='cus_TEST1', stripe_subscription_id='sub_TEST1' WHERE email=?",
                  (OWNER,))


def users(db):
    with sqlite3.connect(db) as c:
        c.row_factory = sqlite3.Row
        return {r["email"]: dict(r) for r in c.execute("SELECT * FROM users")}


def test_live_switch_archives_test_ids_and_keeps_cities(web, monkeypatch):
    seed_test_paid(web)
    before = row(web)
    keys(monkeypatch, "sk_live_x", "pk_live_y", cloud=True)
    webapp.init_db()
    u = users(web)[OWNER]
    assert (u["stripe_customer_id"], u["stripe_subscription_id"], u["subscription_status"], u["extra_cities"]) == \
        (None, None, None, 0)
    assert (u["test_stripe_customer_id"], u["test_stripe_subscription_id"], u["test_subscription_status"],
            u["test_extra_cities"]) == ("cus_TEST1", "sub_TEST1", "active", 1)
    assert row(web) == before                      # CH + SLC still on, streets kept
    assert webapp.allowed_cities(OWNER) == 1        # but plan is now free
    # idempotent: a later live subscription is not wiped by a restart
    webapp.mark_user_paid(OWNER, customer_id="cus_LIVE", subscription_id="sub_LIVE", status="active", extra_cities=1)
    webapp.init_db()
    u = users(web)[OWNER]
    assert u["stripe_customer_id"] == "cus_LIVE" and u["test_stripe_customer_id"] == "cus_TEST1"


def test_no_migration_on_box_or_with_test_keys_in_cloud(web, monkeypatch):
    seed_test_paid(web)
    keys(monkeypatch, "sk_test_x", "pk_test_y")
    webapp.init_db()
    keys(monkeypatch, "sk_test_x", "pk_test_y", cloud=True)
    webapp.init_db()
    assert users(web)[OWNER]["stripe_customer_id"] == "cus_TEST1"
    with sqlite3.connect(web) as c:
        assert not c.execute("SELECT 1 FROM schema_migrations WHERE id='stripe_live_v1'").fetchone()


def test_after_switch_over_limit_save_is_refused_not_auto_disabled(web, monkeypatch):
    seed_test_paid(web)
    keys(monkeypatch, "sk_live_x", "pk_live_y", cloud=True)
    webapp.init_db()
    monkeypatch.delenv("AW_CLOUD")                 # rest of the request path as on the box
    monkeypatch.setattr(billing, "configured", lambda: False)
    client, csrf, page = signed_in_client(web)
    assert row(web)["city_ch"] == 1 and row(web)["city_slc"] == 1   # page load did not disable
    r = client.post("/watch", data={"csrf": csrf, "cities_submitted": "1", "city_ch": "1", "city_slc": "1",
                                    "street_ch": "100 Main St", "street_slc": ""}, follow_redirects=True)
    assert "Your plan allows 1 cities" in r.text or "plan allows" in r.text
    assert row(web)["city_slc"] == 1                 # refused save leaves data as it was


# ------------------------------------------------------------------ copy
BANNED = re.compile(r"no charge yet|test mode|test checkout|4242|sk_test|pk_test|stripe test|\btest\b", re.I)


def test_no_test_wording_on_public_and_billing_pages(web, monkeypatch):
    seed(web, status=None, extra_cities=0, cities={"ch": (1, "")})
    client, csrf, page = signed_in_client(web)
    for path in ("/", "/billing", "/billing/cancel", "/watch", "/signin", "/feedback"):
        html = client.get(path).text
        visible = re.sub(r"<[^>]+>", " ", re.sub(r"(?s)<(style|script)\b.*?</\1>", " ", html))
        m = BANNED.search(visible.replace(OWNER, ""))
        assert not m, (path, visible[max(0, m.start() - 150):m.end() + 60])
    assert "Add them for $5/month each, up to 10 cities total." in TestClient(webapp.app).get("/").text


def test_templates_have_no_test_wording():
    from pathlib import Path
    for p in (Path(webapp.__file__).parent / "templates").glob("*.html"):
        assert not BANNED.search(p.read_text().replace("build_tests", "")), p.name


def test_live_catalog_sets_and_keeps_statement_descriptor(web, monkeypatch):
    keys(monkeypatch, "sk_live_x", "pk_live_y", cloud=True)
    acct = FakeStripeAccount()
    monkeypatch.setattr(billing, "_client", lambda: acct)
    webapp.ensure_live_catalog_now()
    assert acct.products[0].statement_descriptor == "AGWATCH"
    assert "product_modify" not in acct.creates           # set at create, no extra update
    webapp.ensure_live_catalog_now()
    assert "product_modify" not in acct.creates           # unchanged -> untouched
    acct.products[0].statement_descriptor = None          # dropped (e.g. edited in Dashboard)
    webapp.ensure_live_catalog_now()
    assert acct.products[0].statement_descriptor == "AGWATCH" and acct.creates.count("product_modify") == 1
