"""Matcher rules — exercises the real build_matchers / find_hits / overlay_cfg_from_watch."""
import agenda_watch as aw
from conftest import CH, SLC


def hits_for(text, cfg, city=None, doc_name="Agenda.pdf"):
    return aw.find_hits(text, aw.build_matchers(cfg, city=city), doc_name=doc_name)


def items_matched_by(hits, term):
    return [h["item_text"] for h in hits if h["matched"] == term]


# 1 -----------------------------------------------------------------------------------
def test_short_term_rental_agenda_line_matches(cfg):
    text = (
        "CITY COUNCIL BUSINESS MEETING AGENDA\n"
        "\n"
        "1. Call to Order\n"
        "2. Discussion of short-term rental regulations\n"
        "3. Adjourn\n"
    )
    hits = hits_for(text, cfg, city=CH)
    assert items_matched_by(hits, "short-term rental") == ["2. Discussion of short-term rental regulations"]
    h = next(h for h in hits if h["matched"] == "short-term rental")
    assert h["strength"] == "strong" and h["match_type"] == "keyword"


# 2 -----------------------------------------------------------------------------------
def test_zoning_alone_does_not_match(cfg):
    text = (
        "PLANNING COMMISSION AGENDA\n"
        "\n"
        "1. Call to Order\n"
        "2. Zoning map amendment for parcel 22-27-151-001 from R-1-8 to R-2-8\n"
        "3. Adjourn\n"
    )
    assert hits_for(text, cfg, city=CH) == []


def test_conditional_use_alone_does_not_match(cfg):
    text = (
        "PLANNING COMMISSION AGENDA\n"
        "\n"
        "1. Call to Order\n"
        "2. Conditional use permit for a wireless telecommunication facility\n"
        "3. Adjourn\n"
    )
    assert hits_for(text, cfg, city=CH) == []


def test_weak_terms_together_without_rental_term_do_not_match(cfg):
    text = (
        "1. Zoning text amendment: conditional use standards, business license fees and occupancy limits\n"
    )
    assert hits_for(text, cfg, city=CH) == []


# 3 -----------------------------------------------------------------------------------
def test_zoning_matches_when_rental_term_in_same_item(cfg):
    text = (
        "CITY COUNCIL WORK SESSION AGENDA\n"
        "\n"
        "1. Call to Order\n"
        "2. Zoning ordinance amendment regarding short-term rental regulations in residential zones\n"
        "3. Adjourn\n"
    )
    hits = hits_for(text, cfg, city=CH)
    item = "2. Zoning ordinance amendment regarding short-term rental regulations in residential zones"
    assert items_matched_by(hits, "zoning") == [item]
    assert items_matched_by(hits, "short-term rental") == [item]


def test_zoning_does_not_match_when_rental_term_in_different_item(cfg):
    zoning_item = "3. Zoning map amendment for 1500 E Fort Union Blvd from R-1-8 to MU"
    rental_item = "4. Discussion of short-term rental regulations"
    text = (
        "CITY COUNCIL BUSINESS MEETING AGENDA\n"
        "\n"
        "1. Call to Order\n"
        "2. Consent agenda\n"
        f"{zoning_item}\n"
        f"{rental_item}\n"
        "5. Adjourn\n"
    )
    hits = hits_for(text, cfg, city=CH)
    # The rental item still matches on its own...
    assert items_matched_by(hits, "short-term rental") == [rental_item]
    # ...but the zoning item must NOT match just because a rental term is in a neighbouring item.
    assert zoning_item not in [h["item_text"] for h in hits]
    assert items_matched_by(hits, "zoning") == []


def test_conditional_use_does_not_match_when_rental_term_in_different_item(cfg):
    cu_item = "A. Conditional use permit for a cell tower at Mountview Park"
    text = (
        f"{cu_item}\n"
        "B. Transient room tax report for the second quarter\n"
    )
    hits = hits_for(text, cfg, city=SLC)
    assert cu_item not in [h["item_text"] for h in hits]
    # Since Oct 19 2026 transient room tax is weak (hotel tax): a quarterly TRT report alone is not a hit.
    assert items_matched_by(hits, "transient tax") == []


# 5 (matcher level) -------------------------------------------------------------------
def test_build_matchers_street_is_scoped_to_its_city(cfg):
    watch = dict(email="owner@example.com", city_ch=1, city_slc=1, keywords="short-term rental",
                 alerts_on=1, street_ch="100 Example St", street_slc="451 S State St")
    ov = aw.overlay_cfg_from_watch(cfg, watch)

    def streets(city):
        return [label for kind, label, *_ in aw.build_matchers(ov, city=city) if kind == "address"]

    assert streets(CH) == ["100 Example St"]
    assert streets(SLC) == ["451 S State St"]
    ch_text = "4. Site plan review for 100 Example Street"
    assert items_matched_by(hits_for(ch_text, ov, city=CH), "100 Example St") == [ch_text]
    assert hits_for(ch_text, ov, city=SLC) == []


def test_address_regex_abbreviation_variants():
    rx = aw.address_regex("100 Example St")
    for s in ("100 Example St", "100 Example Street", "100 Example St.", "100 Example street"):
        assert rx.search(s), s
    assert not rx.search("102 Example St")


# STR false positives (director-confirmed bug: garbled PDF site-plan text) ------------
def test_street_word_does_not_match_str(cfg):
    text = "1. Public hearing on 1300 West STREET improvements\n"
    assert items_matched_by(hits_for(text, cfg, city=CH), "STR") == []


def test_garbled_drawing_str_fragment_does_not_match(cfg):
    # pdftotext output of a site plan: STREET split into "STR" / "(PRIVA EET F"
    text = "PORCH\n\nGARAGE\n\nSTR\n(PRIVA EET F\n\nGARAG\nE\n\nBUFFALO FORK STR\n\nSTR\n(WI\n"
    assert hits_for(text, cfg, city=CH) == []


def test_structure_does_not_match_str(cfg):
    text = "2. Accessory STRUCTURE setback variance\n"
    assert items_matched_by(hits_for(text, cfg, city=CH), "STR") == []


def test_str_ordinance_amendment_matches(cfg):
    text = "AGENDA\n\n1. Call to Order\n2. STR ordinance amendment\n3. Adjourn\n"
    assert items_matched_by(hits_for(text, cfg, city=CH), "STR") == ["2. STR ordinance amendment"]


def test_discussion_of_str_licensing_matches(cfg):
    text = "AGENDA\n\n1. Discussion of STR licensing\n"
    assert items_matched_by(hits_for(text, cfg, city=CH), "STR") == ["1. Discussion of STR licensing"]


def test_short_term_rental_licensing_matches(cfg):
    text = "AGENDA\n\n4. Short-term rental licensing\n"
    assert items_matched_by(hits_for(text, cfg, city=CH), "short-term rental") == ["4. Short-term rental licensing"]


def test_short_term_rentals_spaced_lowercase_matches(cfg):
    text = "AGENDA\n\n5. Update on short term rentals in residential zones\n"
    assert items_matched_by(hits_for(text, cfg, city=CH), "short-term rental") == [
        "5. Update on short term rentals in residential zones"
    ]


def test_weak_term_with_garbled_str_in_same_item_does_not_match(cfg):
    text = "ZONING\nSTR\n(PRIVA EET F\nGARAGE\n"
    assert hits_for(text, cfg, city=CH) == []
