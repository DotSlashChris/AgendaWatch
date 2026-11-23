"""Nov 3 2026 owner order: (1) a passing short-term-rental demand/revenue mention inside a lodging /
transient-room / sales tax revenue report is not a rental agenda hit (Park City Nov 5 CC packet TRT
report); St. George's Nov 5 "Discussion regarding owner-occupied Short-Term Rentals" stays a hit.
(2) /internal/admin-watch-add switches on listed cities for an ADMIN only, preserving everything else."""
import sqlite3

import pytest
from fastapi.testclient import TestClient

import agenda_watch as aw
from test_admin import ADMIN, OWNER, USER, web  # noqa: F401  (fixture)
from test_web_watch import seed, webapp

KW = {"keywords": ["short-term rental", "STR", "rental license", "business license", "zoning",
                   "conditional use", "transient tax", "occupancy"]}
M = aw.build_keyword_matchers(KW)


def terms(text, doc="11.5.26 CC Packet.pdf"):
    return sorted({h["matched"] for h in aw.find_hits(text, M, doc_name=doc)})


# Real lines (Park City Nov 5 2026 City Council packet, TRT / sales tax report)
PC_TRT = (
    "Transient Room Tax (TRT) Distribution Summary:\nMonthly (August):\n"
    "Revenue: $191,649 — down $21,397 (-10%) vs. August 2025; down $20,304 (-9.6%)\nvs. the budget.\n"
    "Sales Tax Analysis\nAugust's sales tax distribution was notably weaker than July.\n"
    "Transient Room Tax remains the softer revenue indicator early in FY27. August TRT\n"
    "declined 10% compared to last year and 9.6% compared to budget, bringing fiscal\n\nPage 81 of 129\n\n"
    "year-to-date collections 4.5% below last year and 4.2% below budget. The weakness\n"
    "was concentrated in non-hotel lodging: August hotel TRT increased approximately 9%\n"
    "from last year, while non-hotel collections declined approximately 36%. This distinction\n"
    "also shows up in current lodging indicators. The Chamber's August research report\n"
    "shows improving hotel demand during the month, while short-term rental demand\n"
    "declined.\nThe August lodging comparisons also warrant some caution.\n")

SG_ITEM = "Work Session\n1. Call to order.\n2. Discussion regarding owner-occupied Short-Term Rentals.\n3. Adjourn.\n"
SG_PERMITS = "a. Limited Numbers example: Sandy -2 STR Permits per every 100 homes in each neighborhood\n"


def test_park_city_trt_report_demand_line_is_dropped():
    assert terms(PC_TRT) == []


def test_st_george_owner_occupied_str_item_is_kept():
    assert "short-term rental" in terms(SG_ITEM, doc="PMN notice text")
    assert "STR" in terms(SG_PERMITS, doc="Work Session Packet.pdf")


def test_st_george_item_is_kept_even_next_to_a_tax_revenue_report():
    assert "short-term rental" in terms(PC_TRT + "\n" + SG_ITEM)


def test_rule_sentence_about_str_revenue_in_tax_report_is_kept():
    t = PC_TRT + ("\nThe Council will consider an ordinance amending Chapter 4 so that short-term rental "
                  "revenue is licensed and audited.\n")
    assert "short-term rental" in terms(t)


def test_market_mention_without_a_tax_revenue_report_is_kept():
    # narrow: only inside a lodging / transient-room / sales tax revenue report
    assert "short-term rental" in terms("Housing study\nShort-term rental demand has grown in the Old Town neighborhood.\n")


# ---------------------------------------------------------------- admin-watch-add
SECRET = "s3cret-test"
H = {"x-aw-internal-secret": SECRET}


def _cloud(monkeypatch):
    monkeypatch.setenv("AW_CLOUD", "1")
    monkeypatch.setenv("AW_INTERNAL_SECRET", SECRET)
    return TestClient(webapp.app)


def _rows(db, email):
    with sqlite3.connect(db) as c:
        return (c.execute("SELECT keywords, alerts_on FROM watches WHERE email=?", (email,)).fetchone(),
                sorted(c.execute("SELECT city_key, enabled, street, enabled_at FROM watch_cities WHERE email=?", (email,))))


def test_admin_watch_add_preserves_everything_else(web, monkeypatch):
    seed(web, email=ADMIN, keywords="zoning\nSTR", alerts_on=1,
         cities={"ch": (1, "7 Main St"), "slc": (1, ""), "sandy": (0, "9 Oak")})
    seed(web, email=OWNER, cities={"ch": (1, "100 Example St")})
    with sqlite3.connect(web) as c:   # seed() replaces the users row; restore the admin flag
        c.execute("UPDATE users SET is_admin=1 WHERE email=?", (ADMIN,))
    other_before = _rows(web, OWNER)
    sent = []
    monkeypatch.setattr(webapp, "send_email", lambda *a, **k: sent.append(a), raising=False)
    c = _cloud(monkeypatch)
    r = c.post("/internal/admin-watch-add", json={"email": ADMIN, "confirm": ADMIN, "add": ["parkcity", "stgeorge", "ch"]}, headers=H)
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["after"]["enabled"] == ["ch", "parkcity", "slc", "stgeorge"]
    (kw, on), cities = _rows(web, ADMIN)
    assert kw == "zoning\nSTR" and on == 1
    d = {k: (e, s, at) for k, e, s, at in cities}
    assert d["ch"][:2] == (1, "7 Main St") and d["ch"][2] is None   # already on: untouched
    assert d["sandy"][:2] == (0, "9 Oak") and d["slc"][:2] == (1, "")
    assert d["parkcity"][:2] == (1, "") and d["parkcity"][2] and d["stgeorge"][:2] == (1, "")
    assert _rows(web, OWNER) == other_before and sent == []
    g = c.get(f"/internal/admin-watch?email={ADMIN}", headers=H).json()
    assert g["enabled"] == ["ch", "parkcity", "slc", "stgeorge"]


def test_admin_watch_add_refuses_non_admin_bad_keys_and_no_secret(web, monkeypatch):
    seed(web, email=OWNER, cities={"ch": (1, "")})
    before = _rows(web, OWNER)
    c = _cloud(monkeypatch)
    assert c.post("/internal/admin-watch-add", json={"email": ADMIN, "confirm": ADMIN, "add": ["parkcity"]}).status_code == 403
    assert c.post("/internal/admin-watch-add", json={"email": OWNER, "confirm": OWNER, "add": ["parkcity"]}, headers=H).status_code == 403
    assert c.post("/internal/admin-watch-add", json={"email": ADMIN, "confirm": "x", "add": ["parkcity"]}, headers=H).status_code == 400
    assert c.post("/internal/admin-watch-add", json={"email": ADMIN, "confirm": ADMIN, "add": ["moab"]}, headers=H).status_code == 400
    assert c.get(f"/internal/admin-watch?email={OWNER}", headers=H).status_code == 403
    assert _rows(web, OWNER) == before


def test_admin_watch_add_is_404_on_the_box(web, monkeypatch):
    monkeypatch.delenv("AW_CLOUD", raising=False)
    assert TestClient(webapp.app).post("/internal/admin-watch-add", json={}).status_code == 404
