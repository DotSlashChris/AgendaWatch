"""PMN PDF HTTP 404 is skipped (city stays ok, alerts not withheld).
403, other non-404 HTTP errors, and timeouts still fail the city and withhold email."""
import datetime as dt
import json

import pytest
import requests

import agenda_watch as aw
from conftest import FROZEN_NOW

STR_ITEM = "2. Discussion of short-term rental regulations"
STR_AGENDA = f"1. Call to Order\n{STR_ITEM}\n3. Adjourn"
QUIET = "1. Call to Order\n2. Parks budget\n3. Adjourn"
SOON = FROZEN_NOW + dt.timedelta(days=5)
SANDY = "Sandy"
PDF404 = "https://www.utah.gov/pmn/files/40401.pdf"


@pytest.fixture(autouse=True)
def fake_pdftotext(monkeypatch):
    def extract(path):
        raw = path.read_bytes()
        if b"\n" in raw:
            return raw.split(b"\n", 1)[1].decode()
        return raw.decode()
    monkeypatch.setattr(aw, "extract_pdf_text", extract)


def emails(sandbox):
    return sorted(sandbox.outbox.glob("email-*.json"))


def summary(sandbox):
    return json.loads((sandbox.state / "last_run_summary.json").read_text())


def plan(sandbox):
    return json.loads((sandbox.state / "user_alert_plan.json").read_text())


def last_run(sandbox):
    return json.loads((sandbox.state / "last_run.json").read_text())


def test_get_doc_to_file_404_raises_pdfmissing_without_retry(pmn, sandbox, tmp_path):
    dest = tmp_path / "missing.pdf"
    before = list(pmn.requests)
    with pytest.raises(aw.PdfMissing) as ei:
        aw.get_doc_to_file(PDF404, dest)
    assert ei.value.url == PDF404
    assert ei.value.detail == "HTTP 404"
    assert str(ei.value) == f"PMN PDF missing (skipped): {PDF404} HTTP 404"
    got = [u for u in pmn.requests if u not in before and "40401" in u]
    assert got == [PDF404]
    assert not dest.exists()


def test_pmn_pdf_404_is_skipped_city_ok_email_not_withheld(pmn, owner_watch, run_main, sandbox):
    owner_watch(cities={"sandy": (1, "")})
    pmn.add_notice(SANDY, SOON, QUIET, title="Missing packet", files=[40401])
    pmn.set_file(8001, f"Agenda packet\n{STR_ITEM}\nAdjourn")
    pmn.add_notice(SANDY, SOON, STR_AGENDA, title="Council", files=[8001])
    code, out, err = run_main("run-owner")
    assert code == 4, (out, err)
    assert "PMN PDF missing (skipped):" in err
    assert "40401.pdf" in err
    assert "HTTP 404" in err
    assert "alerts not withheld" in err
    s = summary(sandbox)
    by = {c["city"]: c for c in s["per_city"]}
    assert by["Sandy"]["fetch"] == "ok"
    assert by["Sandy"]["failures"] == []
    assert any("40401" in x and "HTTP 404" in x for x in by["Sandy"]["skipped"])
    assert s["failed_cities"] == []
    assert s["outcome"] == "hit"
    assert s["email_prepared"] is True
    assert emails(sandbox), "alert email should have been prepared"
    assert plan(sandbox)["mode"] != "withheld"
    lr = last_run(sandbox)
    assert lr["failures"] == []
    assert any("PMN PDF missing (skipped):" in x and "40401" in x for x in lr["skipped_pdfs"])


def test_pmn_pdf_403_still_fails_city_and_withholds_email(pmn, owner_watch, run_main, sandbox):
    owner_watch(cities={"sandy": (1, "")})
    pmn.fail["/pmn/files/40301.pdf"] = 403
    pmn.add_notice(SANDY, SOON, STR_AGENDA, title="Council", files=[40301])
    code, out, err = run_main("run-owner")
    assert code == 3, (out, err)
    assert "FETCH FAILURES" in out
    s = summary(sandbox)
    by = {c["city"]: c for c in s["per_city"]}
    assert by["Sandy"]["fetch"] == "failed"
    assert by["Sandy"]["failures"]
    assert any("403" in f for f in by["Sandy"]["failures"])
    assert "Sandy" in s["failed_cities"]
    assert emails(sandbox) == []
    assert plan(sandbox)["mode"] == "withheld"
    assert last_run(sandbox)["failures"]


def test_pmn_pdf_timeout_still_fails_city_and_withholds_email(pmn, owner_watch, run_main, sandbox):
    owner_watch(cities={"sandy": (1, "")})
    pmn.fail["/pmn/files/70001.pdf"] = requests.Timeout("timed out")
    pmn.add_notice(SANDY, SOON, STR_AGENDA, title="Council", files=[70001])
    code, out, err = run_main("run-owner")
    assert code == 3, (out, err)
    s = summary(sandbox)
    by = {c["city"]: c for c in s["per_city"]}
    assert by["Sandy"]["fetch"] == "failed"
    assert by["Sandy"]["failures"]
    assert emails(sandbox) == []
    assert plan(sandbox)["mode"] == "withheld"


def test_pmn_pdf_500_still_fails_city_and_withholds_email(pmn, owner_watch, run_main, sandbox):
    owner_watch(cities={"sandy": (1, "")})
    pmn.fail["/pmn/files/50001.pdf"] = 500
    pmn.add_notice(SANDY, SOON, STR_AGENDA, title="Council", files=[50001])
    code, out, err = run_main("run-owner")
    assert code == 3, (out, err)
    s = summary(sandbox)
    by = {c["city"]: c for c in s["per_city"]}
    assert by["Sandy"]["fetch"] == "failed"
    assert any("500" in f for f in by["Sandy"]["failures"])
    assert emails(sandbox) == []
    assert plan(sandbox)["mode"] == "withheld"


def test_notice_page_404_still_fails_city_and_withholds_email(pmn, owner_watch, run_main, sandbox):
    owner_watch(cities={"sandy": (1, "")})
    url = pmn.add_notice(SANDY, SOON, STR_AGENDA, title="Council")
    pmn.fail[url] = 404
    code, out, err = run_main("run-owner")
    assert code == 3, (out, err)
    s = summary(sandbox)
    by = {c["city"]: c for c in s["per_city"]}
    assert by["Sandy"]["fetch"] == "failed"
    assert by["Sandy"]["failures"]
    assert emails(sandbox) == []
    assert plan(sandbox)["mode"] == "withheld"
