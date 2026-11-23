"""Alert email: agenda item title vs packet excerpt, using the real Oct 21 2026 Cottonwood Heights
Planning Commission notice (PMN 1115975; packet https://www.utah.gov/pmn/files/1502179.pdf and agenda
1502181.pdf as cached in the documents table). Fixtures are the stored pdftotext output."""
import datetime as dt
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

import agenda_watch  # noqa: F401  (aw_alerts uses its matcher)
import aw_alerts

F = Path(__file__).parent / "fixtures" / "ch_20261021"
KW = ["short-term rental", "STR", "rental license", "business license", "zoning", "conditional use",
      "transient tax", "occupancy"]
NOW = dt.datetime(2026, 10, 17, 12, 0, tzinfo=ZoneInfo("America/Denver"))
PACKET = "https://www.utah.gov/pmn/files/1502179.pdf"


def notice():
    return {"city": "Cottonwood Heights", "city_key": "ch",
            "notice_url": "https://www.utah.gov/pmn/sitemap/notice/1115975.html",
            "meeting_body": "Planning Commission", "notice_title": "October 21, 2026 Planning Commission",
            "meeting_datetime": "October 21, 2026 5:00 PM", "event_iso": "2026-10-21T17:00:00-06:00",
            "docs": [("PMN notice text", "https://www.utah.gov/pmn/sitemap/notice/1115975.html",
                      (F / "notice_1115975.txt").read_text()),
                     ("1502179.pdf", PACKET, (F / "1502179.txt").read_text()),
                     ("1502181.pdf", "https://www.utah.gov/pmn/files/1502181.pdf", (F / "1502181.txt").read_text())]}


@pytest.fixture
def hits():
    w = {"email": "x@example.invalid", "alerts_on": True, "keywords": KW, "cities": {"ch": {"enabled": True, "street": ""}}}
    return aw_alerts.match_user([notice()], w, NOW)


def test_agenda_items_parsed_from_the_notice():
    all_items = aw_alerts._agenda_for_notice(notice())
    items = {it["num"]: it for it in reversed(all_items)}          # first occurrence wins (work session)
    it = items["3.4"]
    assert it["cases"] == ["ZTA-26-004"] and it["session"] == "6:00 p.m. Business Session"
    assert aw_alerts.item_title(it) == (
        "3.4 Project ZTA-26-004 — The Planning Commission will hold a public hearing to review and make a "
        "possible recommendation to the City Council on a city-initiated Zoning Text Amendment to CHMC "
        "Chapter 19.75, Accessory Dwelling Units.")
    assert aw_alerts.item_title(items["2.0"]) == "2.0 CUP-25-005 -Old Mill Demolition"


def test_packet_definition_maps_to_zta_26_004_and_excerpt_is_the_sentence(hits):
    by = {h["excerpt_source"]: h for h in hits}
    h = by["the packet/attachment 1502179.pdf, page 78"]
    assert "ZTA-26-004" in h["agenda_item"] and "Accessory Dwelling Units" in h["agenda_item"]
    assert h["excerpt_sentence"] == (
        "“Short-term rental” means the rental, letting of rooms or sub-leasing/renting of any structure, dwelling "
        "or portion thereof for occupancy, dwelling, lodging or sleeping purposes for fewer than 30 consecutive "
        "days in duration.")
    assert h["link"] == PACKET
    h2 = by["the packet/attachment 1502179.pdf, page 80"]
    assert "ZTA-26-004" in h2["agenda_item"] and h2["excerpt_sentence"] == "ADUs shall not be used as short-term rentals."


def test_email_leads_with_agenda_item_and_never_calls_packet_text_the_item(hits):
    _, body = aw_alerts.build_user_email(hits, "https://agendawatch.app")
    assert body.count("Item: 3.4 Project ZTA-26-004 — ") == 1          # one entry
    assert "Agenda Watch found 1 matching upcoming agenda item in public agendas" in body
    # the definition excerpt (page 78) is dropped; only the page-80 rule remains, terms follow it
    assert body.count("Excerpt (") == 1
    assert 'Excerpt (from the packet/attachment 1502179.pdf, page 80): "ADUs shall not be used as short-term rentals."' in body
    assert "means the rental" not in body and "Matched: short-term rental\n" in body
    assert "Item: “Short-term rental” means" not in body and "Item: purpose of" not in body
    assert body.index("Cottonwood Heights Planning Commission") < body.index("Item:") \
        < body.index("Excerpt") < body.index("Source: " + PACKET)
    assert "October 21, 2026 at 5:00 PM" in body


