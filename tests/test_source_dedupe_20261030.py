"""Oct 30 2026: Source lines list each URL once and never repeat the PMN notice link."""
import datetime as dt
import gzip
import json
import os

import aw_alerts

FIX = os.path.join(os.path.dirname(__file__), "fixtures", "pmn_provo_20261103.json.gz")
KW = ["short-term rental", "STR", "rental license", "business license", "zoning", "conditional use",
      "transient tax", "occupancy"]
BASE = "https://agendawatch.app"
NOTICE = "https://www.utah.gov/pmn/sitemap/notice/1119169.html"


def _lines(body, prefix):
    return [l for l in body.splitlines() if l.startswith(prefix)]


def test_provo_nov3_one_source_plus_notice():
    n = json.load(gzip.open(FIX, "rt"))[0]
    n["docs"] = [tuple(d) for d in n["docs"]]
    ev = dt.datetime.fromisoformat(n["event_iso"])
    n["meeting_datetime"] = ev.strftime("%B %d, %Y %I:%M %p").replace(" 0", " ")
    w = {"email": "t@example.com", "alerts_on": 1, "keywords": KW, "cities": {"provo": {"enabled": True, "street": ""}}}
    hs = aw_alerts.match_user([n], w, ev - dt.timedelta(days=1))
    assert len(hs) == 14
    [(_, _, body, _)] = aw_alerts.build_user_emails(hs, BASE)
    assert _lines(body, "   Source: ") == ["   Source: https://www.utah.gov/pmn/files/1508539.pdf"]
    assert _lines(body, "   PMN notice: ") == ["   PMN notice: " + NOTICE]
    assert body.index("Source: https://www.utah.gov/pmn/files/1508539.pdf") < body.index("PMN notice:")


def test_url_key_variants():
    k = aw_alerts.url_key
    base = k("https://www.utah.gov/pmn/files/1.pdf")
    for v in ("http://www.utah.gov/pmn/files/1.pdf", "HTTPS://WWW.UTAH.GOV/pmn/files/1.pdf",
              "https://www.utah.gov/pmn/files/1.pdf/", "https://www.utah.gov/pmn/files/1.pdf#page=3",
              "  https://www.utah.gov/pmn/files/1.pdf \n", "https://www.utah.gov:443/pmn/files/1.pdf"):
        assert k(v) == base, v
    assert k("https://www.utah.gov/pmn/files/1.pdf?v=2") != base          # query string is significant
    assert k("https://www.utah.gov/pmn/files/2.pdf") != base


def test_source_links_dedupe_and_order():
    links = ["https://x.gov/b.pdf", "http://X.gov/a.pdf/", "https://x.gov/b.pdf#p2", "https://x.gov/n.html",
             "https://x.gov/a.pdf", "https://x.gov/c.pdf?id=1", "https://x.gov/c.pdf?id=2", " https://x.gov/b.pdf "]
    assert aw_alerts.source_links(links, "http://x.gov/n.html/") == [
        "https://x.gov/b.pdf", "http://X.gov/a.pdf/", "https://x.gov/c.pdf?id=1", "https://x.gov/c.pdf?id=2"]
    assert aw_alerts.source_links(["https://x.gov/n.html#top"], "https://x.gov/n.html") == []


def _h(**kw):
    base = {"city": "Sandy", "city_key": "sandy", "meeting_date": "2026-11-05", "meeting_body": "City Council",
            "meeting_datetime": "November 5, 2026 7:00 PM", "notice_url": "https://www.utah.gov/pmn/sitemap/notice/9.html",
            "link": "https://www.utah.gov/pmn/sitemap/notice/9.html", "matched_terms": ["short-term rental"],
            "is_definition": False, "agenda_item": None, "excerpt_sentence": "A short-term rental item.",
            "excerpt_source": "the PMN notice (agenda text)", "item_text": "t"}
    return dict(base, **kw)


def test_notice_only_item_shows_only_the_notice_line():
    hs = [_h(), _h(link="http://WWW.utah.gov/pmn/sitemap/notice/9.html/", excerpt_sentence="Another short-term rental.",
                   item_text="t2")]
    _, body = aw_alerts.build_user_email(hs, BASE)
    assert _lines(body, "   Source: ") == []
    assert _lines(body, "   PMN notice: ") == ["   PMN notice: https://www.utah.gov/pmn/sitemap/notice/9.html"]


def test_minutes_shape_dedupes_sources():
    m = dict(kind="minutes", minutes_of="October 1, 2026", past_minutes=True,
             excerpt_source="the packet/attachment p.pdf, page 4")
    hs = [_h(link="https://www.utah.gov/pmn/files/5.pdf", item_text="a", **m),
          _h(link="http://www.utah.gov/pmn/files/5.pdf#page=4", item_text="b", excerpt_sentence="Short-term rental b.", **m),
          _h(link="https://www.utah.gov/pmn/sitemap/notice/9.html", item_text="c", excerpt_sentence="Short-term rental c.", **m)]
    _, body = aw_alerts.build_minutes_email(hs, BASE)
    assert _lines(body, "   Source: ") == ["   Source: https://www.utah.gov/pmn/files/5.pdf"]
    assert _lines(body, "   PMN notice: ") == ["   PMN notice: https://www.utah.gov/pmn/sitemap/notice/9.html"]
