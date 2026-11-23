"""Seventeen-city catalog watch storage + $5/extra-city plan limits (per-user cap stays 10 cities).

FAKE account (test+cities@example.invalid) in a throwaway SQLite DB only. No network, no
email, no Stripe calls.
"""
import re
import sqlite3

import pytest
from fastapi.testclient import TestClient

from test_web_watch import OWNER, KW, seed, row, signed_in_client  # noqa: F401  (also sets AW_DB)
from web import app as webapp
from web import stripe_billing as billing

TEN = {
    "ch": "100 Example St", "slc": "451 S State St", "sandy": "10000 S Centennial Pkwy",
    "draper": "1020 E Pioneer Rd", "wj": "8000 S Redwood Rd", "murray": "10 E 4800 S",
    "ogden": "2549 Washington Blvd", "layton": "437 N Wasatch Dr", "provo": "445 W Center St",
    "orem": "56 N State St",
}
# Added to the catalog 2026-10-21 (the per-user cap stays at 10 cities).
NEW = {
    "sj": "1600 W Towne Center Dr", "midvale": "7505 S Holden St", "farmington": "160 S Main St",
    "vineyard": "125 S Main St", "daniel": "2471 Daniels Rd",
}
ALL = {**TEN, **NEW}


@pytest.fixture
def web(sandbox, monkeypatch):
    db = sandbox.root / "data" / "web.db"
    monkeypatch.setattr(webapp, "DB", db)
    monkeypatch.setattr(webapp, "OUTBOX", sandbox.outbox / "magic")
    monkeypatch.setattr(webapp, "HISTORY_JSON", sandbox.state / "sent_history.json")
    webapp.init_db()
    return db


def form(csrf, cities, *, keywords=KW, alerts="1", **extra):
    """Full watch form as the browser sends it: checked cities only, every street field."""
    d = {"csrf": csrf, "cities_submitted": "1", "keywords_submitted": "1", "keywords": keywords,
         "alerts_submitted": "1"}
    if alerts:
        d["alerts_on"] = alerts
    for k in webapp.CITY_KEYS:
        on, street = cities.get(k, (False, ""))
        if on:
            d[f"city_{k}"] = "1"
        d[f"street_{k}"] = street
    d.update(extra)
    return d


def enabled(w):
    return [k for k in webapp.CITY_KEYS if w[f"city_{k}"]]


def test_supported_cities_are_exactly_the_seventeen_utah_cities_by_metro():
    assert webapp.CITY_KEYS == ["ch", "slc", "sandy", "draper", "wj", "murray", "sj", "midvale",
                                "ogden", "layton", "farmington", "provo", "orem", "vineyard", "daniel",
                                "parkcity", "stgeorge"]
    assert [(m, [c["name"] for c in cs]) for m, cs in webapp.METRO_GROUPS] == [
        ("Salt Lake metro", ["Cottonwood Heights", "Salt Lake City", "Sandy", "Draper", "West Jordan", "Murray",
                             "South Jordan", "Midvale"]),
        ("Ogden-Clearfield metro", ["Ogden", "Layton", "Farmington"]),
        ("Provo-Orem metro", ["Provo", "Orem", "Vineyard"]),
        ("Heber metro", ["Daniel", "Park City"]),
        ("St. George metro", ["St. George"]),
    ]
    names = {c["name"] for c in webapp.CITY_LIST}
    assert {"St. George", "Park City"} <= names
    assert {c["state"] for c in webapp.CITY_LIST} == {"Utah"}
    assert all(c["public_bodies"] for c in webapp.CFG["cities"].values())


def test_ten_cities_all_save_with_street_each_and_reload_keeps_each_street_on_its_city(web):
    seed(web, status="active", extra_cities=9, cities={"ch": (1, "")})
    client, csrf, _ = signed_in_client(web)
    r = client.post("/watch", data=form(csrf, {k: (True, s) for k, s in TEN.items()}), follow_redirects=False)
    assert r.status_code == 303, r.text
    w = row(web)
    assert enabled(w) == [k for k in webapp.CITY_KEYS if k in TEN]
    assert {k: w[f"street_{k}"] for k in TEN} == TEN
    # Reload (refresh) the page: every street field is on its own city row.
    page = client.get("/watch").text
    for k, street in TEN.items():
        m = re.search(rf'name="street_{k}"[^>]*value="([^"]*)"', page)
        assert m and m.group(1) == street, k
        assert re.search(rf'name="city_{k}"[^>]*checked', page), k
    # Re-saving the reloaded page unchanged keeps everything.
    r = client.post("/watch", data=form(csrf, {k: (True, s) for k, s in TEN.items()}), follow_redirects=False)
    assert r.status_code == 303 and row(web)["street_provo"] == TEN["provo"]


