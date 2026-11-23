"""Oct 28 2026 fixes: (1) the alert title is the agenda line the match belongs to, not the nearest
preceding heading in the PDF; (2) a 'DRAFT MINUTES FOR <date>' heading counts as minutes."""
import datetime as dt
import gzip
import json
import os

import aw_alerts

FIX = os.path.join(os.path.dirname(__file__), "fixtures", "pmn_titles_20261028.json.gz")
KW = ["short-term rental", "STR", "rental license", "business license", "zoning", "conditional use",
      "transient tax", "occupancy"]
BASE = "https://agendawatch.app"


def _notices():
    out = {}
    for n in json.load(gzip.open(FIX, "rt")):
        n["docs"] = [tuple(d) for d in n["docs"]]
        ev = dt.datetime.fromisoformat(n["event_iso"])
        n["meeting_datetime"] = ev.strftime("%B %d, %Y %I:%M %p").replace(" 0", " ")
        out[n["notice_url"].rsplit("/", 1)[-1].split(".")[0]] = n
    return out


def _hits(n):
    """Replay: the stored meeting treated as upcoming (now = the day before it)."""
    w = {"email": "t@example.com", "alerts_on": 1, "keywords": KW,
         "cities": {n["city_key"]: {"enabled": True, "street": ""}}}
    now = dt.datetime.fromisoformat(n["event_iso"]) - dt.timedelta(days=1)
    return aw_alerts.match_user([n], w, now)


def test_vineyard_oct13_staff_report_maps_to_4_1_not_7_4():
    hs = [h for h in _hits(_notices()["1114503"]) if h["link"].endswith("1500189.pdf")]
    assert hs, "the Short-term Rental Report hits"
    for h in hs:
        assert h["kind"] == "upcoming"
        assert h["agenda_item"] == "4.1 Discuss Short-Term Rentals"
        assert "NUCAC" not in (h["agenda_item"] or "")
    subj, body = aw_alerts.build_user_email(hs, BASE)
    assert "   Item: 4.1 Discuss Short-Term Rentals\n" in body


def test_orem_oct13_uses_sub_item_1_3_not_section_heading():
    hs = _hits(_notices()["1111885"])
    h = next(h for h in hs if "Short Term Rental Continued" in h["item_text"])
    assert h["agenda_item"] == "1.3 DISCUSSION - Short Term Rental Continued"
    assert "WORK SESSION ITEMS" not in h["agenda_item"]
    _, body = aw_alerts.build_user_email([h], BASE)
    assert "Item: 1.3 DISCUSSION - Short Term Rental Continued\n" in body


def test_split_number_line_items_parsed():
    t = "1. WORK SESSION ITEMS\nIntro text.\n1.1\n\nPRESENTATION - Survey (20 min)\nPresenter: A. B\n\n" \
        "1.3\n\nDISCUSSION - Short Term Rental Continued (90 min)\nPresenter: D. Jacob Summers, Deputy City Attorney\n"
    items = aw_alerts.agenda_items(t)
    it = next(i for i in items if i["num"] == "1.3")
    assert aw_alerts.item_title(it) == "1.3 DISCUSSION - Short Term Rental Continued"
    assert aw_alerts._locate_item(items, t, t.index("Short Term"))["num"] == "1.3"
    # bare line numbers (minutes pages) are never agenda items
    assert not aw_alerts.agenda_items("1\n2\n3\n\nPresent:\nMayor\n")


def test_orem_sep2_draft_minutes_are_past_minutes():
    hs = _hits(_notices()["1102939"])
    h = next(h for h in hs if h["link"].endswith("1481597.pdf"))
    assert h["kind"] == "past_minutes" and h["minutes_of"] == "July 15, 2026"
    assert h["minutes_key"] == "minutes|orem|2026-07-15|planning commission"
    assert aw_alerts.is_past(h)


def test_orem_sep2_replay_sends_discussed_email_not_upcoming():
    hs = _hits(_notices()["1102939"])
    emails = aw_alerts.build_user_emails(hs, BASE)
    kinds = [k for k, *_ in emails]
    assert "past_minutes" in kinds
    k, subj, body, groups = next(e for e in emails if e[0] == "past_minutes")
    assert "discussed" in subj and "(July 15, 2026 minutes)" in subj
    assert "Already discussed at the July 15, 2026 meeting" in body
    assert "upcoming" not in body.lower() and "Agenda item:" not in body
    # no draft-minutes text leaks into an upcoming email
    for e in emails:
        if e[0] == "upcoming":
            assert "1481597.pdf" not in e[2]


def test_draft_minutes_heading_variants_and_no_prose():
    for head in ("DRAFT MINUTES FOR JULY 15, 2026", "DRAFT MINUTES OF THE PLANNING COMMISSION JULY 15, 2026",
                 "Draft Minutes – July 15, 2026", "Draft Minutes: July 15, 2026"):
        t = f"{head}\nMr. Smith said short-term rentals need rules.\n"
        assert aw_alerts.minutes_context(t, t.index("short-term"))[0] == "July 15, 2026", head
    prose = "Staff will post the draft minutes for July 15, 2026 soon. Short-term rentals were discussed.\n"
    assert aw_alerts.minutes_context(prose, prose.index("Short-term")) is None
    prose2 = "The minutes of the board are attached.\nShort-term rentals rules.\n"
    assert aw_alerts.minutes_context(prose2, prose2.index("Short-term")) is None


def test_topic_item_requires_unique_page_title_match():
    items = aw_alerts.agenda_items("4.1  Discuss Short-Term Rentals (Ed Kennedy - 10 Minutes)\n"
                                   "7.4  Membership in the North Utah County Aquifer Council\n")
    t = "AGENDA\n4.1  Discuss Short-Term Rentals (Ed Kennedy - 10 Minutes)\n" \
        "7.4  Membership in the North Utah County Aquifer Council\n\fShort-term Rental Report\nSTR rules.\n" \
        "\fBudget Memo\nA short-term rental line.\n"
    assert aw_alerts._locate_item(items, t, t.index("STR rules")) is None          # no page-crossing
    assert aw_alerts._topic_item(items, t, t.index("STR rules"))["num"] == "4.1"
    assert aw_alerts._topic_item(items, t, t.index("A short-term")) is None         # other section title
