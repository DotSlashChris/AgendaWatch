"""City lock: no swapping until the next unlock date (throwaway DB + test users only)."""
import datetime as dt
import sqlite3

import pytest

from web import app as webapp
from tests.test_billing_portal_sync import (  # noqa: F401  (fixtures)
    web, stripe_api, seed_paid, user, on, streets, EMAIL, CUST, STREETS)
from tests.test_web_watch import seed, row, signed_in_client

D = webapp.DENVER
FREE = "test+lockfree@example.invalid"
PE = 1794707703  # Nov 14 2026 18:55 MST


def full_form(csrf, cities, keywords="STR", alerts=True):
    d = {"csrf": csrf, "cities_submitted": "1", "keywords_submitted": "1", "keywords": keywords,
         "alerts_submitted": "1"}
    if alerts:
        d["alerts_on"] = "1"
    for k, street in cities.items():
        d[f"city_{k}"] = "1"
        d[f"street_{k}"] = street
    return d


def lock_now(db, email):
    with sqlite3.connect(db) as c:
        c.execute("UPDATE watch_cities SET locked_at=? WHERE email=? AND enabled=1", (webapp.now_iso(), email))


def test_unlock_window_free_is_first_of_next_month_denver():
    now = dt.datetime(2026, 10, 31, 23, 30, tzinfo=D)
    last, nxt = webapp.unlock_window({"subscription_status": None}, now)
    assert (last.date(), nxt.date()) == (dt.date(2026, 10, 1), dt.date(2026, 11, 1))
    _, nxt = webapp.unlock_window({}, dt.datetime(2026, 12, 15, tzinfo=D))
    assert nxt.date() == dt.date(2027, 1, 1)


def test_unlock_window_paid_uses_period_end_and_rolls_forward():
    u = {"subscription_status": "active", "period_end": PE}
    last, nxt = webapp.unlock_window(u, dt.datetime(2026, 10, 20, tzinfo=D))
    assert nxt.date() == dt.date(2026, 11, 14) and last.date() == dt.date(2026, 10, 14)
    _, nxt = webapp.unlock_window(u, dt.datetime(2026, 11, 20, tzinfo=D))   # stale period end
    assert nxt.date() == dt.date(2026, 12, 14)
    assert webapp.fmt_day(webapp.unlock_window(u, dt.datetime(2026, 10, 20, tzinfo=D))[1].timestamp()) \
        == "November 14, 2026"


def test_migration_locks_existing_enabled_cities(web):
    seed(web, email=FREE, status=None, cities={"ch": (1, "1 Main St"), "slc": (0, "2 Old Rd")})
    with sqlite3.connect(web) as c:
        c.execute("DELETE FROM schema_migrations WHERE id='city_lock_v1'")
    webapp.init_db()
    locks, _ = webapp.city_locks(FREE)
    assert locks == {"ch": "locked"}


def test_free_cannot_swap_locked_city_but_can_edit_street(web):
    seed(web, email=FREE, status=None, cities={"ch": (1, "1 Main St")})
    lock_now(web, FREE)
    client, csrf, page = signed_in_client(web, FREE)
    assert "Your cities are locked until" in page and "🔒 locked" in page
    # free + its one city locked = nothing changeable -> no "Saving locks" line
    assert "Saving locks your selected cities" not in page and "Cities lock until" not in page
    r = client.post("/watch", data=full_form(csrf, {"slc": ""}), follow_redirects=True)
    assert "Cottonwood Heights is locked until" in r.text
    w = row(web, FREE)
    assert w["city_ch"] == 1 and w["city_slc"] == 0 and w["street_ch"] == "1 Main St"
    r = client.post("/watch", data=full_form(csrf, {"ch": "9 New St"}), follow_redirects=False)
    assert r.status_code == 303 and row(web, FREE)["street_ch"] == "9 New St"


def test_turning_off_locked_city_is_refused_server_side(web):
    seed(web, email=FREE, status=None, cities={"ch": (1, "1 Main St")})
    lock_now(web, FREE)
    client, csrf, _ = signed_in_client(web, FREE)
    d = full_form(csrf, {}, alerts=False)
    r = client.post("/watch", data=d, follow_redirects=True)
    assert "locked until" in r.text and row(web, FREE)["city_ch"] == 1


def test_partial_save_keeps_cities_streets_alerts_keywords(web):
    seed(web, email=FREE, status=None, cities={"ch": (1, "1 Main St")}, keywords="STR\nzoning")
    lock_now(web, FREE)
    client, csrf, _ = signed_in_client(web, FREE)
    r = client.post("/watch", data={"csrf": csrf, "alerts_submitted": "1"}, follow_redirects=False)
    assert r.status_code == 303
    w = row(web, FREE)
    assert w["city_ch"] == 1 and w["street_ch"] == "1 Main St" and w["keywords"] == "STR\nzoning"
    assert w["alerts_on"] == 0


def test_new_city_within_allowance_allowed_and_then_locked(web, stripe_api):
    seed_paid(web, extra=2, cities={"ch": (1, "100 Test St")})
    stripe_api.set("active", 2)
    lock_now(web, EMAIL)
    client, csrf, _ = signed_in_client(web, EMAIL)
    r = client.post("/watch", data=full_form(csrf, {"ch": "100 Test St", "sandy": "200 Test Ave"}),
                    follow_redirects=False)
    assert r.status_code == 303 and on(web) == ["ch", "sandy"]
    assert webapp.city_locks(EMAIL)[0] == {"ch": "locked", "sandy": "locked"}
    r = client.post("/watch", data=full_form(csrf, {"ch": "100 Test St", "draper": ""}), follow_redirects=True)
    assert "Sandy is locked until" in r.text and on(web) == ["ch", "sandy"]