def test_free_account_can_pick_any_one_city_with_street(web):
    seed(web, status=None, cities={"ch": (1, "")})
    client, csrf, _ = signed_in_client(web)
    r = client.post("/watch", data=form(csrf, {"provo": (True, TEN["provo"])}), follow_redirects=False)
    assert r.status_code == 303
    w = row(web)
    assert enabled(w) == ["provo"] and w["street_provo"] == TEN["provo"] and w["street_ch"] == ""


@pytest.mark.parametrize("second", ["slc", "sandy", "orem"])
def test_free_account_cannot_enable_a_second_city_400_db_unchanged(web, second):
    seed(web, status=None, cities={"ch": (1, TEN["ch"])})
    before = row(web)
    client, csrf, html = signed_in_client(web)
    assert "const allowed=1;" in html
    r = client.post("/watch", data=form(csrf, {"ch": (True, TEN["ch"]), second: (True, TEN[second])}),
                    follow_redirects=False)
    assert r.status_code == 400
    assert "free plan watches one city" in r.text
    assert row(web) == before


@pytest.mark.parametrize("n", [1, 2, 5, 8])
def test_paid_quantity_n_allows_one_plus_n_cities_and_rejects_more(web, n):
    seed(web, status="active", extra_cities=n, cities={"ch": (1, "")})
    client, csrf, html = signed_in_client(web)
    assert f"const allowed={1 + n};" in html
    keys = webapp.CITY_KEYS
    ok = {k: (True, ALL[k]) for k in keys[: 1 + n]}
    r = client.post("/watch", data=form(csrf, ok), follow_redirects=False)
    assert r.status_code == 303
    saved = row(web)
    assert enabled(saved) == keys[: 1 + n]
    too_many = {k: (True, ALL[k]) for k in keys[: 2 + n]}
    r = client.post("/watch", data=form(csrf, too_many), follow_redirects=False)
    assert r.status_code == 400
    assert f"Your plan allows {1 + n} cities" in r.text
    assert row(web) == saved


def test_cap_is_ten_cities_even_with_larger_quantity_and_unknown_cities_ignored(web):
    seed(web, status="active", extra_cities=25, cities={"ch": (1, "")})
    assert webapp.allowed_cities(OWNER) == 10
    client, csrf, html = signed_in_client(web)
    assert "const allowed=10;" in html
    # 17 cities in the catalog, but a plan never allows more than 10 on at once.
    assert len(webapp.CITY_KEYS) == 17 and webapp.MAX_CITIES == 10
    eleven = {k: (True, ALL[k]) for k in webapp.CITY_KEYS[:11]}
    r = client.post("/watch", data=form(csrf, eleven), follow_redirects=False)
    assert r.status_code == 400 and "Your plan allows 10 cities" in r.text
    ten = {k: (True, ALL[k]) for k in webapp.CITY_KEYS[:10]}
    data = form(csrf, ten, city_boise="1", street_boise="1 Main St")
    r = client.post("/watch", data=data, follow_redirects=False)
    assert r.status_code == 303
    with sqlite3.connect(web) as c:
        keys = sorted(k for (k,) in c.execute("SELECT city_key FROM watch_cities WHERE email=?", (OWNER,)))
        on = sorted(k for (k,) in c.execute("SELECT city_key FROM watch_cities WHERE email=? AND enabled=1", (OWNER,)))
    assert keys == sorted(webapp.CITY_KEYS)  # never a city outside the supported catalog
    assert on == sorted(webapp.CITY_KEYS[:10])
    assert billing.clamp_quantity(50) == 9 and billing.clamp_quantity(0) == 1 and billing.clamp_quantity("x") == 1


def test_legacy_paid_row_without_quantity_counts_as_one_extra_city(web):
    seed(web, status="active", extra_cities=None)
    assert webapp.allowed_cities(OWNER) == 2
    seed(web, status="canceled", extra_cities=4)
    assert webapp.allowed_cities(OWNER) == 1


def test_subscription_quantity_read_from_subscription_object():
    sub = {"id": "sub_x", "status": "active", "customer": "cus_x",
           "items": {"data": [{"id": "si_x", "quantity": 3, "price": {"id": "price_x"}}]}}
    f = billing.subscription_fields_from_obj(sub)
    assert f == {"stripe_subscription_id": "sub_x", "stripe_customer_id": "cus_x",
                 "subscription_status": "active", "extra_cities": 3,
                 "cancel_pending": False, "cancel_at": None, "period_end": None}


