"""Catalog expansion 2026-11-03 (owner order): Park City (PMN entity 233, Summit County -> Heber metro)
and St. George (PMN entity 277, Washington County -> its own St. George metro). Both start alerts-off.

Sandbox DB + fake PMN only: no network, no email."""
import json
import re

from fastapi.testclient import TestClient

import aw_alerts
import aw_cities
import aw_city_sources
from conftest import PROJECT, CITY_KEY
from test_user_alerts import A, SOON, STR_AGENDA, add_watch, plan, seeded, db  # noqa: F401

NEW = {
    "parkcity": ("Park City", "Heber metro", "Park City",
                 ["City Council", "Planning Commission", "Board of Adjustment", "Administrative Public Hearing",
                  "Historic Preservation Board", "Appeal Hearing"]),
    "stgeorge": ("St. George", "St. George metro", "St. George",
                 ["City Council", "Planning Commission", "Board of Adjustments", "Land Use Authority",
                  "Historic Preservation Commission"]),
}
BEFORE = ["ch", "slc", "sandy", "draper", "wj", "murray", "sj", "midvale", "ogden", "layton", "farmington",
          "provo", "orem", "vineyard", "daniel"]


def cfg():
    return aw_cities.load_config(PROJECT / "config.json")


def test_exactly_two_cities_added_with_exact_pmn_entity_bodies_and_metro():
    cities = {c["key"]: c for c in cfg()["cities"].values()}
    assert list(cities) == BEFORE + ["parkcity", "stgeorge"]
    for key, (name, metro, entity, bodies) in NEW.items():
        c = cities[key]
        assert (c["name"], c["metro"], c["state"], c["pmn_entity_name"], c["public_bodies"]) == \
            (name, metro, "Utah", entity, bodies), key
    groups = dict(aw_cities.metro_groups(cfg()))
    assert [c["key"] for c in groups["Heber metro"]] == ["daniel", "parkcity"]
    assert [c["key"] for c in groups["St. George metro"]] == ["stgeorge"]
    note = json.loads((PROJECT / "config.json").read_text())["_cities_note"]
    assert "Seventeen" in note and "233" in note and "277" in note


def test_both_new_cities_alerts_on_after_clean_first_scan():
    # Deployed alerts-off first (20261103-parkcity-stgeorge); turned on after the dry first scan showed no flood.
    assert aw_alerts.off_cities(cfg()) == set()
    assert "first scan" in json.loads((PROJECT / "config.json").read_text())["_alerts_off_cities"]


def test_cap_and_prices_unchanged():
    from web import app as webapp
    from web import stripe_billing as billing
    assert aw_cities.MAX_CITIES == 10 and webapp.MAX_CITIES == 10 and billing.MAX_EXTRA_CITIES == 9
    assert len(webapp.CITY_KEYS) == 17


def test_city_domains_are_the_cities_own_and_nothing_is_seeded():
    assert aw_city_sources.CITY_DOMAINS["parkcity"] == ("parkcity.gov", "parkcity.org")
    assert aw_city_sources.CITY_DOMAINS["stgeorge"] == ("sgcityutah.gov", "sgcity.org")
    assert {k for k, *_ in aw_city_sources.SEEDS} == {"ch"}


def test_home_count_and_areas_are_derived_not_hard_coded():
    from web import app as webapp
    tpl = (PROJECT / "web" / "templates" / "index.html").read_text()
    assert "{{ city_count }}" in tpl and "{{ area_phrase }}" in tpl
    assert not re.search(r"\b1[57]\b|fifteen|seventeen", tpl, re.I)
    html = TestClient(webapp.app).get("/").text
    assert f"— {len(webapp.CITY_LIST)} Utah cities in the {aw_cities.area_phrase(webapp.CFG)} areas." in html
    assert "— 17 Utah cities in the Salt Lake, Ogden, Provo, Heber and St. George areas." in html
    assert "<strong>Park City</strong>" in html and "<strong>St. George</strong>" in html


def _set_off(sandbox, keys):
    p = sandbox.root / "config.json"
    c = json.loads(p.read_text())
    c["alerts_off_cities"] = list(keys)
    p.write_text(json.dumps(c))


def test_new_cities_are_scanned_but_held_while_alerts_off(db, pmn, run_main, sandbox):
    _set_off(sandbox, list(NEW))
    add_watch(db.db, A, {k: (1, "") for k in NEW})
    seeded(db.db)
    for key, (name, *_rest) in NEW.items():
        pmn.add_notice(name, SOON, STR_AGENDA, title=f"{name} Council")
    code, out, err = run_main("run-owner")
    assert code in (0, 4), (out, err)
    p = plan(sandbox)
    assert p["users"] == [] and p["held_cities_off"] == {k: 1 for k in NEW}
    s = json.loads((sandbox.state / "last_run_summary.json").read_text())
    by = {c["city"]: c for c in s["per_city"]}
    for key, (name, *_rest) in NEW.items():
        assert by[name]["fetch"] == "ok" and by[name]["hits"] == 1 and by[name]["key"] == CITY_KEY[name] == key


def test_new_cities_alerts_on_plan_hits_only_for_their_own_watcher(db, pmn, run_main, sandbox):
    from test_user_alerts import B
    add_watch(db.db, A, {"stgeorge": (1, "")})
    add_watch(db.db, B, {"ch": (1, "")})
    seeded(db.db)
    pmn.add_notice("St. George", SOON, STR_AGENDA, title="St. George Council")
    pmn.add_notice("Park City", SOON, STR_AGENDA, title="Park City Council")   # nobody watches Park City
    run_main("run-owner")
    p = plan(sandbox)
    assert [u["email"] for u in p["users"]] == [A] and p["held_cities_off"] == {}
    assert {i["city_key"] for i in p["users"][0]["items"]} == {"stgeorge"}
