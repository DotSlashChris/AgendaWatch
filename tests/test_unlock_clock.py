"""Does unlocking actually work? Frozen Denver clock (webapp.clock), throwaway DB + test users only."""
import datetime as dt
import sqlite3

import pytest
from fastapi.testclient import TestClient

from web import app as webapp
from web import stripe_billing as billing
from tests.test_billing_portal_sync import web  # noqa: F401  (fixture)
from tests.test_web_watch import seed, row, signed_in_client
from tests.test_watch_validation import new_user_client, form

D = webapp.DENVER
UTC = dt.timezone.utc
FREE = "test+clockfree@example.invalid"
PAID = "test+clockpaid@example.invalid"
CUST = "cus_fake_clock"
PE = 1794707703                       # Nov 14 2026 18:55:03 MST (owner's real period end shape)
PE_DT = dt.datetime.fromtimestamp(PE, D)


class Clock:
    def __init__(self, monkeypatch):
        self.t = None
        monkeypatch.setattr(webapp, "clock", lambda: self.t)

    def at(self, *a):
        self.t = a[0] if isinstance(a[0], dt.datetime) else dt.datetime(*a, tzinfo=D)
        return self


@pytest.fixture(autouse=True)
def no_stripe(monkeypatch):
    """No Stripe API calls from these tests (the renewal test patches its own fake)."""
    monkeypatch.setattr(billing, "configured", lambda: False)


@pytest.fixture
def clock(monkeypatch):
    return Clock(monkeypatch)


def on_cities(email):
    w = row(webapp.DB, email)
    return [k for k in webapp.CITY_KEYS if w[f"city_{k}"]]


def locks(email):
    lk, nxt = webapp.city_locks(email)
    return lk, webapp.fmt_day(nxt.timestamp())


def save(c, csrf, cities):
    return c.post("/watch", data=form(csrf, {k: "" for k in cities}), follow_redirects=True)


# ---------------------------------------------------------------- free plan: 1st of the month, Denver
def test_free_locks_until_first_of_month_then_unlocks_at_midnight_denver(web, clock):
    clock.at(2026, 10, 16, 12, 0)
    c, csrf, _ = new_user_client(web, FREE)
    assert save(c, csrf, ["sandy"]).status_code == 200 and on_cities(FREE) == ["sandy"]
    assert locks(FREE) == ({"sandy": "locked"}, "November 1, 2026")

    clock.at(2026, 10, 31, 23, 59, 59)                       # last second of October: still locked
    assert locks(FREE)[0] == {"sandy": "locked"}
    r = save(c, csrf, ["ch"])
    assert "Sandy is locked until November 1, 2026." in r.text and on_cities(FREE) == ["sandy"]

    clock.at(2026, 11, 1, 0, 0, 0)                           # midnight Nov 1 Denver: unlocked
    assert locks(FREE) == ({}, "December 1, 2026")
    page = c.get("/watch").text
    assert 'id="lock-status"' not in page and 'class="lock-tag"' not in page
    assert "Saving locks your selected cities until December 1, 2026." in page
    r = save(c, csrf, ["ch"])                                # switch
    assert "Sandy is locked until" not in r.text and 'class="err page-errors"' not in r.text and on_cities(FREE) == ["ch"]
    assert locks(FREE) == ({"ch": "locked"}, "December 1, 2026")


def test_free_unlock_uses_denver_midnight_not_utc(web, clock):
    clock.at(2026, 10, 16, 12, 0)
    c, csrf, _ = new_user_client(web, FREE)
    save(c, csrf, ["sandy"])
    # Nov 1 00:00 UTC is still Oct 31 18:00 in Denver -> still locked
    clock.at(dt.datetime(2026, 11, 1, 0, 0, tzinfo=UTC))
    assert locks(FREE)[0] == {"sandy": "locked"}
    clock.at(dt.datetime(2026, 11, 1, 6, 0, tzinfo=UTC))    # = Nov 1 00:00 MDT
    assert locks(FREE)[0] == {}