def test_paid_lock_date_is_billing_date(web, stripe_api):
    seed_paid(web, extra=1, cities={"ch": (1, ""), "sandy": (1, "")})
    stripe_api.set("active", 1)
    stripe_api.sub.update(current_period_end=PE)
    lock_now(web, EMAIL)
    client, csrf, page = signed_in_client(web, EMAIL)    # page load syncs period_end from Stripe
    assert user(web)["period_end"] == PE
    page = client.get("/watch").text
    assert "Your cities are locked until November 14, 2026." in page
    assert "Saving locks your selected cities" not in page        # 2 of 2 locked: nothing changeable


def test_downgrade_turns_off_latest_and_blocks_reenable_until_unlock(web, stripe_api):
    seed_paid(web, extra=3, enabled_at={"ch": "2026-10-01", "sandy": "2026-10-02",
                                         "draper": "2026-10-03", "provo": "2026-10-04"})
    lock_now(web, EMAIL)
    stripe_api.set("active", 1)                            # portal decrease took effect at period end
    client, csrf, _ = signed_in_client(web, EMAIL)
    assert on(web) == ["ch", "sandy"] and streets(web) == STREETS
    assert webapp.city_locks(EMAIL)[0]["draper"] == "blocked"
    # can't bring a blocked city back in place of... (cities at limit anyway) nor after a re-upgrade swap
    stripe_api.set("active", 3)
    with sqlite3.connect(web) as c:
        c.execute("UPDATE users SET billing_synced_at=0 WHERE email=?", (EMAIL,))
    client.get("/watch")
    r = client.post("/watch", data=full_form(csrf, {"ch": "100 Test St", "sandy": "200 Test Ave",
                                                    "draper": ""}), follow_redirects=True)
    assert "Draper stays off until" in r.text and "draper" not in on(web)
    # a never-blocked city can still be added within the allowance
    r = client.post("/watch", data=full_form(csrf, {"ch": "100 Test St", "sandy": "200 Test Ave",
                                                    "orem": ""}), follow_redirects=False)
    assert r.status_code == 303 and "orem" in on(web) and streets(web) == STREETS


def test_unlocks_after_unlock_date(web, monkeypatch):
    seed(web, email=FREE, status=None, cities={"ch": (1, "1 Main St")})
    with sqlite3.connect(web) as c:
        c.execute("UPDATE watch_cities SET locked_at='2026-09-20T10:00:00-06:00' WHERE email=?", (FREE,))
    assert webapp.city_locks(FREE, now=dt.datetime(2026, 10, 14, tzinfo=D))[0] == {}


ADMIN = "test+lockadmin@example.invalid"


def make_admin(db, email):
    from conftest import grant_admin
    grant_admin(email, db)


def test_admin_has_no_lock_line_tags_or_enforcement(web):
    seed(web, email=ADMIN, status=None, cities={"ch": (1, "1 Main St")})
    lock_now(web, ADMIN)
    make_admin(web, ADMIN)
    client, csrf, page = signed_in_client(web, ADMIN)
    assert "locked until" not in page and "Saving locks" not in page and "🔒 locked" not in page \
        and 'data-lock=' not in page
    r = client.post("/watch", data=full_form(csrf, {"slc": "2 Swap St"}), follow_redirects=False)
    assert r.status_code == 303
    w = row(web, ADMIN)
    assert w["city_ch"] == 0 and w["city_slc"] == 1 and w["street_slc"] == "2 Swap St"


def test_admin_gets_every_city_free(web):
    """2026-10-22 owner order: admins have no city cap and no billing (all catalog cities + streets)."""
    seed(web, email=ADMIN, status=None, cities={"ch": (1, "")})
    make_admin(web, ADMIN)
    client, csrf, page = signed_in_client(web, ADMIN)
    assert "Admin account" in page
    r = client.post("/watch", data=full_form(csrf, {"ch": "", "slc": ""}), follow_redirects=False)
    assert r.status_code == 303 and row(web, ADMIN)["city_ch"] == 1 and row(web, ADMIN)["city_slc"] == 1


def test_admin_not_blocked_after_count_drop(web, stripe_api):
    seed_paid(web, extra=3, enabled_at={"ch": "2026-10-01", "sandy": "2026-10-02",
                                         "draper": "2026-10-03", "provo": "2026-10-04"})
    make_admin(web, EMAIL)
    lock_now(web, EMAIL)
    stripe_api.set("active", 1)
    client, csrf, _ = signed_in_client(web, EMAIL)
    # 2026-10-22: admins have no city cap, so a Stripe count drop switches nothing off.
    assert sorted(on(web)) == ["ch", "draper", "provo", "sandy"]
    assert webapp.city_locks(EMAIL)[0] == {}
    r = client.post("/watch", data=full_form(csrf, {"ch": "100 Test St", "draper": ""}), follow_redirects=False)
    assert r.status_code == 303 and on(web) == ["ch", "draper"] and streets(web)["sandy"] == "200 Test Ave"


def test_non_admin_unchanged_still_locked(web):
    seed(web, email=FREE, status=None, cities={"ch": (1, "")})
    lock_now(web, FREE)
    assert webapp.city_locks(FREE)[0] == {"ch": "locked"}