def test_webhook_subscription_updated_records_quantity(web, monkeypatch):
    seed(web, status="active", extra_cities=1)
    with sqlite3.connect(web) as c:
        c.execute("UPDATE users SET stripe_customer_id='cus_fake' WHERE email=?", (OWNER,))
    event = {"id": "evt_1", "type": "customer.subscription.updated", "data": {"object": {
        "id": "sub_fake", "status": "active", "customer": "cus_fake", "metadata": {"email": OWNER},
        "items": {"data": [{"id": "si_1", "quantity": 4}]}}}}
    monkeypatch.setattr(billing, "configured", lambda: True)
    monkeypatch.setattr(billing, "construct_event", lambda payload, sig: event)
    r = TestClient(webapp.app).post("/stripe/webhook", content=b"{}", headers={"stripe-signature": "t"})
    assert r.status_code == 200
    assert webapp.allowed_cities(OWNER) == 5


def test_partial_saves_without_cities_alerts_or_keywords_keep_stored_values(web):
    seed(web, status="active", extra_cities=9, cities={k: (True, s) for k, s in TEN.items()},
         keywords="short-term rental\nSTR", alerts_on=1)
    client, csrf, _ = signed_in_client(web)
    base = row(web)
    # 1) no cities section (cities_submitted absent), only one stray city box
    r = client.post("/watch", data={"csrf": csrf, "city_ch": "1", "street_ch": "999 Other St",
                                    "keywords_submitted": "1", "keywords": "zoning",
                                    "alerts_submitted": "1", "alerts_on": "1"}, follow_redirects=False)
    assert r.status_code == 303
    w = row(web)
    assert enabled(w) == [k for k in webapp.CITY_KEYS if k in TEN] and {k: w[f"street_{k}"] for k in TEN} == TEN
    assert w["keywords"] == "zoning"
    # 2) no alerts section -> alerts stay on
    r = client.post("/watch", data={"csrf": csrf, "keywords_submitted": "1", "keywords": "STR"},
                    follow_redirects=False)
    assert r.status_code == 303 and row(web)["alerts_on"] == 1
    # 3) no keywords section -> keywords kept
    r = client.post("/watch", data={"csrf": csrf, "alerts_submitted": "1", "alerts_on": "1"},
                    follow_redirects=False)
    assert r.status_code == 303 and row(web)["keywords"] == "STR"
    w = row(web)
    assert {k: (w[f"city_{k}"], w[f"street_{k}"]) for k in TEN} == {k: (base[f"city_{k}"], s) for k, s in TEN.items()}


def test_full_form_unchecking_a_city_clears_only_its_street(web):
    seed(web, status="active", extra_cities=2,
         cities={"ch": (1, TEN["ch"]), "sandy": (1, TEN["sandy"]), "orem": (1, TEN["orem"])})
    client, csrf, _ = signed_in_client(web)
    r = client.post("/watch", data=form(csrf, {"ch": (True, TEN["ch"]), "sandy": (False, TEN["sandy"]),
                                               "orem": (True, TEN["orem"])}), follow_redirects=False)
    assert r.status_code == 303
    w = row(web)
    assert enabled(w) == ["ch", "orem"]
    assert (w["street_ch"], w["street_sandy"], w["street_orem"]) == (TEN["ch"], "", TEN["orem"])


def test_migration_from_fixed_columns_to_watch_cities_no_loss(sandbox, monkeypatch):
    db = sandbox.root / "data" / "legacy.db"
    with sqlite3.connect(db) as c:
        c.executescript("""
        CREATE TABLE users(email TEXT PRIMARY KEY, created_at TEXT NOT NULL);
        CREATE TABLE watches(email TEXT PRIMARY KEY REFERENCES users(email),
          city_ch INTEGER NOT NULL DEFAULT 1, city_slc INTEGER NOT NULL DEFAULT 0,
          keywords TEXT NOT NULL, address TEXT NOT NULL DEFAULT '',
          alerts_on INTEGER NOT NULL DEFAULT 0, updated_at TEXT NOT NULL, address2 TEXT NOT NULL DEFAULT '',
          street_ch TEXT NOT NULL DEFAULT '', street_slc TEXT NOT NULL DEFAULT '');
        """)
        c.execute("INSERT INTO watches VALUES(?,?,?,?,?,?,?,?,?,?)",
                  ("test+cities@example.invalid", 1, 1, KW, "", 1, "2026-10-01", "", "100 Example St", ""))
        c.execute("INSERT INTO watches VALUES(?,?,?,?,?,?,?,?,?,?)",
                  ("test+legacy@example.invalid", 0, 1, "STR", "", 0, "2026-10-01", "", "", "451 S State St"))
    monkeypatch.setattr(webapp, "DB", db)
    webapp.init_db()
    with sqlite3.connect(db) as c:
        cols = {r[1] for r in c.execute("PRAGMA table_info(watches)")}
        assert not cols & set(webapp.LEGACY_WATCH_COLUMNS)
        rows = sorted(c.execute("SELECT email, city_key, enabled, street FROM watch_cities"))
        kw = dict(c.execute("SELECT email, keywords FROM watches"))
    assert rows == [
        ("test+cities@example.invalid", "ch", 1, "100 Example St"),
        ("test+cities@example.invalid", "slc", 1, ""),
        ("test+legacy@example.invalid", "slc", 1, "451 S State St"),
    ]
    assert kw["test+cities@example.invalid"] == KW
    webapp.init_db()  # idempotent
    with sqlite3.connect(db) as c:
        assert sorted(c.execute("SELECT email, city_key, enabled, street FROM watch_cities")) == rows


