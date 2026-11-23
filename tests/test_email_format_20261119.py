"""Nov 19 2026: professional hit-email wording. Matching, who is emailed, and source-link
dedupe are unchanged. The Dec 2 Cottonwood Heights ZTA-26-006 notice is the fixture."""
import datetime as dt
from pathlib import Path
from zoneinfo import ZoneInfo

import aw_alerts

F = Path(__file__).parent / "fixtures" / "ch_pc_20261202_zta"
BASE = "https://agendawatch.app"
NOTICE = "https://www.utah.gov/pmn/sitemap/notice/1124151.html"
PACKET = "https://www.utah.gov/pmn/files/1518383.pdf"
NOW = dt.datetime(2026, 11, 18, 8, 23, tzinfo=ZoneInfo("America/Denver"))
KW = ["short-term rental", "STR", "rental license", "business license", "zoning", "conditional use",
      "transient tax", "occupancy"]


def zta_notice():
    return {
        "city": "Cottonwood Heights", "city_key": "ch", "notice_url": NOTICE,
        "meeting_body": "Planning Commission",
        "notice_title": "Notice of a Public Hearing for Three Zoning Text Amendments - December 2, 2026",
        "meeting_datetime": "December 2, 2026 05:00 PM",
        "event_iso": "2026-12-02T17:00:00-07:00",
        "docs": [
            ("PMN notice text", NOTICE, (F / "notice_1124151.txt").read_text()),
            ("1518383.pdf", PACKET, (F / "1518383.txt").read_text()),
        ],
    }


def zta_hits():
    w = {"email": "x@example.invalid", "alerts_on": True, "keywords": KW,
         "cities": {"ch": {"enabled": True, "street": "100 Example St"}}}
    return aw_alerts.match_user([zta_notice()], w, NOW)


def test_zta_subject_and_body_are_scannable_with_real_bullets():
    hits = zta_hits()
    assert hits and hits[0]["agenda_item"] is None          # matching is unchanged
    subject, body = aw_alerts.build_user_email(hits, BASE)
    assert subject == "Agenda Watch: Cottonwood Heights Planning Commission — December 2, 2026"
    assert "not identified" not in body
    assert "Item: Matching item" in body
    assert "Cottonwood Heights Planning Commission" in body
    assert "December 2, 2026 at 5:00 PM" in body
    assert "Matched: short-term rental" in body
    # jammed mid-line bullets are split onto their own lines
    assert "  • CHMC Chapter 19.89 regarding Short Term Rentals (File: ZTA-26-006)," in body
    assert "  • CHMC Chapter 19.75 regarding Accessory Dwelling Units (File: ZTA-26-007), and" in body
    assert "  • CHMC Chapter 19.76 regarding Fencing (File: ZTA-26-008)." in body
    jammed = "modify: • CHMC Chapter 19.89"
    assert jammed not in body
    assert body.count(PACKET) == 1
    assert body.count(NOTICE) == 1
    assert f"   Source: {PACKET}" in body
    assert f"   PMN notice: {NOTICE}" in body
    # source line is not the notice
    sources = [l for l in body.splitlines() if l.startswith("   Source: ")]
    assert sources == [f"   Source: {PACKET}"]
    assert "Agenda Watch flags public agenda text and links to the source. It is not legal advice." in body
    assert "Change your watch or turn alerts off: https://agendawatch.app/watch" in body


def test_missing_or_not_identified_title_becomes_matching_item():
    base = {"city": "Sandy", "meeting_datetime": "October 21, 2026 7:00 PM",
            "notice_url": "https://www.utah.gov/pmn/sitemap/notice/9.html",
            "link": "https://www.utah.gov/pmn/files/1.pdf", "meeting_body": "City Council",
            "matched_terms": ["short-term rental"], "is_definition": False,
            "excerpt_sentence": "Nightly short-term rental licenses rose in 2025.",
            "excerpt_source": "the packet/attachment packet.pdf, page 2", "item_text": "t"}
    for title in (None, "", "not identified", "Not Identified"):
        _, body = aw_alerts.build_user_email([dict(base, agenda_item=title)], BASE)
        assert "Item: Matching item" in body
        assert "not identified" not in body.lower() or title is None
        assert "Nightly short-term rental licenses rose in 2025." in body


def test_identified_title_is_kept():
    h = {"city": "Provo", "meeting_datetime": "November 3, 2026 10:30 AM",
         "notice_url": "https://www.utah.gov/pmn/sitemap/notice/1.html",
         "link": "https://www.utah.gov/pmn/files/2.pdf", "meeting_body": "Provo Municipal Council",
         "agenda_item": "8 An ordinance text amendment to Chapter 6.33.",
         "matched_terms": ["short-term rental"], "is_definition": False,
         "excerpt_sentence": "Staff recommends approval of the short-term rental amendment.",
         "excerpt_source": "the packet/attachment 2.pdf, page 1", "item_text": "t"}
    _, body = aw_alerts.build_user_email([h], BASE)
    assert "Item: 8 An ordinance text amendment to Chapter 6.33." in body
    assert "Matching item" not in body


def test_html_has_list_items_links_disclaimer_and_no_duplicate_urls():
    html = aw_alerts.render_user_email_html(zta_hits(), BASE)
    assert html.count(f'href="{PACKET}"') == 1
    assert html.count(f'href="{NOTICE}"') == 1
    assert "<ul" in html and "<li" in html
    assert "CHMC Chapter 19.89 regarding Short Term Rentals (File: ZTA-26-006)" in html
    assert "Matching item" in html
    assert "not identified" not in html
    assert "It is not legal advice." in html
    assert f"{BASE}/watch" in html
    assert "Manage your watch" in html
    assert "style=" in html and "<table" in html
    assert "script" not in html.lower().replace("<title>agenda watch</title>", "")
