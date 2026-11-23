"""Daily owner run (run-owner): scans ALL supported cities (17), emails only watch-enabled-city hits,
writes state/last_run_summary.json; cross-city street scoping. Fake PMN, sandbox DB/state."""
import datetime as dt
import json

import agenda_watch as aw
from conftest import ALL_CITIES, CITY_KEY, FROZEN_NOW, outbox_files

STR_ITEM = "2. Discussion of short-term rental regulations"
STR_AGENDA = f"1. Call to Order\n{STR_ITEM}\n3. Adjourn"
SOON = FROZEN_NOW + dt.timedelta(days=5)


def emails(sandbox):
    return sorted(sandbox.outbox.glob("email-*.json"))


def summary(sandbox):
    return json.loads((sandbox.state / "last_run_summary.json").read_text())


def street_hits(res):
    return sorted((h["city"], h["matched"]) for h in res["hits"] if h["match_type"] == "address")


def test_sandy_street_does_not_match_draper_agenda(pmn, cfg, owner_watch):
    watch = owner_watch(cities={"sandy": (1, "9400 S 700 E"), "draper": (1, "")}, keywords="")
    pmn.add_notice("Draper", SOON, "1. Call to Order\n2. Site plan review, 9400 S 700 E\n3. Adjourn")
    pmn.add_notice("Sandy", SOON, "1. Call to Order\n2. Conditional use permit, 9400 South 700 East\n3. Adjourn")
    res = aw.run(aw.overlay_cfg_from_watch(cfg, watch, scan_all=True))
    assert res["failures"] == []
    assert street_hits(res) == [("Sandy", "9400 S 700 E")]


def test_cross_city_street_scoping_ogden_layton_and_provo_orem(pmn, cfg, owner_watch):
    watch = owner_watch(cities={"ogden": (1, "2549 Washington Blvd"), "orem": (1, "56 N State St")}, keywords="")
    for city in ("Layton", "Provo", "Ogden", "Orem"):
        pmn.add_notice(city, SOON, "1. Call to Order\n2. Review for 2549 Washington Blvd and 56 N State St\n3. Adjourn")
    res = aw.run(aw.overlay_cfg_from_watch(cfg, watch, scan_all=True))
    assert street_hits(res) == [("Ogden", "2549 Washington Blvd"), ("Orem", "56 N State St")]


def test_street_on_a_city_that_is_off_never_matches(pmn, cfg, owner_watch):
    watch = owner_watch(cities={"ch": (1, ""), "murray": (0, "10 E 4800 S")}, keywords="")
    pmn.add_notice("Murray", SOON, "1. Call to Order\n2. Variance at 10 E 4800 S\n3. Adjourn")
    res = aw.run(aw.overlay_cfg_from_watch(cfg, watch, scan_all=True))
    assert street_hits(res) == []


def test_daily_run_scans_all_ten_with_one_enabled_and_emails_only_enabled_city(pmn, owner_watch, run_main, sandbox):
    owner_watch(cities={"provo": (1, "")})
    pmn.add_notice("Provo", SOON, STR_AGENDA, title="Provo Council")
    pmn.add_notice("Draper", SOON, STR_AGENDA, title="Draper Council")   # off on watch
    pmn.add_notice("Layton", SOON, STR_AGENDA, title="Layton Council")   # off on watch
    code, out, err = run_main("run-owner")
    assert code == 4, (out, err)
    # every one of the supported cities was fetched (public bodies + notice list)
    for eid in pmn.entities.values():
        assert any(f"publicBodies.html?id={eid}&" in u for u in pmn.requests), eid
    for bodies in pmn.bodies.values():
        for bid in bodies.values():
            assert any(f"notices.html?id={bid}&" in u for u in pmn.requests), bid
    [ob] = emails(sandbox)
    assert [i["city"] for i in json.loads(ob.read_text())["items"]] == ["Provo"]
    s = summary(sandbox)
    assert [c["city"] for c in s["per_city"]] == ALL_CITIES
    by = {c["city"]: c for c in s["per_city"]}
    assert all(c["fetch"] == "ok" for c in s["per_city"])
    assert (by["Provo"]["enabled_on_watch"], by["Provo"]["new_upcoming_hits"], by["Provo"]["emailed"]) == (True, 1, 1)
    for off in ("Draper", "Layton"):
        assert (by[off]["enabled_on_watch"], by[off]["hits"], by[off]["new_upcoming_hits"], by[off]["emailed"]) == (False, 1, 1, 0)
    assert s["outcome"] == "hit" and s["cities_enabled_on_watch"] == ["Provo"]
    assert {(h["city"], h["emailed"]) for h in s["hit_items"]} == {("Provo", True), ("Draper", False), ("Layton", False)}
    assert "REPORT ONLY" in out and "Per-city summary" in out


