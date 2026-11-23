"""Web /watch save semantics: nothing may turn a city off or drop a street unless the
user submitted the full watch form with that city unchecked.

Runs against a throwaway SQLite DB (AW_DB) — never data/agenda_watch.db. No network/email.
Storage is watches (keywords/alerts) + watch_cities (one row per city); row() flattens it to
city_<key>/street_<key> so assertions read like the form fields.
"""
import os
import re
import sqlite3
import tempfile
import time
from pathlib import Path

import pytest

# Must be set before web.app is imported (it runs init_db() at import time).
os.environ["AW_DB"] = str(Path(tempfile.mkdtemp(prefix="aw-web-test-")) / "import.db")

from fastapi.testclient import TestClient  # noqa: E402

import agenda_watch as aw  # noqa: E402
from web import app as webapp  # noqa: E402

# Web save tests use a FAKE account in a throwaway DB — never the owner's real watch.
OWNER = "test+cities@example.invalid"
STREET_CH = "100 Example St"
KW = "short-term rental\nSTR"


@pytest.fixture
def web(sandbox, monkeypatch):
    db = sandbox.root / "data" / "web.db"
    monkeypatch.setattr(webapp, "DB", db)
    monkeypatch.setattr(webapp, "OUTBOX", sandbox.outbox / "magic")
    monkeypatch.setattr(webapp, "HISTORY_JSON", sandbox.state / "sent_history.json")
    webapp.init_db()
    return db


def seed(db, *, status="active", city_ch=1, city_slc=1, street_ch=STREET_CH, street_slc="",
         keywords=KW, alerts_on=1, cities=None, extra_cities=None, email=OWNER):
    """cities: {key: (enabled, street)} overrides the ch/slc kwargs when given."""
    if cities is None:
        cities = {"ch": (city_ch, street_ch), "slc": (city_slc, street_slc)}
    with sqlite3.connect(db) as c:
        c.execute("INSERT OR REPLACE INTO users(email,created_at,subscription_status,extra_cities) "
                  "VALUES(?,?,?,?)", (email, "2026-10-01T00:00:00-06:00", status, extra_cities))
        c.execute(
            "INSERT OR REPLACE INTO watches(email,keywords,alerts_on,updated_at) VALUES(?,?,?,?)",
            (email, keywords, alerts_on, "2026-10-01T00:00:00-06:00"),
        )
        c.execute("DELETE FROM watch_cities WHERE email=?", (email,))
        for key, (on, street) in cities.items():
            c.execute("INSERT INTO watch_cities(email,city_key,enabled,street,updated_at) VALUES(?,?,?,?,?)",
                      (email, key, int(bool(on)), street or "", "2026-10-01T00:00:00-06:00"))


def row(db, email=OWNER):
    """Flattened watch: keywords, alerts_on, city_<key>, street_<key> for all ten cities."""
    with sqlite3.connect(db) as c:
        c.row_factory = sqlite3.Row
        d = dict(c.execute("SELECT * FROM watches WHERE email=?", (email,)).fetchone())
        for k in webapp.CITY_KEYS:
            d[f"city_{k}"], d[f"street_{k}"] = 0, ""
        for r in c.execute("SELECT city_key, enabled, street FROM watch_cities WHERE email=?", (email,)):
            d[f"city_{r['city_key']}"], d[f"street_{r['city_key']}"] = r["enabled"], r["street"]
        return d


def signed_in_client(db, email=OWNER):
    nonce = "test-nonce-%d" % time.time_ns()
    with sqlite3.connect(db) as c:
        c.execute("INSERT INTO magic_nonces(nonce,email,created_at) VALUES(?,?,?)", (nonce, email, time.time()))
    client = TestClient(webapp.app)
    token = webapp.signer.dumps({"e": email, "n": nonce})
    r = client.get(f"/auth/verify?token={token}", follow_redirects=False)
    assert r.status_code == 303
    page = client.get("/watch")
    assert page.status_code == 200
    csrf = re.search(r'name="csrf" value="([^"]+)"', page.text).group(1)
    return client, csrf, page.text


def full_form(csrf, **overrides):
    """Exactly what the browser sends for the watch form (checked boxes only)."""
    data = {"csrf": csrf, "cities_submitted": "1", "city_ch": "1", "street_ch": STREET_CH,
            "city_slc": "1", "street_slc": "", "keywords_submitted": "1", "keywords": KW,
            "alerts_submitted": "1", "alerts_on": "1"}
    data.update(overrides)
    return {k: v for k, v in data.items() if v is not None}


