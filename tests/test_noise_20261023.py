"""Oct 23 2026 owner order (West Jordan / Layton review): drop (a) performance-measures rows, (b) software /
vendor cost lines, (c) municipal code printed inside an unrelated packet; keep real short-term-rental items
and fee-schedule lines that set or CHANGE the STR fee. Texts are real lines from the cached WJ / Layton packets."""
import json
from pathlib import Path

import agenda_watch as aw

KW = {"keywords": ["short-term rental", "STR", "rental license", "business license", "zoning",
                   "conditional use", "transient tax", "occupancy"]}
M = aw.build_keyword_matchers(KW)


def hits(text, doc="Council Meeting Packet.pdf"):
    return aw.find_hits(text, M, doc_name=doc)


def terms(text, **kw):
    return sorted({h["matched"] for h in hits(text, **kw)})


def drops(text):
    d = []
    aw.find_hits(text, M, doc_name="x.pdf", drops=d)
    return sorted({x["rule"] for x in d})


# ---- (a) performance-measures table rows -------------------------------------------------
PERF = ("PERFORMANCE & WORKLOAD MEASURES\n% of new inspections within 7 calendar days\n% of reinspections within 7 "
        "calendar days\n# of administrative law hearings\n# of businesses licensed annually\nGood Landlord inspections\n"
        "Short-term Rental Renewals\nWall cap cases\nCUP cases / compliance\nSurvey Results (0-100):\n"
        "Resident satisfaction with code enforcement\n")


def test_metric_label_row_is_dropped():
    assert terms(PERF) == []
    assert drops(PERF) == ["metrics-table-row"]


def test_metric_row_without_header_but_with_metric_lines_is_dropped():
    t = PERF.replace("PERFORMANCE & WORKLOAD MEASURES\n", "")
    assert terms(t) == []


def test_bare_label_without_metrics_context_is_kept():
    t = "Agenda\nShort-term Rental Renewals\nNext item\n"
    assert terms(t) == ["short-term rental"]


def test_real_sentence_inside_a_measures_section_is_kept():
    t = PERF + "\nThe Council shall adopt an ordinance to regulate short-term rentals in residential zones.\n"
    assert "short-term rental" in terms(t)


# ---- (b) software / vendor cost budget lines ---------------------------------------------
VENDOR = ("Indirect Costs Services provided to this department by other funds.\n33%\nIT services, software cost "
          "recovery, risk assessment, fleet management\nCityWorks and Granicus (PrimeGov, short-term rental)\n$\n"
          "Add risk assessment\n\n2,000\n")


def test_vendor_cost_line_is_dropped():
    assert terms(VENDOR) == []
    assert drops(VENDOR) == ["vendor-cost-line"]


def test_str_monitoring_contract_is_kept():
    t = "Approve a contract with Granicus Host Compliance for short-term rental monitoring and software support."
    assert terms(t) == ["short-term rental"]


def test_ordinance_sentence_mentioning_software_is_kept():
    t = "5. Ordinance 26-12 amending Chapter 5 to regulate short-term rental operators; staff will use software to track licenses."
    assert "short-term rental" in terms(t)


# ---- (c) code printed inside an unrelated packet --------------------------------------------
ADU = ("j.\n\nNot for Short-Term Rental. The accessory dwelling unit shall be rented for 30 consecutive days or "
       "longer by the same occupant.\n\nk.\n\nApplication for ADU Permit.\n")
PRINT_HEAD = "2/10/26, 3:00 PM\n\ncivicLinQ Code Hosting\n\n"


