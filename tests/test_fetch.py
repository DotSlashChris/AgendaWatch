"""Fetch / run / run-owner flow with a fake PMN (no live HTTP, no email, no Gmail, no Stripe)."""
import datetime as dt
import json

import requests

import agenda_watch as aw
from conftest import CH, SLC, FROZEN_NOW, TZ, outbox_files

STR_ITEM = "2. Discussion of short-term rental regulations"
STR_AGENDA = f"1. Call to Order\n{STR_ITEM}\n3. Adjourn"
QUIET_AGENDA = "1. Call to Order\n2. Approval of the budget for parks maintenance\n3. Adjourn"


def emails(sandbox):
    return sorted(sandbox.outbox.glob("email-*.json"))


# 4 -----------------------------------------------------------------------------------
def test_past_meeting_is_example_not_email(pmn, cfg, sandbox):
    cfg["cities"][SLC]["enabled"] = False
    pmn.add_notice(CH, FROZEN_NOW - dt.timedelta(days=10), STR_AGENDA, title="Past Council Meeting")
    res = aw.run(cfg)
    assert [h["upcoming"] for h in res["hits"]] == [False]
    code, ob, items, past = aw.write_run_artifacts(cfg, res, history=[])
    assert code == 0 and ob is None and items == []
    assert [p["item_text"] for p in past] == [STR_ITEM]
    assert emails(sandbox) == [] and outbox_files(sandbox) == []
    summary = json.loads(next(sandbox.state.glob("match-summary-*.json")).read_text())
    assert summary["upcoming_alert_candidates"] == []
    assert [p["item_text"] for p in summary["past_examples_not_alerted"]] == [STR_ITEM]


def test_upcoming_meeting_produces_owner_email(pmn, cfg, sandbox):
    cfg["cities"][SLC]["enabled"] = False
    pmn.add_notice(CH, FROZEN_NOW + dt.timedelta(days=10), STR_AGENDA, title="Upcoming Council Meeting")
    res = aw.run(cfg)
    code, ob, items, past = aw.write_run_artifacts(cfg, res, history=[])
    assert code == 4 and past == []
    assert emails(sandbox) == [ob]
    email = json.loads(ob.read_text())
    assert email["to"] == "owner@example.com"
    assert [i["item_text"] for i in email["items"]] == [STR_ITEM]
    assert email["body"].rstrip().endswith(aw.FOOTER)


def test_past_vs_upcoming_uses_america_denver_time(pmn, cfg, sandbox):
    """now = Oct 7 2026 3:00 PM MDT. 2:00 PM local today is past; 6:00 PM local today is upcoming
    (6:00 PM read as UTC would be noon MDT, i.e. past — so this pins the America/Denver rule)."""
    cfg["cities"][SLC]["enabled"] = False
    earlier = "1. Short-term rental appeal for 100 Earlier Way"
    later = "1. Short-term rental appeal for 200 Later Way"
    pmn.add_notice(CH, dt.datetime(2026, 10, 7, 14, 0, tzinfo=TZ), earlier, title="Earlier Today")
    pmn.add_notice(CH, dt.datetime(2026, 10, 7, 18, 0, tzinfo=TZ), later, title="Later Today")
    res = aw.run(cfg)
    by_item = {h["item_text"]: h["upcoming"] for h in res["hits"]}
    assert by_item == {earlier: False, later: True}
    code, ob, items, past = aw.write_run_artifacts(cfg, res, history=[])
    assert code == 4
    assert [i["item_text"] for i in json.loads(ob.read_text())["items"]] == [later]
    assert [p["item_text"] for p in past] == [earlier]


def test_already_sent_item_is_not_emailed_again(pmn, cfg, sandbox):
    cfg["cities"][SLC]["enabled"] = False
    pmn.add_notice(CH, FROZEN_NOW + dt.timedelta(days=3), STR_AGENDA)
    res = aw.run(cfg)
    history = [{"key": h["key"]} for h in res["hits"]]
    code, ob, items, _ = aw.write_run_artifacts(cfg, res, history=history)
    assert code == 0 and ob is None and emails(sandbox) == []


# 5 -----------------------------------------------------------------------------------
def _street_hits(res):
    return sorted((h["city"], h["matched"]) for h in res["hits"] if h["match_type"] == "address")


def test_ch_street_matches_only_in_cottonwood_heights(pmn, cfg, owner_watch):
    watch = owner_watch(city_ch=1, city_slc=1, keywords="", street_ch="100 Example St", street_slc="")
    when = FROZEN_NOW + dt.timedelta(days=5)
    pmn.add_notice(CH, when, "1. Call to Order\n2. Site plan review, 100 Example St\n3. Adjourn")
    pmn.add_notice(SLC, when, "1. Call to Order\n2. Variance request, 100 Example St\n3. Adjourn")
    res = aw.run(aw.overlay_cfg_from_watch(cfg, watch))
    assert res["failures"] == []
    assert _street_hits(res) == [(CH, "100 Example St")]