def test_resave_after_unlock_relocks_kept_city(web, clock):
    """'Saving locks your selected cities until <date>' must be true for a city kept across the unlock."""
    clock.at(2026, 10, 16, 12, 0)
    c, csrf, _ = new_user_client(web, FREE)
    save(c, csrf, ["sandy"])
    clock.at(2026, 11, 3, 9, 0)
    assert locks(FREE)[0] == {}
    save(c, csrf, ["sandy"])                                 # keep Sandy, save (e.g. edited keywords)
    assert locks(FREE) == ({"sandy": "locked"}, "December 1, 2026")
    r = save(c, csrf, ["ch"])
    assert "Sandy is locked until December 1, 2026." in r.text and on_cities(FREE) == ["sandy"]


def test_free_year_boundary_dec_to_jan(web, clock):
    clock.at(2026, 12, 10, 8, 0)
    c, csrf, _ = new_user_client(web, FREE)
    save(c, csrf, ["provo"])
    assert locks(FREE) == ({"provo": "locked"}, "January 1, 2027")
    clock.at(2026, 12, 31, 23, 59, 59)
    assert locks(FREE)[0] == {"provo": "locked"}
    clock.at(2027, 1, 1, 0, 0)
    assert locks(FREE) == ({}, "February 1, 2027")
    save(c, csrf, ["orem"])
    assert on_cities(FREE) == ["orem"] and locks(FREE) == ({"orem": "locked"}, "February 1, 2027")


# ---------------------------------------------------------------- paid: Stripe billing date
def seed_paid(db, extra=1, period_end=PE, cities=None):
    seed(db, email=PAID, status="active", extra_cities=extra, cities=cities or {})
    with sqlite3.connect(db) as con:
        con.execute("UPDATE users SET period_end=?, stripe_customer_id=?, stripe_subscription_id='sub_clock' "
                    "WHERE email=?", (period_end, CUST, PAID))


def test_paid_locked_until_period_end_then_unlocked_and_relocks_to_next_period(web, clock):
    seed_paid(web)
    clock.at(2026, 10, 20, 10, 0)
    c, csrf, _ = signed_in_client(web, PAID)
    save(c, csrf, ["ch", "sandy"])
    assert locks(PAID) == ({"ch": "locked", "sandy": "locked"}, "November 14, 2026")
    clock.at(PE_DT - dt.timedelta(seconds=1))                 # Nov 14 18:55:02 MST
    assert len(locks(PAID)[0]) == 2
    assert "Sandy is locked until November 14, 2026." in save(c, csrf, ["ch", "draper"]).text
    clock.at(PE_DT)                                          # exactly the billing moment (period_end not updated yet)
    assert locks(PAID) == ({}, "December 14, 2026")
    r = save(c, csrf, ["ch", "draper"])
    assert "Sandy is locked until" not in r.text and 'class="err page-errors"' not in r.text and on_cities(PAID) == ["ch", "draper"]
    assert locks(PAID) == ({"ch": "locked", "draper": "locked"}, "December 14, 2026")


def test_paid_stale_period_end_rolls_forward(web, clock):
    u = {"subscription_status": "active", "period_end": PE}
    # one month stale
    last, nxt = webapp.unlock_window(u, dt.datetime(2026, 11, 20, tzinfo=D))
    assert (last, nxt) == (PE_DT, dt.datetime.fromtimestamp(PE, UTC).replace(month=12).astimezone(D))
    # several months stale, across the year boundary
    last, nxt = webapp.unlock_window(u, dt.datetime(2027, 2, 1, tzinfo=D))
    assert (last.date(), nxt.date()) == (dt.date(2027, 1, 14), dt.date(2027, 2, 14))
    # month-end anchor must not drift: Jan 31 -> Feb 28 -> Mar 31 -> Apr 30 (Stripe behaviour)
    jan31 = int(dt.datetime(2027, 1, 31, 17, 0, tzinfo=UTC).timestamp())
    u2 = {"subscription_status": "active", "period_end": jan31}
    last, nxt = webapp.unlock_window(u2, dt.datetime(2027, 4, 15, tzinfo=D))
    assert (last.astimezone(UTC).date(), nxt.astimezone(UTC).date()) == (dt.date(2027, 3, 31), dt.date(2027, 4, 30))


