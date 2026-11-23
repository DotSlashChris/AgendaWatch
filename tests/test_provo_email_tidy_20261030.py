"""Oct 30 2026 email tidy: title stops at the agenda line (no next-section text), at most two excerpts
(agenda line + staff recommendation first), no repeated city name. Matcher and hit keys unchanged."""
import datetime as dt
import gzip
import json
import os

import aw_alerts

FIX = os.path.join(os.path.dirname(__file__), "fixtures", "pmn_provo_20261103.json.gz")
KW = ["short-term rental", "STR", "rental license", "business license", "zoning", "conditional use",
      "transient tax", "occupancy"]
BASE = "https://agendawatch.app"
TITLE = "8 An ordinance text amendment to Chapter 6.33 regarding short-term rental regulations. (26-082)"


def _provo_hits():
    n = json.load(gzip.open(FIX, "rt"))[0]
    n["docs"] = [tuple(d) for d in n["docs"]]
    ev = dt.datetime.fromisoformat(n["event_iso"])
    n["meeting_datetime"] = ev.strftime("%B %d, %Y %I:%M %p").replace(" 0", " ")
    w = {"email": "t@example.com", "alerts_on": 1, "keywords": KW, "cities": {"provo": {"enabled": True, "street": ""}}}
    return aw_alerts.match_user([n], w, ev - dt.timedelta(days=1))


def test_provo_nov3_hits_unchanged_all_recorded():
    hs = _provo_hits()
    assert len(hs) == 14                                             # the 14 hits sent Oct 30, all still recorded
    assert len({h["fingerprint"] for h in hs}) == 14
    assert {h["meeting_date"][:10] for h in hs} == {"2026-11-03"}


def test_provo_nov3_email_title_excerpts_and_meeting_line():
    hs = _provo_hits()
    assert {h["agenda_item"] for h in hs if h["agenda_item"]} == {TITLE}
    [(kind, subject, body, _)] = aw_alerts.build_user_emails(hs, BASE)
    assert kind == "upcoming" and subject == "Agenda Watch: Provo Municipal Council — November 3, 2026"
    assert f"\n   Item: {TITLE}\n" in body
    assert "Closed Meeting" not in body and "motion to close" not in body
    assert "1. Provo Municipal Council\n" in body and "   November 3, 2026 at 10:30 AM\n" in body
    assert "Provo Provo" not in body
    quoted = [l for l in body.splitlines() if l.startswith('     "')]
    assert len(quoted) == 2
    assert quoted[0].startswith('     "8 An ordinance text amendment to Chapter 6.33 regarding short-term rental')
    assert "(the PMN notice (agenda text))" in quoted[0]
    assert quoted[1].startswith('     "ADMINISTRATIVE RECOMMENDATION: Approve the ordinance')
    for gone in ("(1) It is unlawful", "(4) A short-term rental business license", "ten percent (10%)",
                 "6.33.050", "SHORT-TERM RENTAL DWELLINGS Sections", "(i) Leasing", "FISCAL IMPACT"):
        assert gone not in body
    assert "Source: https://www.utah.gov/pmn/files/1508539.pdf" in body
    assert "PMN notice: https://www.utah.gov/pmn/sitemap/notice/1119169.html" in body


def test_item_title_drops_next_section_and_keeps_label_descriptions():
    t = aw_alerts.item_title
    assert t({"num": "8", "heading": "An ordinance text amendment. (26-082)", "desc": "Closed Meeting The Council."}) \
        == "8 An ordinance text amendment. (26-082)"
    assert t({"num": "3", "heading": "Fee update", "desc": "Adjournment."}) == "3 Fee update"
    assert t({"num": "3", "heading": "Fee update", "desc": "The Council will consider a motion to close the meeting."}) \
        == "3 Fee update"
    assert t({"num": "3.4", "heading": "Project ZTA-26-004", "desc": "Accessory Dwelling Units text amendment."}) \
        == "3.4 Project ZTA-26-004 — Accessory Dwelling Units text amendment."


def test_meeting_name_dedups_city_case_insensitively():
    m = aw_alerts.meeting_name
    assert m("Provo", "Provo Municipal Council") == "Provo Municipal Council"
    assert m("Provo", "PROVO MUNICIPAL COUNCIL") == "PROVO MUNICIPAL COUNCIL"
    assert m("Orem", "City Council") == "Orem City Council"
    assert m("Lehi", "Lehighton Board") == "Lehi Lehighton Board"           # whole word only
    assert m("Sandy", "") == "Sandy" and m("Salt Lake City", "salt lake city Council") == "salt lake city Council"


def _h(**kw):
    base = {"city": "Provo", "city_key": "provo", "meeting_date": "2026-10-20", "meeting_body": "Provo Municipal Council",
            "meeting_datetime": "October 20, 2026 5:30 PM", "notice_url": "https://x/n1", "link": "https://x/p.pdf",
            "matched_terms": ["short-term rental"], "is_definition": False, "agenda_item": None,
            "excerpt_source": "the packet/attachment p.pdf, page 3", "item_text": "t"}
    return dict(base, **kw)


def test_minutes_email_subject_and_meeting_line_do_not_repeat_city():
    hs = [_h(kind="minutes", minutes_of="September 8, 2026", past_minutes=True,
             excerpt_sentence=f"Short-term rental item {i} discussed.", item_text=f"t{i}") for i in range(4)]
    subject, body = aw_alerts.build_minutes_email(hs, BASE)
    assert "Provo Provo" not in subject and "Provo Provo" not in body
    assert subject.startswith("Agenda Watch: Provo Municipal Council discussed short-term rentals")
    assert "\n   Meeting: Provo Municipal Council — September 8, 2026 (minutes)\n" in body
    assert sum(1 for l in body.splitlines() if l.startswith('     "')) == 2     # excerpt cap applies here too


def test_excerpt_cap_prefers_recommendation_and_drops_ordinance_body():
    hs = [_h(excerpt_sentence="(1) It is unlawful to operate a short-term rental.", item_text="(1) It is unlawful"),
          _h(excerpt_sentence="6.33.030 License application for a short-term rental.", item_text="6.33.030 x"),
          _h(excerpt_sentence="Staff recommends approval of the short-term rental amendment.", item_text="s"),
          _h(excerpt_sentence="Neighbors raised short-term rental parking concerns.", item_text="n"),
          _h(excerpt_sentence="(b) A short-term rental cap applies.", item_text="(b) x")]
    _, body = aw_alerts.build_user_email(hs, BASE)
    quoted = [l for l in body.splitlines() if l.startswith('     "')]
    assert len(quoted) == 2
    assert quoted[0].startswith('     "Staff recommends approval') and "parking concerns" in quoted[1]
    assert "It is unlawful" not in body and "6.33.030" not in body
    # with no agenda line and no recommendation: the best two matched sentences
    plain = [_h(excerpt_sentence=f"Short-term rental sentence {i}.", item_text=f"p{i}") for i in range(5)]
    _, body2 = aw_alerts.build_user_email(plain, BASE)
    quoted2 = [l for l in body2.splitlines() if l.startswith('     "')]
    assert len(quoted2) == 2 and "sentence 0." in quoted2[0] and "sentence 1." in quoted2[1]


def test_recommendation_patterns():
    r = aw_alerts.RECOMMEND_RE
    for s in ("Staff Recommendation: approve.", "Recommendation: deny.", "Staff recommends approval.",
              "Planning Commission recommends approval.", "ADMINISTRATIVE RECOMMENDATION: Approve the ordinance"):
        assert r.search(s), s
    assert not r.search("The ordinance clarifies short-term rentals.")
