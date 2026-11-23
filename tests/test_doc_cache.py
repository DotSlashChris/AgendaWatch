"""Document cache: notice-level skip, changed notices, conditional GET, re-match, purge."""
import datetime as dt
import sqlite3

import pytest

import agenda_watch as aw
import aw_store
from conftest import CH, SLC, FROZEN_NOW

STR_ITEM = "4. Ordinance amending short-term rental licensing"
QUIET = "1. Call to Order\n2. Parks budget\n3. Adjourn"


@pytest.fixture(autouse=True)
def fake_pdftotext(monkeypatch):
    calls = []

    def extract(path):
        calls.append(path)
        return path.read_bytes().split(b"\n", 1)[1].decode()
    monkeypatch.setattr(aw, "extract_pdf_text", extract)
    return calls


def one_run(cfg):
    res = aw.run(cfg)
    aw.log_run(aw.dt.datetime.now(aw.TZ), 0, res)   # persists notice fingerprints like run-owner does
    return res


def pdf_gets(pmn):
    return [u for u in pmn.requests if "/pmn/files/" in u]


def setup(pmn, cfg, edited="10/01/2026 9:00 AM"):
    cfg["cities"][SLC]["enabled"] = False
    pmn.set_file(501, f"Agenda packet\n{STR_ITEM}\nAdjourn")
    pmn.set_file(502, "Minutes\nnothing here")
    pmn.add_notice(CH, FROZEN_NOW + dt.timedelta(days=5), QUIET, files=[501, 502], edited=edited)


def hit_keys(res):
    return sorted(h["key"] for h in res["hits"])


def test_unchanged_notice_skips_attachments(pmn, cfg, sandbox, fake_pdftotext):
    setup(pmn, cfg)
    r1 = one_run(cfg)
    assert len(pdf_gets(pmn)) == 2 and r1["fetch_stats"]["docs_fetched"] == 2
    pmn.requests.clear()
    r2 = one_run(cfg)
    assert pdf_gets(pmn) == []                       # no attachment request at all
    assert r2["fetch_stats"]["notices_unchanged"] == 1 and r2["fetch_stats"]["docs_reused"] == 2
    assert hit_keys(r1) == hit_keys(r2) and len(r1["hits"]) == 2
    assert len(fake_pdftotext) == 2
    assert list((sandbox.root / "cache").glob("*")) == []   # PDFs deleted after extraction


def test_changed_notice_uses_conditional_get(pmn, cfg, sandbox):
    setup(pmn, cfg)
    one_run(cfg)
    pmn.details[next(iter(pmn.details))]["edited"] = "10/06/2026 4:00 PM"
    pmn.set_file(502, "Minutes\nnow mentions short-term rental moratorium extension", etag='"502-v2"')
    pmn.requests.clear(); pmn.cond = []
    r = one_run(cfg)
    assert sorted(pdf_gets(pmn)) == sorted([aw_url(501), aw_url(502)])
    assert all("If-None-Match" in h for _, h in pmn.cond)
    st = r["fetch_stats"]
    assert st["docs_304"] == 1 and st["docs_fetched"] == 1
    assert len(r["hits"]) > 2                        # new text in changed doc matched
    with sqlite3.connect(sandbox.db) as con:
        assert con.execute("SELECT etag FROM documents WHERE url=?", (aw_url(502),)).fetchone()[0] == '"502-v2"'


def test_new_attachment_on_notice_is_fetched(pmn, cfg, sandbox):
    setup(pmn, cfg)
    one_run(cfg)
    pmn.set_file(503, "Addendum\nshort-term rental fee schedule")
    d = pmn.details[next(iter(pmn.details))]
    d["files"].append(503)                             # same Last Edited, but attachment list changed
    pmn.requests.clear()
    r = one_run(cfg)
    assert r["fetch_stats"]["docs_fetched"] == 1 and r["fetch_stats"]["docs_304"] == 2


def test_stored_text_rematched_against_current_watches(pmn, cfg, sandbox):
    setup(pmn, cfg)
    pmn.set_file(502, "Minutes\nSite plan review for 1450 E Maple Ave")
    r1 = one_run(cfg)
    assert not any("Maple" in h["item_text"] for h in r1["hits"])
    cfg["cities"][CH]["street"] = "Maple Ave"        # watch changed after the docs were cached
    cfg.setdefault("city_streets", {})[CH] = "Maple Ave"
    pmn.requests.clear()
    r2 = one_run(cfg)
    assert pdf_gets(pmn) == [] and r2["fetch_stats"]["docs_reused"] == 2
    assert any("Maple" in h["item_text"] for h in r2["hits"])


def test_failed_pdf_reports_and_is_not_cached(pmn, cfg, sandbox):
    setup(pmn, cfg)
    pmn.fail["/pmn/files/501"] = 503
    r1 = one_run(cfg)
    assert r1["failures"] and r1["fetch_stats"]["docs_fetched"] == 1
    del pmn.fail["/pmn/files/501"]
    pmn.requests.clear()
    r2 = one_run(cfg)
    assert aw_url(501) in pdf_gets(pmn) and len(r2["hits"]) == 2   # not skipped: doc missing from cache


def test_purge_removes_old_documents(sandbox):
    with sqlite3.connect(sandbox.db) as con:
        aw_store.ensure_schema(con)
        aw_store.save_documents(con, [
            {"url": "u-old", "text_hash": "h", "text": "t"}, {"url": "u-new", "text_hash": "h", "text": "t"}],
            [], now="2025-01-01T00:00:00+00:00")
        con.execute("UPDATE documents SET checked_at=? WHERE url='u-new'", (aw_store.now_iso(),))
        aw_store.purge(con, days=365)
        assert [r[0] for r in con.execute("SELECT url FROM documents")] == ["u-new"]


def aw_url(fid):
    return f"https://www.utah.gov/pmn/files/{fid}.pdf"
