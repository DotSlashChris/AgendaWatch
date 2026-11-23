"""Owner cap (Nov 4 2026): a city-page document alerts only if its meeting is upcoming AND it was first seen
after the source was added; a source's first successful scan is baseline (no backfill). Sandbox only."""
import datetime as dt
import sqlite3

import aw_alerts
import aw_city_sources as acs
import agenda_watch as aw
from test_city_sources_20261031 import (AGENDA_TEXT, AGENDA_URL, CH_URL, CHRIS, OCT31, PACKET, PACKET_URL,
                                        PAGE_HTML, _cities, _con, _fake_doc)

NEW_URL = "https://www.cottonwoodheights.utah.gov/home/showpublisheddocument/fixture-nov18-packet"
NEW_ROW = ('<tr><td>November 18, 2026 Planning Commission</td>'
           '<td><a href="/home/showpublisheddocument/fixture-nov18-packet">Packet</a></td></tr>\n')
PAGE2 = PAGE_HTML.replace("<tbody>\n", "<tbody>\n" + NEW_ROW, 1)
OLD_ROW = ('<tr><td>October 28, 2026 Planning Commission</td>'
           '<td><a href="/home/showpublisheddocument/fixture-oct28-packet">Packet</a></td></tr>\n')
TEXTS = {AGENDA_URL: AGENDA_TEXT, PACKET_URL: PACKET, NEW_URL: PACKET.replace("November 4, 2026", "November 18, 2026")}
NOV1 = dt.datetime(2026, 11, 1, 9, 0, tzinfo=aw.TZ)


def _run(con, now, page, persist=True, fetch=None):
    return acs.scan({"cities": _cities()}, con, now, {}, fetch_page_fn=lambda u: page,
                    fetch_doc_fn=fetch or _fake_doc(TEXTS), persist=persist)


def _src_added(con, when):
    con.execute("UPDATE city_sources SET added_at=?", (when.isoformat(),))


def test_first_scan_is_baseline_no_backfill(tmp_path):
    con = _con(tmp_path); _src_added(con, OCT31 - dt.timedelta(days=1))
    entries, stats, _ = _run(con, OCT31, PAGE_HTML)
    assert entries == [] and stats["per_source"][0]["first_scan"] is True
    assert stats["docs_capped"] == 2                                   # Nov 4 agenda + packet: baseline
    rows = con.execute("SELECT doc_url, baseline FROM city_source_docs").fetchall()
    assert rows and all(b == 1 for _, b in rows)                       # every listed link, past ones too
    assert aw_alerts.match_user(entries, CHRIS, OCT31) == []


def test_new_upcoming_document_after_baseline_alerts_through_normal_path(tmp_path):
    con = _con(tmp_path); _src_added(con, OCT31 - dt.timedelta(days=1))
    _run(con, OCT31, PAGE_HTML)                                        # baseline
    entries, stats, _ = _run(con, NOV1, PAGE2)
    assert [d[1] for e in entries for d in e["docs"]] == [NEW_URL]      # only the new packet
    hits = aw_alerts.match_user(entries, CHRIS, NOV1)
    assert hits and all(h["kind"] == "upcoming" and h["link"] == NEW_URL for h in hits)
    mails = aw_alerts.build_user_emails([aw_alerts._brief(h) for h in hits], "https://agendawatch.app")
    assert mails[0][1] == "Agenda Watch: Cottonwood Heights Planning Commission — November 18, 2026"
    entries3, _, _ = _run(con, NOV1 + dt.timedelta(days=1), PAGE2)      # still eligible next day (dedupe elsewhere)
    assert [d[1] for e in entries3 for d in e["docs"]] == [NEW_URL]


def test_new_link_with_past_meeting_date_is_never_eligible(tmp_path):
    con = _con(tmp_path); _src_added(con, OCT31 - dt.timedelta(days=1))
    _run(con, OCT31, PAGE_HTML)
    entries, _, _ = _run(con, NOV1, PAGE_HTML.replace("<tbody>\n", "<tbody>\n" + OLD_ROW, 1))
    assert entries == []
    (b,) = con.execute("SELECT baseline FROM city_source_docs WHERE doc_url LIKE '%oct28%'").fetchone()
    assert b == 1


