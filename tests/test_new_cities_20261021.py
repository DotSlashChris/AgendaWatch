"""Catalog expansion 2026-10-21: South Jordan, Midvale (Salt Lake metro), Farmington (Ogden-Clearfield),
Vineyard (Provo-Orem), Daniel (Town of Daniel, Wasatch County -> Heber metro).

Sandbox DB + fake PMN only: no network, no email."""
import json

import aw_alerts
import aw_cities
from conftest import PROJECT, CITY_KEY
from test_user_alerts import A, SOON, STR_AGENDA, STR_ITEM, add_watch, by_user, plan, seeded, db  # noqa: F401

NEW = {
    # key: (name, metro, exact PMN entity, exact PMN public bodies)
    "sj": ("South Jordan", "Salt Lake metro", "South Jordan",
           ["City Council", "Planning Commission", "Board Of Adjustments",
            "VARIANCE AND APPEAL HEARINGS", "ARCHITECTURAL REVIEW COMMITTEE"]),
    "midvale": ("Midvale", "Salt Lake metro", "Midvale",
                ["City Council", "Planning Commission (Midvale)", "Board of Adjustments", "Appeal Authority"]),
    "farmington": ("Farmington", "Ogden-Clearfield metro", "Farmington",
                   ["Farmington City Council", "Farmington City Planning Commission",
                    "Farmington City Board of Adjustment", "Historic Preservation Commission"]),
    "vineyard": ("Vineyard", "Provo-Orem metro", "Vineyard",
                 ["City Council", "Planning Commission", "Development Review Committee",
                  "Administrative Law Judge/Hearing Officer", "Public Notices"]),
    "daniel": ("Daniel", "Heber metro", "Daniel", ["Town Council", "Planning Commission", "Board of Appeals"]),
}


def cfg():
    return aw_cities.load_config(PROJECT / "config.json")


def test_new_cities_map_to_exact_pmn_entity_and_bodies_under_the_right_metro():
    cities = {c["key"]: c for c in cfg()["cities"].values()}
    for key, (name, metro, entity, bodies) in NEW.items():
        c = cities[key]
        assert (c["name"], c["metro"], c["state"], c["pmn_entity_name"], c["public_bodies"]) == \
            (name, metro, "Utah", entity, bodies), key
    assert len(cities) == 17
    names = {c["name"] for c in cities.values()}
    assert not names & {"Heber City", "Heber"}   # Park City / St. George added 2026-11-03


def test_daniel_is_the_wasatch_county_town_in_its_own_heber_group():
    groups = dict(aw_cities.metro_groups(cfg()))
    assert [c["key"] for c in groups["Heber metro"]] == ["daniel", "parkcity"]
    assert "Wasatch County" in json.loads((PROJECT / "config.json").read_text())["_cities_note"]


def test_plan_cap_unchanged_at_ten_with_seventeen_cities():
    from web import app as webapp
    from web import stripe_billing as billing
    assert aw_cities.MAX_CITIES == 10 and webapp.MAX_CITIES == 10 and billing.MAX_EXTRA_CITIES == 9
    assert len(webapp.CITY_KEYS) == 17


def test_alerts_off_list_is_empty_after_owner_turned_wj_layton_on():
    # 2026-10-25: owner removed West Jordan and Layton after the gate (WJ 0, Layton 1 past ADU item).
    off = aw_alerts.off_cities(cfg())
    assert off == set()   # 2026-11-03: Park City / St. George turned on after a clean first scan
    assert not off & set(NEW)


def _set_off(sandbox, keys):
    p = sandbox.root / "config.json"
    c = json.loads(p.read_text())
    c["alerts_off_cities"] = list(keys)
    p.write_text(json.dumps(c))


def test_new_city_in_alerts_off_is_scanned_but_held_never_planned(db, pmn, run_main, sandbox):
    _set_off(sandbox, ["wj", "layton", "sj", "midvale", "farmington", "vineyard", "daniel"])
    add_watch(db.db, A, {k: (1, "") for k in NEW})
    seeded(db.db)
    for key, (name, *_rest) in NEW.items():
        pmn.add_notice(name, SOON, STR_AGENDA, title=f"{name} Council")
    code, out, err = run_main("run-owner")
    assert code in (0, 4), (out, err)
    p = plan(sandbox)
    assert p["mode"] == "send" and p["users"] == []
    assert p["held_cities_off"] == {k: 1 for k in NEW}
    s = json.loads((sandbox.state / "last_run_summary.json").read_text())
    by = {c["city"]: c for c in s["per_city"]}
    for key, (name, *_rest) in NEW.items():
        assert by[name]["fetch"] == "ok" and by[name]["hits"] == 1 and by[name]["key"] == CITY_KEY[name] == key


def test_new_city_with_alerts_on_plans_the_hit_for_its_watcher(db, pmn, run_main, sandbox):
    add_watch(db.db, A, {"vineyard": (1, ""), "daniel": (1, "")})
    seeded(db.db)
    pmn.add_notice("Vineyard", SOON, STR_AGENDA, title="Vineyard Council")
    pmn.add_notice("South Jordan", SOON, STR_AGENDA, title="South Jordan Council")  # not on A's watch
    run_main("run-owner")
    p = plan(sandbox)
    assert by_user(p) == {A: [STR_ITEM]}
    [item] = p["users"][0]["items"]
    assert item["city_key"] == "vineyard" and p["held_cities_off"] == {}