def test_paid_period_start_matches_stripe_across_dst(web, clock):
    # Stripe periods are whole UTC months: Nov 15 01:55:03Z <- Oct 15 01:55:03Z (= Oct 14 19:55 MDT, not 18:55).
    last, nxt = webapp.unlock_window({"subscription_status": "active", "period_end": PE},
                                     dt.datetime(2026, 10, 20, tzinfo=D))
    assert nxt == PE_DT and last == dt.datetime(2026, 10, 15, 1, 55, 3, tzinfo=UTC)


def test_renewal_webhook_moves_period_end_and_unlocks(web, clock, monkeypatch):
    seed_paid(web)
    clock.at(2026, 10, 20, 10, 0)
    c, csrf, _ = signed_in_client(web, PAID)
    save(c, csrf, ["ch", "sandy"])
    new_pe = int(dt.datetime(2026, 12, 15, 1, 55, 3, tzinfo=UTC).timestamp())   # Dec 14 18:55 MST
    sub = {"id": "sub_clock", "object": "subscription", "status": "active", "customer": CUST,
           "metadata": {"email": PAID}, "cancel_at_period_end": False, "cancel_at": None,
           "items": {"data": [{"id": "si_c", "quantity": 1, "current_period_end": new_pe}]}}
    event = {"id": "evt_renew", "type": "customer.subscription.updated", "data": {"object": sub}}
    monkeypatch.setattr(billing, "configured", lambda: True)
    monkeypatch.setattr(billing, "construct_event", lambda payload, sig: event)
    monkeypatch.setattr(billing, "fetch_current_subscription", lambda cust, pref=None: sub)
    clock.at(2026, 11, 14, 19, 0)
    r = TestClient(webapp.app).post("/stripe/webhook", content=b"{}", headers={"stripe-signature": "t"})
    assert r.status_code == 200
    with sqlite3.connect(web) as con:
        assert con.execute("SELECT period_end FROM users WHERE email=?", (PAID,)).fetchone()[0] == new_pe
    assert locks(PAID) == ({}, "December 14, 2026")
    monkeypatch.setattr(billing, "configured", lambda: False)
    save(c, csrf, ["ch", "orem"])
    assert on_cities(PAID) == ["ch", "orem"] and locks(PAID)[1] == "December 14, 2026"


def test_drop_count_stays_off_until_billing_date_then_can_return(web, clock):
    """Quantity 2 -> 1 mid-period: the switched-off city 'stays off until' the billing date, then is free."""
    seed_paid(web, extra=2)
    clock.at(2026, 10, 20, 10, 0)
    c, csrf, _ = signed_in_client(web, PAID)
    save(c, csrf, ["ch", "sandy", "draper"])
    clock.at(2026, 10, 25, 10, 0)
    webapp.apply_subscription_state(PAID, customer_id=CUST, status="active", quantity=1, period_end=PE)
    off = [k for k in ("ch", "sandy", "draper") if k not in on_cities(PAID)]
    assert len(off) == 1 and webapp.city_locks(PAID)[0][off[0]] == "blocked"
    kept = on_cities(PAID)
    r = save(c, csrf, [kept[0], off[0]])                     # try to bring it back instead of the other one
    assert f"{webapp.CITY_NAME[off[0]]} stays off until November 14, 2026." in r.text
    clock.at(PE_DT - dt.timedelta(seconds=1))
    assert webapp.city_locks(PAID)[0][off[0]] == "blocked"
    clock.at(PE_DT)
    assert off[0] not in webapp.city_locks(PAID)[0]
    r = save(c, csrf, [kept[0], off[0]])
    assert "stays off" not in r.text and sorted(on_cities(PAID)) == sorted([kept[0], off[0]])


def test_cancel_blocks_until_first_of_month_on_free(web, clock):
    """Full cancel (back to free) mid-period: the dropped city follows the free calendar (1st of month)."""
    seed_paid(web, extra=1)
    clock.at(2026, 10, 20, 10, 0)
    c, csrf, _ = signed_in_client(web, PAID)
    save(c, csrf, ["ch", "sandy"])
    clock.at(2026, 10, 22, 10, 0)
    webapp.apply_subscription_state(PAID, customer_id=CUST, status="canceled", quantity=0)
    lk, when = locks(PAID)
    assert "blocked" in lk.values() and when == "November 1, 2026"
    clock.at(2026, 11, 1, 0, 0)
    assert "blocked" not in webapp.city_locks(PAID)[0].values()
