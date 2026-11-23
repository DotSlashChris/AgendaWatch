"""Dead PMN PDF URLs are remembered and stop being daily errors; a new attachment
is still fetched. A city listing page that keeps returning HTTP 403 is reported
once as known-blocked (PMN coverage stays) and is not a fresh daily error."""
import datetime as dt
import json
import types

import pytest
import requests

import agenda_watch as aw
import aw_city_sources as acs
import aw_store
from conftest import FROZEN_NOW, TZ
from test_city_sources_20261031 import (
    AGENDA_TEXT, AGENDA_URL, CH_URL, PACKET, PACKET_URL, PAGE_HTML, _cities, _con, _fake_doc,
)

STR_ITEM = "2. Discussion of short-term rental regulations"
STR_AGENDA = f"1. Call to Order\n{STR_ITEM}\n3. Adjourn"
QUIET = "1. Call to Order\n2. Parks budget\n3. Adjourn"
SOON = FROZEN_NOW + dt.timedelta(days=5)
SANDY = "Sandy"
PDF404 = "https://www.utah.gov/pmn/files/40401.pdf"
PDF_OK = "https://www.utah.gov/pmn/files/8002.pdf"


@pytest.fixture(autouse=True)
def fake_pdftotext(monkeypatch):
    def extract(path):
        raw = path.read_bytes()
        if b"\n" in raw:
            return raw.split(b"\n", 1)[1].decode()
        return raw.decode()
    monkeypatch.setattr(aw, "extract_pdf_text", extract)


def set_clock(monkeypatch, when):
    class FrozenDateTime(dt.datetime):
        @classmethod
        def now(cls, tz=None):
            return when.astimezone(tz) if tz else when.replace(tzinfo=None)

    monkeypatch.setattr(aw, "dt", types.SimpleNamespace(datetime=FrozenDateTime, timedelta=dt.timedelta))
    return when


def pdf_gets(pmn, fid):
    return [u for u in pmn.requests if f"/pmn/files/{fid}." in u]


def last_run(sandbox):
    return json.loads((sandbox.state / "last_run.json").read_text())


def summary(sandbox):
    return json.loads((sandbox.state / "last_run_summary.json").read_text())


def test_record_pdf_404_same_day_does_not_go_permanent():
    now = FROZEN_NOW
    rows = {}
    assert aw_store.record_pdf_404(rows, PDF404, "n", now) == f"PMN PDF missing (skipped): {PDF404} HTTP 404"
    assert aw_store.record_pdf_404(rows, PDF404, "n", now + dt.timedelta(hours=2)) == (
        f"PMN PDF missing (skipped): {PDF404} HTTP 404"
    )
    assert rows[PDF404]["consecutive_dates"] == 1
    assert rows[PDF404]["status"] == "watching"
    nxt = now + dt.timedelta(days=1)
    assert aw_store.record_pdf_404(rows, PDF404, "n", nxt) == (
        f"PMN PDF permanently missing (will not retry daily): {PDF404}"
    )
    assert rows[PDF404]["status"] == "permanent"
    assert aw_store.record_pdf_404(rows, PDF404, "n", nxt + dt.timedelta(days=1)) is None


def test_404_second_day_is_permanent_then_quiet(pmn, owner_watch, run_main, sandbox, monkeypatch):
    owner_watch(cities={"sandy": (1, "")})
    pmn.add_notice(SANDY, SOON, QUIET, title="Missing packet", files=[40401])

    set_clock(monkeypatch, FROZEN_NOW)
    code, out, err = run_main("run-owner")
    assert code == 0, (out, err)
    assert f"PMN PDF missing (skipped): {PDF404} HTTP 404" in err
    assert "permanently missing" not in err
    assert pdf_gets(pmn, 40401)
    s = summary(sandbox)
    by = {c["city"]: c for c in s["per_city"]}
    assert by["Sandy"]["fetch"] == "ok"
    assert by["Sandy"]["failures"] == []
    assert s["failed_cities"] == []

    pmn.requests.clear()
    set_clock(monkeypatch, FROZEN_NOW + dt.timedelta(days=1))
    code, out, err = run_main("run-owner")
    assert code == 0, (out, err)
    assert f"PMN PDF permanently missing (will not retry daily): {PDF404}" in err
    assert f"PMN PDF missing (skipped): {PDF404}" not in err
    assert pdf_gets(pmn, 40401)
    lr = last_run(sandbox)
    assert any("permanently missing" in x and "40401" in x for x in lr["skipped_pdfs"])

    pmn.requests.clear()
    set_clock(monkeypatch, FROZEN_NOW + dt.timedelta(days=2))
    code, out, err = run_main("run-owner")
    assert code == 0, (out, err)
    assert "40401" not in err
    assert "PMN PDF missing" not in err
    assert "permanently missing" not in err
    assert pdf_gets(pmn, 40401) == []
    lr = last_run(sandbox)
    assert lr["skipped_pdfs"] == []
    assert by_ok(sandbox, "Sandy")


