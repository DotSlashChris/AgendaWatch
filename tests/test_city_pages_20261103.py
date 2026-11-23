"""Nov 3 2026 owner order: one public page per catalog city at /cities/<slug>, no login, generated from
config.json (title/H1 "<City> short-term rental agendas", one sentence, a sign-in link, nothing else),
listed in sitemap.xml. Unknown slugs 404. The edge's generated catalog must match config.json."""
import html as htmllib
import re
import sys
from pathlib import Path

from fastapi.testclient import TestClient

import aw_cities
from test_web_watch import web, webapp  # noqa: F401  (fixture)

ROOT = Path(__file__).resolve().parent.parent
CFG = aw_cities.load_config(ROOT / "config.json")
NAMES = [c["name"] for c in aw_cities.city_list(CFG)]


def visible(body):
    main = body.split("</header>", 1)[1].split("<footer>", 1)[0]
    return re.sub(r"\s+", " ", htmllib.unescape(re.sub(r"<[^>]+>", " ", main))).strip()


def test_slugs_kebab_case_from_catalog():
    assert len(NAMES) == 17
    assert aw_cities.city_slug("Park City") == "park-city"
    assert aw_cities.city_slug("St. George") == "st-george"
    assert aw_cities.city_slug("Salt Lake City") == "salt-lake-city"
    assert aw_cities.city_slug("Cottonwood Heights") == "cottonwood-heights"
    slugs = [p["slug"] for p in aw_cities.city_pages(CFG)]
    assert len(set(slugs)) == 17 and all(re.fullmatch(r"[a-z0-9]+(-[a-z0-9]+)*", s) for s in slugs)


def test_every_catalog_city_page_loads_anonymously(web):
    c = TestClient(webapp.app)
    for name in NAMES:
        r = c.get(f"/cities/{aw_cities.city_slug(name)}")
        assert r.status_code == 200, name
        want = f"{name} short-term rental agendas"
        assert f"<title>{htmllib.escape(want)}</title>" in r.text, name
        assert f"<h1>{htmllib.escape(want)}</h1>" in r.text, name
        assert '<a class="btn" href="/signin">Sign in</a>' in r.text
        assert visible(r.text) == (f"{want} Agenda Watch emails you when an upcoming public agenda in {name} "
                                   "mentions short-term rentals. Sign in")
        low = r.text.lower()
        for bad in ("$", "price", "/month", "stripe", "test mode", "test-mode", "example", "notice", "blog"):
            assert bad not in visible(r.text).lower(), (name, bad)
        others = [n for n in NAMES if n != name and n not in name and name not in n]
        assert not any(o in visible(r.text) for o in others), name
        assert 'action="/signout"' not in low


def test_unknown_slug_404(web):
    c = TestClient(webapp.app)
    for p in ("/cities/moab", "/cities/Park-City", "/cities/park_city", "/cities/st.-george", "/cities/"):
        assert c.get(p).status_code == 404, p


def test_sitemap_lists_home_plus_exactly_catalog(web):
    r = TestClient(webapp.app).get("/sitemap.xml")
    assert r.status_code == 200 and "xml" in r.headers["content-type"]
    locs = re.findall(r"<loc>([^<]+)</loc>", r.text)
    assert locs == ["https://agendawatch.app/"] + [f"https://agendawatch.app/cities/{aw_cities.city_slug(n)}" for n in NAMES]


def test_future_catalog_city_gets_a_page_automatically():
    cfg = {"cities": {"Moab": {"key": "moab", "state": "Utah", "metro": "Grand"}}}
    assert aw_cities.city_pages(cfg) == [{"slug": "moab", "name": "Moab", "key": "moab"}]
    assert "https://agendawatch.app/cities/moab" in aw_cities.sitemap_xml(cfg)


def test_worker_catalog_is_generated_from_config():
    sys.path.insert(0, str(ROOT / "tools"))
    import gen_worker_catalog
    assert (ROOT / "cloudflare" / "src" / "catalog.gen.ts").read_text() == gen_worker_catalog.render()


def test_home_page_has_no_city_page_content(web):
    r = TestClient(webapp.app).get("/")
    assert r.status_code == 200 and "/cities/" not in r.text and "short-term rental agendas" not in r.text