def test_first_seen_before_source_added_is_not_eligible(tmp_path):
    con = _con(tmp_path); _src_added(con, NOV1 + dt.timedelta(hours=1))   # source "added" after the scan time
    con.execute("INSERT INTO city_source_docs VALUES('ch',?,'x',NULL,?,1)", (CH_URL, OCT31.isoformat()))
    entries, _, _ = _run(con, NOV1, PAGE2)
    assert entries == []


def test_blocked_pdf_link_only_only_for_eligible_documents(tmp_path):
    def blocked(url, page_url, prev):
        raise acs.CityFetchError(f"GET {url} failed: HTTP 403")
    con = _con(tmp_path); _src_added(con, OCT31 - dt.timedelta(days=1))
    _, stats, failures = _run(con, OCT31, PAGE_HTML, fetch=blocked)     # first scan: baseline, not even requested
    assert stats["unreadable"] == [] and failures == [] and stats["docs_skipped"] == 2
    _, stats, _ = _run(con, NOV1, PAGE2, fetch=blocked)
    assert [u["link"] for u in stats["unreadable"]] == [NEW_URL]        # new upcoming packet: link-only


def test_page_blocked_records_nothing_and_read_only_check_writes_nothing(tmp_path):
    con = _con(tmp_path)
    def down(u):
        raise acs.CityFetchError("GET failed: HTTP 403")
    acs.scan({"cities": _cities()}, con, OCT31, {}, fetch_page_fn=down, fetch_doc_fn=_fake_doc(TEXTS))
    assert con.execute("SELECT COUNT(*) FROM city_source_docs").fetchone()[0] == 0
    entries, stats, _ = _run(con, OCT31, PAGE_HTML, persist=False)
    assert entries == [] and stats["per_source"][0]["first_scan"] is True
    assert con.execute("SELECT COUNT(*) FROM city_source_docs").fetchone()[0] == 0


def test_links_that_can_never_email_are_not_requested(tmp_path):
    calls = []
    def f(url, page_url, prev):
        calls.append(url)
        return _fake_doc(TEXTS)(url, page_url, prev)
    con = _con(tmp_path); _src_added(con, OCT31 - dt.timedelta(days=1))
    _run(con, OCT31, PAGE_HTML, fetch=f)
    assert calls == []                                                  # baseline scan: zero document requests
    _run(con, NOV1, PAGE2.replace("<tbody>\n", "<tbody>\n" + OLD_ROW, 1), fetch=f)
    assert calls == [NEW_URL]                                           # only the new upcoming packet


class _Stream:
    def __init__(self, status, chunks, headers=None):
        self.status_code, self.chunks, self.headers, self.closed = status, chunks, headers or {}, False
    def iter_content(self, n):
        assert n == aw.DOC_CHUNK
        yield from self.chunks
    @property
    def content(self):
        raise AssertionError("must stream, never read .content")
    def close(self):
        self.closed = True


def test_fetch_doc_streams_to_disk_keeps_conditional_get_and_cleans_up(monkeypatch, tmp_path):
    monkeypatch.setattr(aw, "CACHE", tmp_path / "cache")
    monkeypatch.setattr(aw, "extract_pdf_text", lambda p: p.read_bytes().decode()[4:])
    seen = []
    resp = {}
    def get(url, timeout=None, headers=None, stream=False, **kw):
        seen.append((dict(headers or {}), stream))
        return resp["r"]
    monkeypatch.setattr(acs.CITY_S, "get", get)
    resp["r"] = r = _Stream(200, [b"%PDF", b"abc", b"def"], {"ETag": '"e1"', "Last-Modified": "Mon"})
    text, row, how = acs.fetch_doc(NEW_URL, CH_URL, None)
    assert (text, how, row["length"], row["etag"]) == ("abcdef", "fetched", 10, '"e1"') and r.closed
    assert seen[-1] == ({}, True) and list((tmp_path / "cache").iterdir()) == []
    resp["r"] = r = _Stream(304, [])
    text, row, how = acs.fetch_doc(NEW_URL, CH_URL, {"etag": '"e1"', "last_modified": "Mon", "text": "abcdef"})
    assert (text, row, how) == ("abcdef", None, "304") and r.closed
    assert seen[-1][0] == {"If-None-Match": '"e1"', "If-Modified-Since": "Mon"}
    resp["r"] = _Stream(200, [b"<html>Access denied</html>"])
    import pytest
    with pytest.raises(acs.CityFetchError, match="not a PDF"):
        acs.fetch_doc(NEW_URL, CH_URL, None)
    assert list((tmp_path / "cache").iterdir()) == []