def test_public_landing_price_line_and_ten_cities(web):
    html = TestClient(webapp.app).get("/").text
    assert "Add them for $5/month each, up to 10 cities total." in html
    for bad in ("$15", "extra city or address", "Extra city or address", "Stripe", "test mode"):
        assert bad not in html, bad
    for c in webapp.CITY_LIST:
        assert c["name"] in html
    for metro, _ in webapp.METRO_GROUPS:
        assert metro in html


def test_watch_page_lists_seventeen_city_rows_with_metro_headings_and_5_dollar_copy(web):
    seed(web, status=None, cities={"ch": (1, "")})
    _, _, html = signed_in_client(web)
    f = re.search(r'<form method="post" action="/watch".*?</form>', html, re.S).group(0)
    assert len(re.findall(r'type="checkbox" name="city_', f)) == 17
    assert len(re.findall(r'name="street_', f)) == 17
    assert f.count("Street to watch") == 17
    for metro, _ in webapp.METRO_GROUPS:
        assert metro in f
    assert "$15" not in html and "$5/month per extra city" in html
    for bad in ("zip", "Zip", "ZIP", "mailing", "name=\"state\""):
        assert bad not in f


def test_home_page_count_and_areas_come_from_the_catalog(web):
    import aw_cities
    html = TestClient(webapp.app).get("/").text
    assert aw_cities.area_phrase(webapp.CFG) == "Salt Lake, Ogden, Provo, Heber and St. George"
    assert "Upcoming rental items only — 17 Utah cities in the Salt Lake, Ogden, Provo, Heber and St. George areas." in html
    assert "Public meeting agendas and packets for 17 Utah cities:" in html
    assert "No other cities yet." not in html
    assert not re.search(r"\bten\b", html, re.I)
    assert "Add them for $5/month each, up to 10 cities total." in html
    listed = re.findall(r"<strong>([^<]+)</strong>",
                        re.search(r"Public meeting agendas.*?</ul>", html, re.S).group(0))
    assert listed == [c["name"] for c in webapp.CITY_LIST] and len(listed) == 17


def test_welcome_copy_free_first_city_and_no_signup_form(web):
    c = TestClient(webapp.app)
    home = c.get("/").text
    assert "<h2>Your first city is free.</h2>" in home
    assert '<a class="btn" href="/signin">Get started free</a>' in home
    assert '<a class="btn" href="/signin">Start free with your email</a>' in home
    assert ("No password and no sign-up form. Enter your email and we'll send you a link. "
            "New here? That link creates your free account.") in home
    assert "Sign in with email" not in home and '<a href="/signin">Sign in</a>' in home
    page = c.get("/signin").text
    assert "<h1>Sign in or create your free account</h1>" in page
    assert ">Email me a link</button>" in page and "New here? That link creates your free account." in page


def test_check_email_page_welcomes_new_and_returning_users(web, monkeypatch):
    monkeypatch.setenv("RESEND_API_KEY", "re_test_dummy")
    sent = []
    monkeypatch.setattr(webapp, "_send_via_resend",
                        lambda to, subject, body, redirect_owner=True, **k: (sent.append(to) or True, "sent via Resend (mock)"))
    c = TestClient(webapp.app)
    csrf = re.search(r'name="csrf" value="([^"]+)"', c.get("/signin").text).group(1)
    html = c.post("/signin", data={"csrf": csrf, "email": "test+welcome@example.invalid"}).text
    assert sent == ["test+welcome@example.invalid"]
    assert "<h1>Check your inbox</h1>" in html
    assert "The link signs you in, or sets up your free account if you're new." in html
    assert "We couldn't send" not in html