def by_ok(sandbox, city):
    s = summary(sandbox)
    by = {c["city"]: c for c in s["per_city"]}
    assert by[city]["fetch"] == "ok"
    assert by[city]["failures"] == []
    assert s["failed_cities"] == []
    return True


def test_new_attachment_on_same_notice_is_still_fetched(
    pmn, owner_watch, run_main, sandbox, monkeypatch,
):
    owner_watch(cities={"sandy": (1, "")})
    notice = pmn.add_notice(SANDY, SOON, QUIET, title="Council", files=[40401])
    nid = notice.rsplit("/", 1)[-1].split(".")[0]

    set_clock(monkeypatch, FROZEN_NOW)
    run_main("run-owner")
    set_clock(monkeypatch, FROZEN_NOW + dt.timedelta(days=1))
    run_main("run-owner")

    pmn.details[nid]["files"] = [40401, 8002]
    pmn.set_file(8002, f"Agenda packet\n{STR_ITEM}\nAdjourn")
    pmn.requests.clear()
    set_clock(monkeypatch, FROZEN_NOW + dt.timedelta(days=2))
    code, out, err = run_main("run-owner")
    assert code == 4, (out, err)
    assert pdf_gets(pmn, 40401) == []
    assert pdf_gets(pmn, 8002)
    assert "40401" not in err
    s = summary(sandbox)
    assert s["outcome"] == "hit"
    assert s["email_prepared"] is True
    by_ok(sandbox, "Sandy")


def test_permanent_pdf_recovered_on_weekly_recheck(
    pmn, owner_watch, run_main, sandbox, monkeypatch,
):
    owner_watch(cities={"sandy": (1, "")})
    later = FROZEN_NOW + dt.timedelta(days=20)
    pmn.add_notice(SANDY, later, QUIET, title="Missing then back", files=[40401])
    set_clock(monkeypatch, FROZEN_NOW)
    run_main("run-owner")
    set_clock(monkeypatch, FROZEN_NOW + dt.timedelta(days=1))
    run_main("run-owner")

    pmn.set_file(40401, f"Agenda packet\n{STR_ITEM}\nAdjourn")
    pmn.requests.clear()
    # last checked on day+1; +7 days is due for recheck
    set_clock(monkeypatch, FROZEN_NOW + dt.timedelta(days=8))
    code, out, err = run_main("run-owner")
    assert code == 4, (out, err)
    assert pdf_gets(pmn, 40401)
    assert "permanently missing" not in err
    assert "PMN PDF missing (skipped)" not in err
    by_ok(sandbox, "Sandy")
    with __import__("sqlite3").connect(sandbox.db) as con:
        left = list(con.execute("SELECT url FROM missing_pmn_pdfs WHERE url=?", (PDF404,)))
    assert left == []


def test_pmn_pdf_403_and_timeout_still_fail_after_this_change(
    pmn, owner_watch, run_main, sandbox,
):
    owner_watch(cities={"sandy": (1, "")})
    pmn.fail["/pmn/files/40301.pdf"] = 403
    pmn.add_notice(SANDY, SOON, STR_AGENDA, title="Council", files=[40301])
    code, out, err = run_main("run-owner")
    assert code == 3, (out, err)
    assert summary(sandbox)["failed_cities"] == ["Sandy"]