def test_unmapped_hit_says_not_identified_but_keeps_excerpt():
    n = {"city": "Sandy", "city_key": "sandy", "notice_url": "https://x/n", "meeting_body": "City Council",
         "notice_title": "Council", "meeting_datetime": "October 21, 2026 7:00 PM",
         "event_iso": "2026-10-21T19:00:00-06:00",
         "docs": [("PMN notice text", "https://x/n", "Regular meeting. See packet."),
                  ("packet.pdf", "https://x/p.pdf", "Staff report\n\fBackground. Nightly short-term rental licenses rose in 2025. Other text.")]}
    w = {"email": "x", "alerts_on": True, "keywords": KW, "cities": {"sandy": {"enabled": True, "street": ""}}}
    [h] = aw_alerts.match_user([n], w, NOW)
    assert h["agenda_item"] is None
    assert h["excerpt_sentence"] == "Nightly short-term rental licenses rose in 2025."
    assert h["excerpt_source"] == "the packet/attachment packet.pdf, page 2"
    _, body = aw_alerts.build_user_email([h], "https://agendawatch.app")
    assert "not identified" not in body and "Item: Matching item" in body and 'Excerpt (from the packet/attachment packet.pdf, page 2): "Nightly' in body


def test_nearest_case_number_not_on_agenda_is_not_identified():
    items = aw_alerts.agenda_items("3.1 Project CUP-26-020\nChildcare at 2104 East Fort Union Blvd.\n")
    text = "Memo CUP-26-020 ...\n\fMinutes: CUP-26-016 discussed. A short-term rental was mentioned."
    assert aw_alerts._locate_item(items, text, text.index("short-term")) is None
    text2 = "Memo CUP-26-020 ...\n\fA short-term rental condition."
    assert aw_alerts._locate_item(items, text2, text2.index("short-term"))["cases"] == ["CUP-26-020"]


def test_definition_detection():
    d = aw_alerts.is_definition
    assert d("“Short-term rental” means the rental, letting of rooms for fewer than 30 days.")
    assert d('"Owner occupancy" shall mean residence for 200 days.')
    assert d("A short-term rental is defined as any rental under 30 days.")
    assert not d("ADUs shall not be used as short-term rentals.")
    assert not d("The commission will discuss short-term rental licensing.")
    text = "19.75.020 Definitions\nAccessory unit: a second unit. Short-term rental: under 30 days.\n19.75.030 Standards\nNo short-term rentals."
    assert d("Short-term rental: under 30 days.", text, text.index("Short-term rental:"))
    assert not d("No short-term rentals.", text, text.index("No short-term"))


def test_all_definition_item_kept_without_excerpt_and_plural():
    base = {"city": "Sandy", "meeting_datetime": "October 21, 2026 7:00 PM", "notice_url": "https://x/n",
            "link": "https://x/p.pdf", "agenda_item": "4.1 Project ZTA-26-009 — Text amendment.", "meeting_body": "Council"}
    defs = [dict(base, matched_terms=["short-term rental"], excerpt_sentence="“STR” means a short-term rental.",
                 excerpt_source="the packet/attachment p.pdf, page 3", is_definition=True)]
    other = dict(base, agenda_item="5.2 Business license fees — Fee update.", matched_terms=["business license"],
                 excerpt_sentence="Nightly rental business license fees rise.", is_definition=False, notice_url="https://x/n2")
    _, body = aw_alerts.build_user_email(defs + [other], "https://agendawatch.app")
    assert "found 2 matching upcoming agenda items" in body
    entry = body.split("1. ")[1].split("2. ")[0]
    assert "Item: 4.1 Project ZTA-26-009" in entry and "Excerpt" not in entry
    assert "Matched: short-term rental" in entry and "Source: https://x/p.pdf" in entry


def test_provo_tab_numbered_agenda_items_parsed():
    t = ("Action Agenda\n\n2\tA resolution consenting to board and commission appointments. (26-041) \n\n"
         "5\tAn ordinance prohibiting use of property officially declared unfit or unsafe. (26-069) \n\n"
         "7\tAn ordinance amending Provo City Code regarding permitted uses in the ITOD Zone. (PLOTA20250243) \n\nAdjournment\n")
    items = aw_alerts.agenda_items(t)
    assert [(i["num"], i["cases"]) for i in items] == [("2", ["26-041"]), ("5", ["26-069"]), ("7", ["PLOTA20250243"])]
    assert aw_alerts.item_title(items[1]) == "5 An ordinance prohibiting use of property officially declared unfit or unsafe. (26-069)"