# ---------------------------------------------------------------- fix: cities sentinel
def test_partial_post_without_city_slc_does_not_turn_slc_off(web):
    """Reproduces the 2026-10-06 17:22 MT POST: city_ch/street_ch/keywords/alerts sent,
    city_slc absent, no full city section. SLC must stay on."""
    seed(web, city_ch=1, city_slc=1, street_slc="451 S State St")
    client, csrf, _ = signed_in_client(web)
    r = client.post("/watch", data={"csrf": csrf, "city_ch": "1", "street_ch": STREET_CH,
                                    "keywords_submitted": "1", "keywords": KW,
                                    "alerts_submitted": "1", "alerts_on": "1"},
                    follow_redirects=False)
    assert r.status_code == 303
    w = row(web)
    assert (w["city_ch"], w["city_slc"]) == (1, 1)
    assert w["street_ch"] == STREET_CH and w["street_slc"] == "451 S State St"


def test_keywords_only_post_keeps_cities_streets_and_alerts(web):
    seed(web, city_ch=1, city_slc=1, street_slc="451 S State St", alerts_on=1)
    client, csrf, _ = signed_in_client(web)
    r = client.post("/watch", data={"csrf": csrf, "keywords_submitted": "1", "keywords": "zoning"},
                    follow_redirects=False)
    assert r.status_code == 303
    w = row(web)
    assert (w["city_ch"], w["city_slc"], w["alerts_on"]) == (1, 1, 1)
    assert (w["street_ch"], w["street_slc"]) == (STREET_CH, "451 S State St")
    assert w["keywords"] == "zoning"


def test_full_form_with_slc_unchecked_still_turns_slc_off(web):
    """Intentional saves still work: full form, SLC box unchecked -> off."""
    seed(web, city_ch=1, city_slc=1)
    client, csrf, _ = signed_in_client(web)
    r = client.post("/watch", data=full_form(csrf, city_slc=None), follow_redirects=False)
    assert r.status_code == 303
    assert (row(web)["city_ch"], row(web)["city_slc"]) == (1, 0)


def test_full_form_paid_both_cities_saves_both(web):
    seed(web, city_ch=1, city_slc=0)
    client, csrf, _ = signed_in_client(web)
    r = client.post("/watch", data=full_form(csrf), follow_redirects=False)
    assert r.status_code == 303
    w = row(web)
    assert (w["city_ch"], w["city_slc"], w["street_ch"], w["alerts_on"]) == (1, 1, STREET_CH, 1)


# ---------------------------------------------------------------- fix: alerts sentinel
def test_partial_post_without_alerts_field_keeps_alerts_on(web):
    seed(web, alerts_on=1)
    client, csrf, _ = signed_in_client(web)
    r = client.post("/watch", data={"csrf": csrf, "cities_submitted": "1", "city_ch": "1",
                                    "city_slc": "1", "street_ch": STREET_CH},
                    follow_redirects=False)
    assert r.status_code == 303
    assert row(web)["alerts_on"] == 1


def test_full_form_alerts_unchecked_turns_alerts_off(web):
    seed(web, alerts_on=1)
    client, csrf, _ = signed_in_client(web)
    client.post("/watch", data=full_form(csrf, alerts_on=None), follow_redirects=False)
    assert row(web)["alerts_on"] == 0


# ---------------------------------------------------------------- template / paid flag
def test_watch_page_has_all_sentinels_inside_watch_form_and_paid_flag(web):
    seed(web, city_ch=1, city_slc=1)
    _, _, html = signed_in_client(web)
    form = re.search(r'<form method="post" action="/watch".*?</form>', html, re.S).group(0)
    for name in ("cities_submitted", "keywords_submitted", "alerts_submitted"):
        assert f'name="{name}" value="1"' in form
    assert "const paid=true;" in html
    assert re.search(r'name="city_slc"[^>]*checked', form)


# ---------------------------------------------------------------- no silent switch-off when unpaid
@pytest.mark.parametrize("status", [None, "past_due", "canceled", "incomplete"])
def test_unpaid_or_stale_status_with_both_cities_errors_and_changes_nothing(web, status):
    seed(web, status=status, city_ch=1, city_slc=1, street_slc="451 S State St")
    before = row(web)
    client, csrf, html = signed_in_client(web)
    assert "const paid=false;" in html
    r = client.post("/watch", data=full_form(csrf, street_slc="451 S State St"), follow_redirects=False)
    assert r.status_code == 400
    assert "free plan watches one city" in r.text
    assert row(web) == before


def test_init_db_migration_rerun_never_touches_city_flags(web):
    seed(web, city_ch=1, city_slc=1, street_slc="451 S State St")
    before = row(web)
    webapp.init_db(); webapp.init_db()
    assert row(web) == before


def test_run_owner_overlay_does_not_write_db(pmn, owner_watch, run_main, sandbox):
    owner_watch(city_ch=1, city_slc=1, street_ch=STREET_CH, street_slc="")  # sandbox DB only
    before = aw.load_owner_watch({"owner_email": webapp.OWNER})
    code, out, _ = run_main("run-owner")
    assert "cities on: Cottonwood Heights [street '100 Example St'], Salt Lake City " in out
    assert "'Cottonwood Heights', 'Salt Lake City'" in out
    assert aw.load_owner_watch({"owner_email": webapp.OWNER}) == before