def _printout(head_text, n=5):
    body = "Land use text. " * 30
    return head_text + "".join(PRINT_HEAD + body + "\n\n" for _ in range(n // 2)) + PRINT_HEAD + ADU + PRINT_HEAD + body * 2 + PRINT_HEAD + body


def test_code_printed_under_an_appraisal_addendum_is_dropped():
    t = _printout("Planned Trail - Eastman\n\nAddenda\n\nAddendum D\nZoning Ordinance\n\nPlanned Trail - Eastman\n\n")
    assert terms(t) == []
    assert drops(t) == ["code-printout-in-attachment"]


def test_real_adu_text_amendment_is_kept():
    t = ("Text Amendment - Accessory Dwelling Unit Standards - Amending Title 19 Zoning, Chapter 19.02 Definitions; "
         "and Section 19.06.300 - Ordinance 26-27\n\n" + ADU)
    assert terms(t) == ["short-term rental"]


def test_printout_directly_under_a_text_amendment_heading_is_kept():
    t = _printout("Subject: Text Amendment - Amending Title 19 Zoning, Chapter 19.06\n\nExhibit A\n\n")
    assert "short-term rental" in terms(t)


# ---- (d) fee-schedule rows ------------------------------------------------------------------
def fee(str_date=None, drop_revision_table=False, extra=""):
    t = ("CONSOLIDATED FEE SCHEDULE\nEffective through June 30, 2027\nUpdated June 23, 2026 Res. 26-021\n\n"
         "BUSINESS LICENSING\n5) Rental Dwelling Units (annual fee)\n"
         "a) Application (one-time) ...................................... $55\n"
         "b) Good Landlord Program Rental License Fee ........................ $50 per rental unit\n"
         "c) Standard Fee ........................................ $325 per rental unit\n"
         "d) Short-Term Rental License Fee ................................ $325 per rental unit" + extra + "\n\n")
    if not drop_revision_table:
        t += ("CONSOLIDATED FEE SCHEDULE\nEffective through June 30, 2027\nUpdated June 23, 2026 Res. 26-021\n\n"
              "APPENDIX (continued)\n\nRevised\n\nBUSINESS LICENSING\n5) Rental Dwelling Units (annual fee)\n"
              "a) Application (one-time) ....................................... 06/26/2024\n"
              "b) Good Landlord Program Rental License Fee ........................ 04/22/2025\n"
              "c) Standard Fee ........................................ 04/22/2025\n"
              f"d) Short-Term License Fee ................................ {str_date or '04/22/2025'}\n")
    return t


def test_str_fee_unchanged_per_revision_table_is_dropped():
    assert terms(fee()) == []
    assert set(drops(fee())) == {"str-fee-unchanged", "non-str-rental-fee-row"}


def test_str_fee_changed_in_this_update_is_kept():
    t = fee(str_date="06/23/2026")
    assert terms(t) == ["rental license", "short-term rental"]
    hs = hits(t)
    assert all("Short-Term Rental License Fee" in h["item_text"] for h in hs)    # Good Landlord rows stay dropped


def test_str_fee_with_no_revision_evidence_is_kept_as_ambiguous():
    t = fee(drop_revision_table=True)
    assert "short-term rental" in terms(t)
    assert all("Short-Term Rental License Fee" in h["item_text"] for h in hits(t))


def test_str_fee_with_explicit_change_mark_is_kept():
    assert "short-term rental" in terms(fee(drop_revision_table=True, extra=" (new)"))


def test_good_landlord_row_alone_is_dropped_but_kept_when_it_changed():
    gl = ("Updated June 23, 2026 Res. 26-021\nb) Good Landlord Program Rental License Fee ............ $50 per rental unit\n"
          "APPENDIX\nRevised\nb) Good Landlord Program Rental License Fee ............ {d}\n")
    assert terms(gl.format(d="04/22/2025")) == []
    assert terms(gl.format(d="06/23/2026")) == ["rental license"]


def test_revision_table_row_alone_follows_its_date():
    old = "Updated June 23, 2026 Res. 26-021\nd) Short-Term Rental License Fee ................ 04/22/2025\n"
    new = old.replace("04/22/2025", "06/23/2026")
    assert terms(old) == [] and terms(new) == ["rental license", "short-term rental"]


def test_agenda_item_about_a_str_fee_is_kept():
    t = "6. Public Hearing - Resolution 26-50 increasing the short-term rental license fee from $150 to $325."
    assert "short-term rental" in terms(t)


# ---- regressions on the exact Oct 23 results ---------------------------------------------
def test_alerts_on_for_wj_and_layton_after_owner_gate():
    # 2026-10-25: owner turned WJ/Layton alerts on (WJ 0 hits, Layton 1 = past Sept 17 ADU amendment).
    cfg = json.loads((Path(__file__).resolve().parents[1] / "config.json").read_text())
    assert not {"wj", "layton"} & set(cfg["alerts_off_cities"])
    import aw_alerts
    assert not aw_alerts.off_cities(cfg) & {"wj", "layton"}


def test_noise_rules_do_not_touch_street_hits():
    sm = aw.build_street_matchers("100 Example St")
    t = "PERFORMANCE MEASURES\n# of x\n# of y\n100 Example St rezone"
    assert [h for h in aw.find_hits(t, sm, doc_name="a.pdf")]
