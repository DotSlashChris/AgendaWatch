"""My watch: empty default for new users, 1..allowed cities on Save, lock wording (throwaway DB only)."""
import re
import sqlite3
import time

from fastapi.testclient import TestClient

from web import app as webapp
from tests.test_billing_portal_sync import web, stripe_api, seed_paid, EMAIL  # noqa: F401
from tests.test_web_watch import seed, row, signed_in_client

NEW = "test+brandnew@example.invalid"
FREE = "test+valfree@example.invalid"


def new_user_client(db, email=NEW):
    """Sign in through the real magic-link path so the watch row is created by the app."""
    token, _ = webapp.issue_magic_link(email)
    c = TestClient(webapp.app)
    r = c.get(token.replace(webapp.base_url(), ""), follow_redirects=False)
    assert r.status_code == 303
    page = c.get("/watch").text
    return c, re.search(r'name="csrf" value="([^"]+)"', page).group(1), page


def form(csrf, cities, **extra):
    d = {"csrf": csrf, "cities_submitted": "1", "keywords_submitted": "1", "keywords": "STR",
         "alerts_submitted": "1", **extra}
    for k, st in cities.items():
        d[f"city_{k}"] = "1"
        d[f"street_{k}"] = st
    return d


def test_new_user_sees_no_city_checked(web):
    c, csrf, page = new_user_client(web)
    assert re.search(r'<input type="checkbox" name="city_\w+"[^>]*checked', page) is None
    with sqlite3.connect(web) as con:
        assert con.execute("SELECT COUNT(*) FROM watch_cities WHERE email=?", (NEW,)).fetchone()[0] == 0
    assert "Your cities are locked until" not in page          # nothing locked yet
    assert "Saving locks your selected cities until" in page   # explains what Save does


def test_save_with_zero_cities_is_refused(web):
    c, csrf, _ = new_user_client(web)
    r = c.post("/watch", data=form(csrf, {}), follow_redirects=True)
    assert r.status_code == 400 and "Choose at least 1 city to watch." in r.text
    with sqlite3.connect(web) as con:
        assert con.execute("SELECT COUNT(*) FROM watch_cities WHERE email=?", (NEW,)).fetchone()[0] == 0


def test_free_over_limit_refused_and_one_city_saves_and_locks(web):
    c, csrf, _ = new_user_client(web)
    r = c.post("/watch", data=form(csrf, {"ch": "", "slc": ""}), follow_redirects=True)
    assert r.status_code == 400 and "free plan watches one city" in r.text
    r = c.post("/watch", data=form(csrf, {"sandy": "1 Main St"}, alerts_on="1"), follow_redirects=False)
    assert r.status_code == 303
    w = row(web, NEW)
    assert w["city_sandy"] == 1 and w["street_sandy"] == "1 Main St"
    page = c.get("/watch").text
    assert "Your cities are locked until" in page and "Saving locks your selected cities" not in page  # at limit, all locked


def test_paid_limit_is_one_plus_extra(web, stripe_api):
    seed_paid(web, extra=2, cities={"ch": (1, "")})
    stripe_api.set("active", 2)
    c, csrf, _ = signed_in_client(web, EMAIL)
    r = c.post("/watch", data=form(csrf, {"ch": "", "sandy": "", "draper": "", "orem": ""}), follow_redirects=True)
    assert r.status_code == 400 and "Your plan allows 3 cities" in r.text
    r = c.post("/watch", data=form(csrf, {"ch": "", "sandy": "", "draper": ""}), follow_redirects=False)
    assert r.status_code == 303


def test_partial_save_without_cities_keeps_everything(web):
    seed(web, email=FREE, status=None, cities={"ch": (1, "1 Main St")}, keywords="STR\nzoning")
    c, csrf, _ = signed_in_client(web, FREE)
    r = c.post("/watch", data={"csrf": csrf, "keywords_submitted": "1", "keywords": "STR"}, follow_redirects=False)
    assert r.status_code == 303
    w = row(web, FREE)
    assert w["city_ch"] == 1 and w["street_ch"] == "1 Main St" and w["keywords"] == "STR"


def test_admin_sees_no_lock_wording(web):
    seed(web, email=FREE, status=None, cities={"ch": (1, "")})
    from conftest import grant_admin
    grant_admin(FREE, web)
    with sqlite3.connect(web) as con:
        con.execute("UPDATE watch_cities SET locked_at=? WHERE email=?", (webapp.now_iso(), FREE))
    c, csrf, page = signed_in_client(web, FREE)
    assert "locked until" not in page and "Saving locks" not in page