def test_slc_street_matches_only_in_salt_lake_city(pmn, cfg, owner_watch):
    watch = owner_watch(city_ch=1, city_slc=1, keywords="", street_ch="", street_slc="451 S State St")
    when = FROZEN_NOW + dt.timedelta(days=5)
    pmn.add_notice(CH, when, "1. Call to Order\n2. Presentation by owner of 451 S State St\n3. Adjourn")
    pmn.add_notice(SLC, when, "1. Call to Order\n2. Design review, 451 South State Street\n3. Adjourn")
    res = aw.run(aw.overlay_cfg_from_watch(cfg, watch))
    assert res["failures"] == []
    assert _street_hits(res) == [(SLC, "451 S State St")]


def test_run_owner_street_scoping_end_to_end(pmn, owner_watch, run_main, sandbox):
    owner_watch(city_ch=1, city_slc=1, keywords="", street_ch="100 Example St", street_slc="")
    when = FROZEN_NOW + dt.timedelta(days=5)
    pmn.add_notice(SLC, when, "1. Call to Order\n2. Variance request, 100 Example St\n3. Adjourn")
    code, out, err = run_main("run-owner")
    assert code == 0, (out, err)  # SLC-only mention of a CH street -> silence
    assert outbox_files(sandbox) == []
    pmn.add_notice(CH, when, "1. Call to Order\n2. Site plan review, 100 Example St\n3. Adjourn")
    code, out, err = run_main("run-owner")
    assert code == 4, (out, err)
    [ob] = emails(sandbox)
    assert [(i["city"], i["item_text"]) for i in json.loads(ob.read_text())["items"]] == [
        (CH, "2. Site plan review, 100 Example St")
    ]


# 6 -----------------------------------------------------------------------------------
def test_failed_fetch_http_error_stops_and_reports_no_email(pmn, owner_watch, run_main, sandbox):
    owner_watch(city_ch=1, city_slc=0)
    pmn.add_notice(CH, FROZEN_NOW + dt.timedelta(days=5), STR_AGENDA)  # would be a hit if fetch worked
    pmn.fail["/pmn/list/entities.html"] = 503
    code, out, err = run_main("run-owner")
    assert code == 2
    assert "FAILURES" in err and "HTTP 503" in err
    assert "Owner-only email prepared" not in out and "Silence" not in out
    assert outbox_files(sandbox) == []  # nothing sent or queued, no "no change" email
    assert len(list(sandbox.state.glob("failures-*.txt"))) == 1
    assert json.loads((sandbox.state / "last_run.json").read_text())["fatal"]
    assert sum("entities.html" in u for u in pmn.requests) == 3  # retried, then gave up
    assert not any("notice" in u for u in pmn.requests)  # stopped: no further fetching


def test_failed_fetch_network_exception_stops_and_reports_no_email(pmn, cfg, owner_watch, run_main, sandbox):
    owner_watch(city_ch=1, city_slc=0)
    pmn.fail["/pmn/list/entities.html"] = requests.ConnectionError("connection refused")
    code, out, err = run_main("run")
    assert code == 2
    assert "ConnectionError" in err
    assert outbox_files(sandbox) == []


def test_partial_fetch_failure_reports_and_sends_nothing(pmn, owner_watch, run_main, sandbox):
    owner_watch(city_ch=1, city_slc=0)
    pmn.add_notice(CH, FROZEN_NOW + dt.timedelta(days=5), QUIET_AGENDA)
    pmn.fail["/pmn/list/notices.html"] = 500
    code, out, err = run_main("run-owner")
    assert code == 3  # silence with failures -> bot must report, not stay quiet
    assert "HTTP 500" in err
    assert outbox_files(sandbox) == []


def test_normal_run_with_zero_hits_sends_nothing(pmn, owner_watch, run_main, sandbox):
    owner_watch(city_ch=1, city_slc=1, street_ch="100 Example St", street_slc="451 S State St")
    pmn.add_notice(CH, FROZEN_NOW + dt.timedelta(days=5), QUIET_AGENDA)
    pmn.add_notice(SLC, FROZEN_NOW + dt.timedelta(days=6), QUIET_AGENDA)
    code, out, err = run_main("run-owner")
    assert code == 0, err
    assert "No new UPCOMING hits. Silence (no email)." in out
    assert outbox_files(sandbox) == []  # no "no change" email
    assert not list(sandbox.state.glob("failures-*.txt"))


def test_run_owner_alerts_off_does_not_fetch(pmn, owner_watch, run_main, sandbox):
    owner_watch(alerts_on=0)
    code, out, err = run_main("run-owner")
    assert code == 0 and "ALERTS_OFF" in out
    assert pmn.requests == [] and outbox_files(sandbox) == []


def test_run_owner_missing_watch_is_soft_error(pmn, run_main, sandbox):
    code, out, err = run_main("run-owner")
    assert code == 1 and "SOFT_ERROR" in err
    assert pmn.requests == [] and outbox_files(sandbox) == []