def test_city_source_403_second_date_is_known_blocked_then_quiet(tmp_path):
    con = _con(tmp_path)

    def boom(u):
        raise acs.CityFetchError(f"GET {u} failed: HTTP 403")

    day1 = dt.datetime(2026, 10, 31, 8, 21, tzinfo=TZ)
    _e, _s, failures = acs.scan({"cities": _cities()}, con, day1, {}, fetch_page_fn=boom)
    assert failures == [f"city source blocked: {CH_URL} HTTP 403"]

    day2 = day1 + dt.timedelta(days=1)
    _e, stats, failures = acs.scan({"cities": _cities()}, con, day2, {}, fetch_page_fn=boom)
    assert len(failures) == 1
    assert failures[0].startswith(f"city source known-blocked: {CH_URL} HTTP 403")
    assert "still covered by Utah Public Notice" in failures[0]
    assert "daily error" in failures[0]
    row = con.execute(
        "SELECT block_status, block_count, known_blocked_logged FROM city_sources WHERE url=?",
        (CH_URL,),
    ).fetchone()
    assert row == ("known", 2, 1)

    calls = []

    def counted(u):
        calls.append(u)
        raise acs.CityFetchError(f"GET {u} failed: HTTP 403")

    day3 = day1 + dt.timedelta(days=2)
    _e, stats, failures = acs.scan({"cities": _cities()}, con, day3, {}, fetch_page_fn=counted)
    assert failures == []
    assert calls == []
    assert CH_URL in stats["known_blocked"]
    # Source is still stored; Cottonwood Heights PMN coverage is untouched.
    assert [r["url"] for r in acs.sources(con)] == [CH_URL]


def test_city_source_known_blocked_weekly_recheck_and_recovery(tmp_path):
    con = _con(tmp_path)

    def boom(u):
        raise acs.CityFetchError(f"GET {u} failed: HTTP 403")

    day1 = dt.datetime(2026, 10, 31, 8, 21, tzinfo=TZ)
    acs.scan({"cities": _cities()}, con, day1, {}, fetch_page_fn=boom)
    day2 = day1 + dt.timedelta(days=1)
    acs.scan({"cities": _cities()}, con, day2, {}, fetch_page_fn=boom)

    calls = []

    def counted_boom(u):
        calls.append(u)
        raise acs.CityFetchError(f"GET {u} failed: HTTP 403")

    # last_checked is day2; exactly +7 days is due
    day_recheck = day2 + dt.timedelta(days=7)
    _e, _s, failures = acs.scan({"cities": _cities()}, con, day_recheck, {}, fetch_page_fn=counted_boom)
    assert calls == [CH_URL]
    assert failures == []  # already reported as known-blocked

    calls.clear()
    entries, stats, failures = acs.scan(
        {"cities": _cities()}, con, day_recheck + dt.timedelta(days=7), {},
        fetch_page_fn=lambda u: PAGE_HTML,
        fetch_doc_fn=_fake_doc({AGENDA_URL: AGENDA_TEXT, PACKET_URL: PACKET}),
    )
    assert failures == []
    assert stats["pages_ok"] == 1
    assert entries == []  # first successful scan is baselined; no backfill
    assert con.execute("SELECT block_status, last_error FROM city_sources").fetchone() == (None, None)
    src_stats = stats["per_source"][0]
    assert src_stats.get("first_scan") is True


def test_city_source_timeout_is_still_a_daily_report(tmp_path):
    con = _con(tmp_path)

    def boom(u):
        raise acs.CityFetchError("GET failed: timed out")

    day1 = dt.datetime(2026, 10, 31, 8, 21, tzinfo=TZ)
    _e, _s, f1 = acs.scan({"cities": _cities()}, con, day1, {}, fetch_page_fn=boom)
    _e, _s, f2 = acs.scan({"cities": _cities()}, con, day1 + dt.timedelta(days=1), {}, fetch_page_fn=boom)
    assert f1 == [f"city source blocked: {CH_URL} timed out"]
    assert f2 == [f"city source blocked: {CH_URL} timed out"]