def _visible_errors(html):
    """Error elements rendered visible in the HTML (not display:none)."""
    return [m for m in re.findall(r'<(?:p|div)[^>]*class="err[^"]*"[^>]*>', html) if "display:none" not in m]


def test_no_error_visible_on_load_and_save_never_disabled(web):
    c, csrf, page = new_user_client(web)
    assert _visible_errors(page) == []
    assert 'id="save-err"' in page and "Choose at least 1 city to watch." in page   # message only in the click handler
    assert "btn.disabled" not in page and re.search(r'id="savebtn"[^>]*disabled', page) is None


def test_server_error_shown_once(web):
    c, csrf, _ = new_user_client(web)
    r = c.post("/watch", data=form(csrf, {}), follow_redirects=True)
    body = r.text.split("<script>")[0]                      # rendered markup only
    assert body.count("Choose at least 1 city to watch.") == 1
    assert len(_visible_errors(body)) == 1


BADGE = re.compile(r'id="city_(\w+)"[^>]*>[^<]*<span class="lock-tag"')


def _badges(page):
    return sorted(BADGE.findall(page))


def test_lock_badge_only_after_save_and_only_for_saved_locked_cities(web):
    # New user: no badge, no status line.
    c, csrf, page = new_user_client(web)
    assert _badges(page) == [] and 'class="lock-tag"' not in page
    assert 'id="lock-status"' not in page
    # The page script never creates a badge client-side (ticking a box cannot add one).
    scripts = "".join(re.findall(r"(?s)<script>(.*?)</script>", page))
    assert scripts and not any(t in scripts for t in ("lock-tag", "\U0001F512", "createElement", "innerHTML", "insertAdjacent"))
    # A refused save (2 cities on free) re-renders the ticked boxes but adds no badge.
    r = c.post("/watch", data=form(csrf, {"ch": "", "slc": ""}), follow_redirects=True)
    assert r.status_code == 400 and _badges(r.text) == [] and 'id="lock-status"' not in r.text
    # After a successful save: badge for exactly the saved city, and the status line.
    assert c.post("/watch", data=form(csrf, {"sandy": ""}), follow_redirects=False).status_code == 303
    page = c.get("/watch").text
    assert _badges(page) == ["sandy"]
    assert 'id="lock-status"' in page and page.count("Your cities are locked until") == 1


def test_enabled_but_never_locked_city_shows_no_badge(web):
    # Legacy/unsaved-under-lock rows (enabled, locked_at NULL) are not locked: no badge, no status line.
    seed(web, email=FREE, status=None, cities={"ch": (1, "")})
    with sqlite3.connect(web) as con:
        con.execute("UPDATE watch_cities SET locked_at=NULL WHERE email=?", (FREE,))
    _, _, page = signed_in_client(web, FREE)
    assert _badges(page) == [] and 'id="lock-status"' not in page
    assert "Your cities are locked until" not in page


SAVE_LINE = "Saving locks your selected cities until"


def test_save_lock_line_only_when_selection_can_change(web):
    # paid, 2 allowed, 1 saved+locked city -> room to add -> line shown
    seed(web, email=FREE, status="active", extra_cities=1, cities={"ch": (1, "")})
    with sqlite3.connect(web) as con:
        con.execute("UPDATE watch_cities SET locked_at=? WHERE email=? AND enabled=1", (webapp.now_iso(), FREE))
    c, csrf, page = signed_in_client(web, FREE)
    assert page.count(SAVE_LINE) == 1
    # fill the second slot -> both locked at the limit -> line hidden (status line still shown)
    assert c.post("/watch", data=form(csrf, {"ch": "", "sandy": ""}), follow_redirects=False).status_code == 303
    page = c.get("/watch").text
    assert SAVE_LINE not in page and "Your cities are locked until" in page
    # a saved-but-unlocked city (e.g. after the unlock date) -> changeable -> line shown
    with sqlite3.connect(web) as con:
        con.execute("UPDATE watch_cities SET locked_at=NULL WHERE email=? AND city_key='sandy'", (FREE,))
    assert c.get("/watch").text.count(SAVE_LINE) == 1
    # over the limit (e.g. legacy/downgrade): locked cities may be turned off -> line shown
    with sqlite3.connect(web) as con:
        con.execute("UPDATE watch_cities SET locked_at=? WHERE email=?", (webapp.now_iso(), FREE))
        con.execute("UPDATE users SET subscription_status=NULL, extra_cities=0 WHERE email=?", (FREE,))
    assert c.get("/watch").text.count(SAVE_LINE) == 1