def test_hits_only_in_off_cities_are_counted_but_silence(pmn, owner_watch, run_main, sandbox):
    owner_watch(cities={"ch": (1, "100 Example St")})
    pmn.add_notice("West Jordan", SOON, STR_AGENDA)
    code, out, err = run_main("run-owner")
    assert code == 0, (out, err)
    assert outbox_files(sandbox) == []
    s = summary(sandbox)
    assert s["outcome"] == "silence" and s["totals"]["new_upcoming_hits"] == 1 and s["totals"]["emailed_items"] == 0
    assert {c["city"]: c["hits"] for c in s["per_city"]}["West Jordan"] == 1


def test_per_city_summary_json_written_with_counts_and_denver_time(pmn, owner_watch, run_main, sandbox):
    owner_watch(cities={"ch": (1, ""), "slc": (1, "")})
    pmn.add_notice("Murray", SOON, "1. Call to Order\n2. Budget\n3. Adjourn")
    pmn.add_notice("Murray", FROZEN_NOW - dt.timedelta(days=3), "1. Call to Order\n2. Parks\n3. Adjourn")
    code, out, err = run_main("run-owner")
    assert code == 0, err
    s = summary(sandbox)
    assert s["run_time"].startswith("2026-10-07T15:00:00") and s["run_time"].endswith("-06:00")
    assert s["run_time_tz"] == "America/Denver"
    assert len(ALL_CITIES) == 17
    assert len(s["per_city"]) == 17 and s["totals"]["cities_scanned"] == 17 and s["totals"]["cities_fetch_ok"] == 17
    murray = next(c for c in s["per_city"] if c["city"] == "Murray")
    assert (murray["key"], murray["metro"], murray["notices_checked"], murray["documents_checked"],
            murray["fetch"], murray["hits"]) == ("murray", "Salt Lake metro", 2, 2, "ok", 0)
    assert s["totals"]["notices_checked"] == 2 and s["outcome"] == "silence" and s["hit_items"] == []
    for c in s["per_city"]:
        assert set(c) >= {"city", "notices_checked", "documents_checked", "fetch", "hits"}
        assert c["key"] == CITY_KEY[c["city"]]
    assert "Per-city summary" in out and "Murray" in out


def test_fetch_failure_in_one_city_reports_that_city_and_sends_nothing(pmn, owner_watch, run_main, sandbox):
    owner_watch(cities={"ch": (1, "")})
    pmn.add_notice("Cottonwood Heights", SOON, STR_AGENDA)  # would be an email if the run were clean
    layton_bid = next(iter(pmn.bodies[pmn.entities["Layton"]].values()))
    pmn.fail[f"notices.html?id={layton_bid}&"] = 503
    code, out, err = run_main("run-owner")
    assert code == 3, (out, err)
    assert "FAILED CITIES: Layton" in err and "HTTP 503" in err
    assert outbox_files(sandbox) == []
    assert "withheld" in out
    s = summary(sandbox)
    assert s["outcome"] == "failure" and s["failed_cities"] == ["Layton"]
    assert s["email_prepared"] is False and s["totals"]["emailed_items"] == 0
    assert {c["city"]: c["fetch"] for c in s["per_city"]} == {
        c: ("failed" if c == "Layton" else "ok") for c in ALL_CITIES}


def test_fatal_fetch_failure_writes_failure_summary_for_every_city(pmn, owner_watch, run_main, sandbox):
    owner_watch(cities={"ch": (1, "")})
    pmn.fail["/pmn/list/entities.html"] = 500
    code, out, err = run_main("run-owner")
    assert code == 2 and outbox_files(sandbox) == []
    s = summary(sandbox)
    assert s["outcome"] == "failure" and len(s["failed_cities"]) == 17 and s["fatal"]
