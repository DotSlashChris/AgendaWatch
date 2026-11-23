"""Nov 12 2026: self-referencing canonical tags on public pages (sitemap URL form, fixed apex)
and 301 /cities/<slug>/ to the no-slash path. Sitemap unchanged."""
import re

from fastapi.testclient import TestClient

import aw_cities
from test_city_pages_20261103 import CFG, NAMES
from test_web_watch import web, webapp  # noqa: F401
from test_city_pages_20261103 import ROOT  # noqa: F401


CANON_RE = re.compile(r'<link rel="canonical" href="([^"]+)">')
SITE = "https://agendawatch.app"


def _client():
    # A non-apex base URL: the canonical must still be the fixed apex, not the request Host.
    return TestClient(webapp.app, base_url="https://www.example.test")


def test_home_canonical_matches_sitemap_form(web):
    r = _client().get("/")
    assert r.status_code == 200
    assert CANON_RE.findall(r.text) == [f"{SITE}/"]
    # home sitemap form includes the trailing slash; only one canonical tag
    assert r.text.count('rel="canonical"') == 1


def test_every_catalog_city_page_has_self_canonical(web):
    c = _client()
    for name in NAMES:
        slug = aw_cities.city_slug(name)
        r = c.get(f"/cities/{slug}")
        assert r.status_code == 200, name
        assert CANON_RE.findall(r.text) == [f"{SITE}/cities/{slug}"], name
        assert r.text.count('rel="canonical"') == 1


def test_trailing_slash_301s_to_no_slash_and_keeps_query(web):
    c = _client()
    r = c.get("/cities/salt-lake-city/?utm=1", follow_redirects=False)
    assert r.status_code == 301
    assert r.headers["location"] == "/cities/salt-lake-city?utm=1"
    r2 = c.get("/cities/park-city/", follow_redirects=False)
    assert r2.status_code == 301 and r2.headers["location"] == "/cities/park-city"
    # no-slash page is still 200
    assert c.get("/cities/salt-lake-city").status_code == 200


def test_unknown_slug_trailing_slash_301s_then_404(web):
    c = _client()
    r = c.get("/cities/moab/?x=1", follow_redirects=False)
    assert r.status_code == 301
    assert r.headers["location"] == "/cities/moab?x=1"
    assert c.get("/cities/moab").status_code == 404
    # /cities/ itself is unchanged (not a /cities/<slug>/ path)
    assert c.get("/cities/", follow_redirects=False).status_code == 404


def test_sitemap_unchanged(web):
    r = _client().get("/sitemap.xml")
    locs = re.findall(r"<loc>([^<]+)</loc>", r.text)
    assert locs == [f"{SITE}/"] + [f"{SITE}/cities/{aw_cities.city_slug(n)}" for n in NAMES]


def test_other_pages_have_no_canonical(web):
    c = _client()
    r = c.get("/signin")
    assert r.status_code == 200
    assert 'rel="canonical"' not in r.text
    r = c.get("/feedback", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/signin"
