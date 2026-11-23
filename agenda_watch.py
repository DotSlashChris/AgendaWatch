#!/usr/bin/env python3
"""Agenda Watch - build step 1: fetcher + matcher.

Pulls live posted meeting notices/agendas for the ten supported Utah cities (config.json
states -> metros -> cities; see aw_cities.py) from Utah Public Notice (https://www.utah.gov/pmn/), extracts notice text and attached
PDF text (poppler pdftotext), matches keywords / street addresses, and builds ONE
owner-only email (digest, max N items) for UPCOMING meetings only. Agenda Watch flags
public agenda text; it is not legal advice.

Product rules (director — recorded in README / config._product_rules):
  - Past meetings are a labeled example only, never an alert.
  - Free = one city + one optional street on that city, upcoming only.
  - Paid = $5/month per extra city (subscription quantity); allowed cities = 1 + extra cities,
    capped at the ten supported cities. Street included with each city, never charged per street.
    Ignore multi-email signups for now.
  - A street matches ONLY in that city's agendas; keywords remain global across enabled cities.

PMN endpoints used (discovered from https://www.utah.gov/pmn/js/app.js):
  GET /pmn/list/entities.html?id=<govTypeId>        -> entity ids  (3 = Municipality)
  GET /pmn/list/publicBodies.html?id=<entityId>     -> public body ids
  GET /pmn/list/notices.html?id=<bodyId>&page=0     -> notices (upcoming + past 6 months)
  GET /pmn/sitemap/notice/<noticeId>.html           -> notice detail
  GET /pmn/files/<fileId>.pdf                       -> attached agenda / packet PDF

Commands:
  python3 agenda_watch.py run         fetch + match using config.json cities/keywords/addresses (no send)
  python3 agenda_watch.py run-owner   daily owner run: fetch + scan EVERY supported city; alert email
                                      (outbox) only for new upcoming hits in cities ENABLED on the
                                      owner's watch in data/agenda_watch.db (no send). Writes a
                                      per-city report to state/last_run_summary.json.
  python3 agenda_watch.py mark-sent <outbox.json> <gmail_message_id>   record items into state/sent_history.json

run-owner exit codes:
  0  silence ok (or ALERTS_OFF)
  1  soft error (missing watch / bad data)
  2  fatal fetch failure (PMN municipality list unreachable; nothing scanned)
  3  fetch failure in one or more cities: reported per city, NO email prepared
     (any hits from that run's partial data are withheld)
  4  owner email prepared in outbox/ (bot sends; this script never emails)
"""
import bisect
import datetime as dt
import hashlib
import html
import json
import re
import os
import sqlite3
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor
import sys
import time
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

import aw_cities
import aw_guard
import aw_alerts
import aw_store

ROOT = Path(__file__).resolve().parent
CACHE = ROOT / "cache"
STATE = ROOT / "state"
OUTBOX = ROOT / "outbox"
HISTORY = STATE / "sent_history.json"
DB = Path(os.environ.get("AW_DB") or (ROOT / "data" / "agenda_watch.db"))  # AW_DB: tests / throwaway runs
aw_alerts._AW = sys.modules[__name__]   # per-user matching reuses this module's matcher
SUMMARY_FILE = "last_run_summary.json"
UA = "AgendaWatch/0.1 (owner-only build test; contact via owner)"
FOOTER = "Agenda Watch flags public agenda text. It is not legal advice."
TZ = ZoneInfo("America/Denver")

# ---------------------------------------------------------------- matching rules
# Strong = standalone hit. Weak = only with strong rental language nearby (~400 chars)
# or as context for an address hit.
STR_TOKEN = r"(?-i:\bSTRs?\b)"
STRONG_KEYWORDS = {
    "short-term rental": (r"\bshort[\s\-\u2010-\u2014]*term\s+rentals?\b", 10),
    # Uppercase only (case-sensitive even though matchers compile with re.I), whole token:
    # STREET / STRUCTURE / STRAND / "str." never match. A bare STR must also pass
    # str_reads_as_rental() on its agenda item (drops garbled PDF drawing fragments).
    "STR": (STR_TOKEN, 9),
    "rental license": (r"\brental\s+licens(?:e|es|ing)\b", 8),
}
WEAK_KEYWORDS = {
    # Transient room tax is the hotel/lodging tax: in budgets, revenue tables and CRA / tax-increment
    # agreements it says nothing about rentals (Provo Oct 19: 14 of 21 hits). Weak since Oct 19 2026:
    # it needs rental language (short-term rental, rental license, nightly/vacation rental, STR) in the
    # same agenda item.
    "transient tax": (r"\btransient\s+(?:room\s+)?tax(?:es)?\b", 8),
    "business license": (r"\bbusiness\s+licens(?:e|es|ing)\b", 6),
    "conditional use": (r"\bconditional\s+uses?\b", 5),
    "occupancy": (r"\boccupanc(?:y|ies)\b", 3),
    "zoning": (r"\bzoning\b", 2),
}
KEYWORD_PATTERNS = {**STRONG_KEYWORDS, **WEAK_KEYWORDS}
STRONG_NEARBY_RE = re.compile(
    r"\b(?:short[\s\-\u2010-\u2014]*term\s+rentals?|"
    r"rental\s+licens(?:e|es|ing)|(?:nightly|vacation)\s+rentals?)\b|" + STR_TOKEN,
    re.I,
)
STR_RE = re.compile(STR_TOKEN)
# "STR" counts as the rental abbreviation only when its agenda item has rental context
# or reads as normal prose (>= 1 ordinary lowercase word of 3+ letters). PDF site-plan drawings yield
# items like "STR (PRIVA EET F", "BUFFALO FORK STR", "STR (WI": all-caps fragments, no prose.
# Needs prose (drawing fragments are all caps) AND, since Oct 19 2026, plain prose is no longer enough (West Jordan grant text "STR, Y-STR, or
# mitochondrial DNA testing" matched): the item needs housing/rental/regulatory context, and STR glued
# to a hyphen (Y-STR, STR-1) or in a DNA/forensics item never counts.
STR_CONTEXT_RE = re.compile(
    r"\b(?:rent\w*|licens\w*|ordinances?|regulat\w*|lodging|nightly|vacation|airbnb|vrbo|permits?|"
    r"enforcement|code|zon\w*|amend\w*|dwellings?|housing|homes?|ADUs?|neighbou?rhoods?|owners?)\b", re.I)
STR_NOT_RENTAL_RE = re.compile(r"\b(?:DNA|forensic\w*|loci|locus|genotyp\w*|laborator\w*)\b|-STR\b|\bSTR-", re.I)
PROSE_WORD_RE = re.compile(r"\b[a-z]{3,}\b")


def str_reads_as_rental(item_text):
    return (bool(STR_CONTEXT_RE.search(item_text)) and bool(PROSE_WORD_RE.search(item_text))
            and not STR_NOT_RENTAL_RE.search(item_text))


# Short-term rental / rental words that are not about rental rules:
#  - land-use tables listing "Short Term Rentals" among other use types (no prose, no rule words);
#  - fiscal boilerplate: the term appears only as a tax base ("levy on qualifying sales and
#    short-term rentals" in a tax-increment agreement) with no rule word in the sentence.
USE_TABLE_NOUN_RE = re.compile(
    r"\b(?:Dwellings?|Condominiums?|Apartments?|Facilit(?:y|ies)|Quarters|Services|Parks|Courts|Manufacturing|Duplex)\b")
RULE_WORD_RE = re.compile(
    r"\b(?:ordinances?|amend\w*|regulat\w*|licens\w*|prohibit\w*|permit\w*|shall|may not|must|allow\w*|"
    r"restrict\w*|bans?|banned|banning|discuss\w*|hearings?|consider(?:s|ed|ing)?|approv\w*|adopt\w*|code|chapter|enforc\w*|"
    r"fees?|not for|not be used)\b", re.I)
FISCAL_RE = re.compile(r"\b(?:increment|levy|sales tax|tax revenues?|room tax|tax sharing)\b", re.I)


def _sentence_at(text, s, e):
    a = max(text.rfind(". ", 0, s), text.rfind("\n\n", 0, s), text.rfind(";", 0, s), s - 400)
    b = [x for x in (text.find(". ", e), text.find("\n\n", e), text.find(";", e)) if x != -1]
    return text[max(0, a + 1):min(b + [e + 400])]


# Nov 3 2026 (owner order): a passing market mention of short-term rentals inside a lodging /
# transient-room / sales tax revenue report ("short-term rental demand declined" in Park City's
# Nov 5 2026 TRT report) is not an item about rental rules. Needs all three: the term is followed
# (or preceded) by a market word, a tax-revenue report is within ~1500 chars, and the sentence has
# no rule word. "Discussion regarding owner-occupied Short-Term Rentals" has no market word: kept.
STR_MARKET_RE = re.compile(
    r"^[\s\-\u2010-\u2014]*(?:demand|revenues?|collections?|bookings?|occupancy\s+rates?|activity|performance|"
    r"room\s+nights?|nights?\s+booked)\b|"
    r"\b(?:demand|revenues?|collections?|bookings?)\s+(?:for|from|of)\s*$", re.I)
TAX_REPORT_RE = re.compile(
    r"\b(?:transient\s+room\s+tax(?:es)?|TRT|lodging\s+tax(?:es)?|resort\s+tax(?:es)?|room\s+tax(?:es)?|sales\s+tax(?:es)?)\b"
    r"[^\n]{0,80}\b(?:revenues?|collections?|distributions?|receipts?)\b|"
    r"\b(?:revenues?|collections?|distributions?|receipts?)\b[^\n]{0,80}"
    r"\b(?:transient\s+room\s+tax(?:es)?|TRT|lodging\s+tax(?:es)?|resort\s+tax(?:es)?|room\s+tax(?:es)?|sales\s+tax(?:es)?)\b",
    re.I)
TAX_REPORT_WINDOW = 1500


def _is_tax_report_market_mention(doc_text, s, e):
    """A short-term-rental demand/revenue mention inside a tax revenue report (no rule word)."""
    after, before = doc_text[e:e + 40], doc_text[max(0, s - 40):s]
    if not (STR_MARKET_RE.search(after) or STR_MARKET_RE.search(before)):
        return False
    if RULE_WORD_RE.search(_sentence_at(doc_text, s, e)):
        return False
    return bool(TAX_REPORT_RE.search(doc_text, max(0, s - TAX_REPORT_WINDOW), min(len(doc_text), e + TAX_REPORT_WINDOW)))


def rental_term_is_noise(doc_text, s, e, item_text):
    """True for a short-term-rental match that is a use-table row, a tax-base mention, or a market
    mention in a tax revenue report."""
    if not RULE_WORD_RE.search(item_text) and len(USE_TABLE_NOUN_RE.findall(item_text)) >= 3:
        return True
    sent = _sentence_at(doc_text, s, e)
    if FISCAL_RE.search(sent) and not RULE_WORD_RE.search(sent):
        return True
    return _is_tax_report_market_mention(doc_text, s, e)


# "certificate(s) of occupancy" is a building-permit milestone, not owner occupancy.
CERT_OCCUPANCY_RE = re.compile(r"certificates?\s+of\s*$", re.I)


def has_rental_language(item_text):
    """Strong rental language in an item; a bare STR must read as the rental abbreviation."""
    for mo in STRONG_NEARBY_RE.finditer(item_text):
        if STR_RE.fullmatch(mo.group(0)):
            if str_reads_as_rental(item_text):
                return True
        elif mo.group(0).lower().startswith("short") and rental_term_is_noise(item_text, mo.start(), mo.end(), item_text):
            continue   # a use-table row / tax-base mention is not rental language either
        else:
            return True
    return False


NEARBY_CHARS = 400
ADDRESS_WEIGHT = 12

# Minutes / comment-transcript signals — skip unless match looks like an agenda heading.
MINUTES_DOC_RE = re.compile(r"\bminutes?\b", re.I)
MINUTES_PHRASE_RE = re.compile(
    r"(?:spoke in (?:opposition|favor|support)|"
    r"there were no other comments|"
    r"Chair closed|"
    r"public comments? (?:were|was) (?:heard|taken|closed|opened)|"
    r"approved the minutes|"
    r"motion (?:was )?(?:made|seconded) to|"
    r"the following (?:persons?|individuals?) (?:spoke|addressed)|"
    r"minutes of the (?:regular |special )?(?:meeting|session))",
    re.I,
)
AGENDA_DOC_HINT_RE = re.compile(r"\b(?:agenda|packet|notice)\b", re.I)

ABBR = {
    "e": ["e", "east"], "east": ["e", "east"], "w": ["w", "west"], "west": ["w", "west"],
    "n": ["n", "north"], "north": ["n", "north"], "s": ["s", "south"], "south": ["s", "south"],
    "st": ["st", "street"], "street": ["st", "street"], "dr": ["dr", "drive"], "drive": ["dr", "drive"],
    "ave": ["ave", "av", "avenue"], "avenue": ["ave", "av", "avenue"], "rd": ["rd", "road"], "road": ["rd", "road"],
    "blvd": ["blvd", "boulevard"], "boulevard": ["blvd", "boulevard"], "ln": ["ln", "lane"], "lane": ["ln", "lane"],
    "cir": ["cir", "circle"], "circle": ["cir", "circle"], "ct": ["ct", "court"], "court": ["ct", "court"],
    "way": ["way", "wy"], "pkwy": ["pkwy", "parkway"], "parkway": ["pkwy", "parkway"],
}
DIRECTIONS = {"e", "east", "w", "west", "n", "north", "s", "south"}


def address_regex(addr):
    toks = re.findall(r"[A-Za-z0-9]+", addr.lower())
    parts = []
    for t in toks:
        alts = ABBR.get(t, [t])
        alt = "(?:" + "|".join(re.escape(a) + r"\.?" for a in alts) + ")"
        if t in DIRECTIONS:
            alt = "(?:" + alt + r"[\s,]+)?"  # direction optional
            parts.append(alt)
        else:
            parts.append(alt + r"[\s,]*")
    return re.compile(r"\b" + "".join(parts).rstrip("*").rstrip(r"[\s,]") + r"\b", re.I)


def build_keyword_matchers(cfg):
    """Keywords are global across every enabled city."""
    m = []
    for kw in cfg["keywords"]:
        if kw in KEYWORD_PATTERNS:
            pat, w = KEYWORD_PATTERNS[kw]
            strength = "strong" if kw in STRONG_KEYWORDS else "weak"
        else:
            pat, w = r"\b" + re.escape(kw) + r"\b", 3
            strength = "strong"  # unknown custom keywords treated as strong
        m.append(("keyword", kw, re.compile(pat, re.I), w, strength))
    return m


def build_street_matchers(street):
    """One optional street for a single city. Empty -> no address matchers."""
    a = (street or "").strip()
    if not a:
        return []
    return [("address", a, address_regex(a), ADDRESS_WEIGHT, "address")]


def build_matchers(cfg, city=None):
    """Keyword matchers + optional city-scoped street.
    If city is given, only that city's street is included (never cross-city).
    If city is None, include every street from city_streets / legacy addresses
    (legacy CLI config.json path only).
    """
    m = build_keyword_matchers(cfg)
    if city is not None:
        streets_map = cfg.get("city_streets") or {}
        street = streets_map.get(city) or (cfg.get("cities", {}).get(city) or {}).get("street") or ""
        m.extend(build_street_matchers(street))
        return m
    # Legacy / unscoped: prefer city_streets values, else flat addresses list
    streets_map = cfg.get("city_streets") or {}
    if streets_map:
        seen = set()
        for street in streets_map.values():
            s = (street or "").strip()
            if s and s not in seen:
                seen.add(s)
                m.extend(build_street_matchers(s))
    else:
        for a in cfg.get("addresses", []):
            m.extend(build_street_matchers(a))
    return m


ITEM_START = re.compile(
    r"^\s*(?:\d{1,2}:\d{2}\s*[ap]\.?m\.?\s+)?"
    r"(?:\(?\d{1,2}(?:\.\d{1,2})*[.)]|\(?[a-zA-Z][.)]|[IVX]{1,5}\.|[A-Z]\-\d+|\u2022|-)\s+\S"
)


def item_text_for(lines, idx):
    """Return the agenda item (line/paragraph) containing line idx."""
    start = idx
    for k in range(idx, max(-1, idx - 8), -1):
        if not lines[k].strip():
            start = k + 1
            break
        start = k
        if ITEM_START.match(lines[k]):
            break
    end = idx
    for k in range(idx + 1, min(len(lines), idx + 8)):
        if not lines[k].strip() or ITEM_START.match(lines[k]):
            break
        end = k
    txt = " ".join(l.strip() for l in lines[start:end + 1])
    txt = re.sub(r"\s+", " ", txt).strip()
    if len(txt) > 700:
        txt = txt[:700].rsplit(" ", 1)[0] + " ..."
    return txt


def excerpt_for(text, s, e, width=300):
    half = (width - (e - s)) // 2
    a, b = max(0, s - half), min(len(text), e + half)
    ex = re.sub(r"\s+", " ", text[a:b]).strip()
    return ("..." if a > 0 else "") + ex + ("..." if b < len(text) else "")


def looks_like_agenda_heading(item_text):
    """True if the item text starts like a numbered/lettered agenda action item."""
    return bool(ITEM_START.match(item_text.strip()))


def is_minutes_context(doc_name, item_text, excerpt):
    """Skip minutes/comment transcripts unless the match is clearly an agenda heading."""
    blob = f"{item_text}\n{excerpt}"
    doc_is_minutes = bool(MINUTES_DOC_RE.search(doc_name or ""))
    phrase_hit = bool(MINUTES_PHRASE_RE.search(blob))
    if not doc_is_minutes and not phrase_hit:
        return False
    # Allow through only if it still looks like an action-item heading AND
    # the document is not pure minutes (or heading is strong enough on agenda/packet).
    if looks_like_agenda_heading(item_text) and not phrase_hit:
        return False
    if looks_like_agenda_heading(item_text) and AGENDA_DOC_HINT_RE.search(doc_name or "") and not doc_is_minutes:
        return False
    return True


def weak_has_strong_nearby(doc_text, match_start, match_end):
    a = max(0, match_start - NEARBY_CHARS)
    b = min(len(doc_text), match_end + NEARBY_CHARS)
    return bool(STRONG_NEARBY_RE.search(doc_text[a:b]))


# ------------------------------------------------------------------ Oct 23 2026 noise rules
# West Jordan / Layton review (owner order): drop three kinds of non-items, keep real STR items and
# fee lines that set or change the STR fee. General rules, never keyed to a file name or city.
#  (a) budget performance-measures / KPI table rows where the rental term is only a metric label;
#  (b) software / vendor / IT cost-recovery budget lines where the rental term only names a product;
#  (c) municipal code printed inside an unrelated packet (addendum / appraisal / exhibit printouts);
#  (d) fee-schedule rows: a short-term-rental fee row is kept unless the schedule's own revision
#      table shows it was NOT changed in this update; other rental-licence fee rows are STR-noise
#      unless the evidence shows they changed.
NOISE_LABELS = {"short-term rental", "STR", "rental license"}
PERF_HEADER_RE = re.compile(
    r"\bPERFORMANCE\s*(?:&|AND)?\s*(?:WORKLOAD\s+)?(?:MEASURES|INDICATORS|METRICS)\b|\bWORKLOAD\s+MEASURES\b|"
    r"\bKEY\s+PERFORMANCE\s+INDICATORS\b|\bKPIs?\b|\bOUTPUT\s+MEASURES\b|\bOUTCOME\s+MEASURES\b", re.I)
METRIC_LINE_RE = re.compile(r"^\s*(?:#|%|No\.)\s*of\b|\bSurvey\s+Results\b", re.I)
VENDOR_RE = re.compile(
    r"\b(?:Granicus|PrimeGov|CityWorks|Tyler\s+Tech\w*|Munis|Accela|Energov|OpenGov|CivicPlus|Laserfiche|"
    r"software|cost\s+recovery|indirect\s+costs?|IT\s+services|subscriptions?|fleet\s+management)\b", re.I)
STR_VENDOR_KEEP_RE = re.compile(r"\b(?:host\s+compliance|deckard|rentalscape|identifii|lodgify|compliance\s+monitoring)\b", re.I)
CODE_PRINT_RE = re.compile(r"civic\s*linq|code\s+hosting|municode|american\s+legal\s+publishing|codepublishing|"
                           r"library\.municode", re.I)
ATTACH_RE = re.compile(r"\b(?:addend(?:um|a)|appendix|appendices|exhibit|attachment|appraisal)\b", re.I)
REAL_AMENDMENT_RE = re.compile(r"text\s+amendment|amend(?:ing|ment)?\s+(?:title|chapter|section)", re.I)
STR_ROW_RE = re.compile(r"short[\s\-\u2010-\u2014]*term|nightly|vacation|\bSTR\b", re.I)
FEE_ROW_RE = re.compile(r"[.\u2026]{3,}\s*(?:\$\s?\d|\d{1,2}/\d{1,2}/\d{2,4}|no\s+charge)|\$\s?\d[\d,]*(?:\.\d\d)?\s+per\b", re.I)
UPDATED_RE = re.compile(r"\bUpdated\s+([A-Z][a-z]+)\.?\s+(\d{1,2}),\s+(\d{4})")
DATE_ROW_RE = re.compile(r"^(.*?)\s*[.\u2026]{3,}\s*(\d{1,2})/(\d{1,2})/(\d{4})\s*$")
FEE_CHANGE_MARK_RE = re.compile(r"\(\s*new\s*\)|\bnew\s+fee\b|\bincreas(?:e|ed)\b|\bproposed\b|\bwas\s+\$|\bfrom\s+\$\s?\d[\d,.]*\s+to\s+\$", re.I)


def _is_metric_label(lines, li):
    """(a) The rental term is a bare label in a performance-measures / KPI table."""
    line = lines[li].strip()
    if len(line.split()) > 8 or RULE_WORD_RE.search(line) or line.endswith((".", ";", ":")):
        return False
    before = lines[max(0, li - 25):li]
    near = lines[max(0, li - 14):li + 8]
    if any(PERF_HEADER_RE.search(l) for l in before):
        return True
    return sum(1 for l in near if METRIC_LINE_RE.search(l)) >= 2


def _is_vendor_cost_line(doc_text, s, e):
    """(b) The rental term only names a product/module inside a software / IT cost budget line."""
    w = doc_text[max(0, s - 160):e + 160]
    if STR_VENDOR_KEEP_RE.search(w) or RULE_WORD_RE.search(w):
        return False
    return bool(VENDOR_RE.search(w))


def _is_code_printout(doc_text, s):
    """(c) Municipal code reproduced as an attachment (code-hosting printout under an addendum /
    appendix / exhibit / appraisal), not an agenda item about the code. Layton's real Ordinance 26-27
    text amendment has no code-hosting header, so it is never caught here."""
    marks = [m.start() for m in CODE_PRINT_RE.finditer(doc_text, max(0, s - 6000), min(len(doc_text), s + 6000))]
    if len(marks) < 3:
        return False
    first = marks[0]
    # walk back over the contiguous run of printout headers to the start of the attachment
    while True:
        prev = [m.start() for m in CODE_PRINT_RE.finditer(doc_text, max(0, first - 15000), first)]
        if not prev:
            break
        first = prev[0]
    head = doc_text[max(0, first - 700):first + 200]
    if not ATTACH_RE.search(head):
        return False
    return not REAL_AMENDMENT_RE.search(doc_text[max(0, first - 700):first])


def _norm_fee_label(line):
    lab = re.split(r"\s*[.\u2026]{3,}|\s+\$\s?\d", line, maxsplit=1)[0]
    lab = re.sub(r"^\W*(?:\(?[A-Za-z0-9]{1,3}[.)])\s*", "", lab.strip())
    return re.sub(r"[^a-z0-9]", "", re.sub(r"\brental\b", "", lab.lower()))


def _date_of(m, off=2):
    try:
        return dt.date(int(m.group(off + 2)), int(m.group(off)), int(m.group(off + 1)))
    except ValueError:
        return None


def fee_row_change(doc_text, lines, li, pos):
    """'changed' | 'unchanged' | 'unknown' for a fee-schedule row, from the schedule's own evidence:
    the 'Updated <date>' header, a revision-date table (label ... mm/dd/yyyy), or explicit change marks."""
    line = lines[li]
    upd = None
    for m in UPDATED_RE.finditer(doc_text, 0, pos):
        try:
            upd = dt.datetime.strptime(f"{m.group(1)} {m.group(2)} {m.group(3)}", "%B %d %Y").date()
        except ValueError:
            pass
    m = DATE_ROW_RE.match(line.strip())
    if m:                                    # this row IS a revision-table row
        d = _date_of(m, 2)
        return "unknown" if (d is None or upd is None) else ("changed" if d >= upd else "unchanged")
    lab = _norm_fee_label(line)
    if lab and upd:
        dates = []
        for l in lines:
            mm = DATE_ROW_RE.match(l.strip())
            if mm and _norm_fee_label(l) == lab:
                d = _date_of(mm, 2)
                if d:
                    dates.append(d)
        if dates:
            return "changed" if any(d >= upd for d in dates) else "unchanged"
    if FEE_CHANGE_MARK_RE.search(" ".join(lines[li:li + 3])):
        return "changed"
    return "unknown"


def noise_reason(doc_text, lines, li, s, e, label):
    """Why a keyword match is a non-item (owner order Oct 23 2026), or None."""
    line = lines[li]
    if _is_metric_label(lines, li):
        return "metrics-table-row"
    if _is_vendor_cost_line(doc_text, s, e):
        return "vendor-cost-line"
    if _is_code_printout(doc_text, s):
        return "code-printout-in-attachment"
    if label in NOISE_LABELS and FEE_ROW_RE.search(line):
        status = fee_row_change(doc_text, lines, li, s)
        if STR_ROW_RE.search(re.split(r"\s*[.\u2026]{3,}", line, maxsplit=1)[0]):
            if status == "unchanged":
                return "str-fee-unchanged"
        elif status != "changed":
            return "non-str-rental-fee-row"
    return None


def find_hits(doc_text, matchers, doc_name="", drops=None):
    lines = doc_text.splitlines()
    offsets, pos = [], 0
    for l in lines:
        offsets.append(pos)
        pos += len(l) + 1
    hits = []
    for kind, label, rx, w, strength in matchers:
        for mo in rx.finditer(doc_text):
            # skip matches inside URLs / emails
            lstart = doc_text.rfind("\n", 0, mo.start()) + 1
            tokstart = max(
                doc_text.rfind(" ", 0, mo.start()),
                doc_text.rfind("\t", 0, mo.start()),
                lstart - 1,
            ) + 1
            token = (
                doc_text[tokstart:mo.end() + 40].split()[0]
                if doc_text[tokstart:mo.end() + 40].split()
                else ""
            )
            if "http" in token or "www." in token or "@" in token or "/" in token[: mo.start() - tokstart + 1]:
                continue

            # Weak keywords need strong rental language nearby (~400 chars).
            if strength == "weak" and not weak_has_strong_nearby(doc_text, mo.start(), mo.end()):
                continue

            li = bisect.bisect_right(offsets, mo.start()) - 1
            item = item_text_for(lines, li)
            # ...and that rental language must be in the SAME agenda item, not a neighbouring one.
            if strength == "weak" and not has_rental_language(item):
                continue
            # Bare STR: only when it reads as the rental abbreviation, not drawing text.
            if label == "STR" and kind == "keyword" and not str_reads_as_rental(item):
                continue
            if label == "short-term rental" and kind == "keyword" and rental_term_is_noise(doc_text, mo.start(), mo.end(), item):
                continue
            if label == "occupancy" and CERT_OCCUPANCY_RE.search(doc_text[max(0, mo.start() - 30):mo.start()]):
                continue
            if kind == "keyword":
                why = noise_reason(doc_text, lines, li, mo.start(), mo.end(), label)
                if why:
                    if drops is not None:
                        drops.append({"matched": label, "rule": why, "line": lines[li].strip()[:160],
                                      "start": mo.start(), "doc": doc_name})
                    continue
            excerpt = excerpt_for(doc_text, mo.start(), mo.end())
            if is_minutes_context(doc_name, item, excerpt):
                continue

            hits.append({
                "match_type": kind,
                "matched": label,
                "matched_text": mo.group(0),
                "weight": w,
                "strength": strength,
                "item_text": item,
                "excerpt": excerpt,
                "start": mo.start(), "end": mo.end(),   # offsets in doc_text (per-user alert excerpts)
            })
    return hits


# ---------------------------------------------------------------- fetching
class FetchError(Exception):
    pass


class PdfMissing(FetchError):
    """A PMN attachment returned HTTP 404. Skip this PDF only; do not fail the city."""
    def __init__(self, url, detail="HTTP 404"):
        self.url = url
        self.detail = detail
        super().__init__(f"PMN PDF missing (skipped): {url} {detail}")


class BudgetExceeded(FetchError):
    """Daily PMN request budget reached: no more outbound requests today."""


# Daily outbound-request budget (aw_guard cap_pmn_requests). cap None = not armed (unit tests of get()).
BUDGET = {"cap": None, "used": 0, "start": 0, "tripped": False, "lock": threading.Lock()}


def _take_request():
    if BUDGET["cap"] is None:
        return
    with BUDGET["lock"]:
        if BUDGET["used"] >= BUDGET["cap"]:
            BUDGET["tripped"] = True
            raise BudgetExceeded(f"daily PMN request budget reached ({BUDGET['cap']} requests/day); "
                                 "fetch stopped for today")
        BUDGET["used"] += 1


S = requests.Session()
S.headers["User-Agent"] = UA


def get(url, binary=False, tries=3):
    last = None
    for i in range(tries):
        _take_request()
        try:
            r = S.get(url, timeout=45)
            if r.status_code == 200:
                return r.content if binary else r.text
            last = f"HTTP {r.status_code}"
        except requests.RequestException as ex:
            last = repr(ex)
        time.sleep(1.5 * (i + 1))
    raise FetchError(f"GET {url} failed: {last}")


def parse_id_list(page, section_id, url):
    soup = BeautifulSoup(page, "html.parser")
    sec = soup.find(id=section_id)
    if sec is None:
        raise FetchError(f"structure change: #{section_id} not found in {url}")
    out = {}
    for a in sec.select("li a[id]"):
        out[html.unescape(a.get_text(strip=True))] = a["id"]
    if not out:
        raise FetchError(f"structure change: no ids under #{section_id} in {url}")
    return out


def list_notices(base, body_id):
    url = f"{base}/pmn/list/notices.html?id={body_id}&page=0"
    soup = BeautifulSoup(get(url), "html.parser")
    res = soup.find(id="browseResults")
    if res is None:
        raise FetchError(f"structure change: #browseResults missing in {url}")
    rows = []
    for tr in res.select("tbody tr"):
        tds = tr.find_all("td")
        if len(tds) < 2:
            continue
        a = tds[0].find("a", href=True)
        if not a:
            continue
        try:
            when = dt.datetime.strptime(tds[1].get_text(strip=True), "%Y/%m/%d %I:%M %p")
        except ValueError:
            raise FetchError(
                f"structure change: unparseable event date '{tds[1].get_text(strip=True)}' in {url}"
            )
        # PMN event times are local Utah (America/Denver)
        when = when.replace(tzinfo=TZ)
        rows.append({"notice_url": base + a["href"], "title": a.get_text(" ", strip=True), "event": when})
    if not rows and "No notices" not in res.get_text() and "no notices" not in res.get_text().lower():
        if res.find("table") is None:
            raise FetchError(f"structure change: no notice table in {url}")
    return rows, url


def parse_notice(base, url):
    soup = BeautifulSoup(get(url), "html.parser")
    dl = {}
    for dt_ in soup.select("article dl dt"):
        dd = dt_.find_next_sibling("dd")
        if dd is not None:
            dl.setdefault(dt_.get_text(" ", strip=True), dd)
    if "Event Start Date & Time" not in dl:
        raise FetchError(f"structure change: 'Event Start Date & Time' missing in {url}")
    agenda_dd = soup.select_one("dd.agenda")
    agenda = agenda_dd.get_text("\n") if agenda_dd else ""
    pdfs = []
    for a in soup.select("article a[href]"):
        h = a["href"]
        if re.search(r"/pmn/files/\d+\.(pdf|PDF)$", h):
            u = h if h.startswith("http") else base + h
            if u not in [p[1] for p in pdfs]:
                pdfs.append((a.get_text(" ", strip=True) or u.rsplit("/", 1)[-1], u))
    return {
        "entity": dl.get("Entity").get_text(" ", strip=True) if dl.get("Entity") else "",
        "body": dl.get("Public Body").get_text(" ", strip=True) if dl.get("Public Body") else "",
        "title": dl.get("Notice Title").get_text(" ", strip=True) if dl.get("Notice Title") else "",
        "start": dl["Event Start Date & Time"].get_text(" ", strip=True),
        "edited": dl.get("Notice Last Edited On").get_text(" ", strip=True) if dl.get("Notice Last Edited On") else "",
        "agenda": agenda, "pdfs": pdfs,
    }


FETCH_WORKERS = 4  # bounded concurrency for notice detail pages + attachments


def notice_fingerprint(n):
    """Notice-level change key: PMN 'Notice Last Edited On' + sorted attachment URLs."""
    return (n.get("edited") or "") + "|" + "|".join(sorted(u for _, u in n["pdfs"]))


def get_doc(url, etag=None, last_modified=None, tries=3):
    """GET with optional conditional headers. Returns (status 200|304, bytes|None, headers)."""
    headers = {}
    if etag:
        headers["If-None-Match"] = etag
    if last_modified:
        headers["If-Modified-Since"] = last_modified
    last = None
    for i in range(tries):
        _take_request()
        try:
            r = S.get(url, timeout=90, headers=headers) if headers else S.get(url, timeout=90)
            if r.status_code == 304 and headers:
                return 304, None, r.headers
            if r.status_code == 200:
                return 200, r.content, r.headers
            last = f"HTTP {r.status_code}"
        except requests.RequestException as ex:
            last = repr(ex)
        time.sleep(1.5 * (i + 1))
    raise FetchError(f"GET {url} failed: {last}")


DOC_CHUNK = 1 << 20  # 1 MiB: attachments stream to disk; a 185 MB packet never sits in memory


def get_doc_to_file(url, dest, etag=None, last_modified=None, tries=3):
    """Like get_doc, but a 200 body is streamed to `dest` (never held in memory: 4 workers x 100-185 MB
    PMN packets OOM-killed the 1 GiB container on 2026-11-04). Returns (200|304, bytes written, headers)."""
    headers = {}
    if etag:
        headers["If-None-Match"] = etag
    if last_modified:
        headers["If-Modified-Since"] = last_modified
    last = None
    for i in range(tries):
        _take_request()
        try:
            r = (S.get(url, timeout=90, headers=headers, stream=True) if headers
                 else S.get(url, timeout=90, stream=True))
            try:
                if r.status_code == 304 and headers:
                    return 304, 0, r.headers
                if r.status_code == 404:
                    raise PdfMissing(url)  # do not retry; a missing PMN PDF will not become 200
                if r.status_code == 200:
                    n = 0
                    chunks = r.iter_content(DOC_CHUNK) if hasattr(r, "iter_content") else [r.content]
                    with open(dest, "wb") as f:
                        for chunk in chunks:
                            if chunk:
                                f.write(chunk)
                                n += len(chunk)
                    return 200, n, r.headers
                last = f"HTTP {r.status_code}"
            finally:
                close = getattr(r, "close", None)
                if close:
                    close()
        except requests.RequestException as ex:
            last = repr(ex)
        time.sleep(1.5 * (i + 1))
    raise FetchError(f"GET {url} failed: {last}")


def extract_pdf_text(pdf_path):
    txt = pdf_path.with_suffix(pdf_path.suffix + ".txt")
    try:
        p = subprocess.run(["pdftotext", "-enc", "UTF-8", str(pdf_path), str(txt)], capture_output=True, text=True)
        if p.returncode != 0:
            raise FetchError(f"pdftotext failed: {p.stderr.strip()}")
        return txt.read_text(errors="replace")
    finally:
        txt.unlink(missing_ok=True)


def fetch_document(url, notice_url, prev):
    """Text for one attachment. prev = stored documents row or None.
    Returns (text, row_to_save or None, how) with how in {'304', 'fetched'}. The PDF is deleted after extraction."""
    CACHE.mkdir(exist_ok=True)
    pdf = CACHE / f"{threading.get_ident()}-{re.sub(r'\W', '_', url.rsplit('/', 1)[-1])}"
    try:
        status, length, headers = get_doc_to_file(url, pdf, (prev or {}).get("etag"),
                                                  (prev or {}).get("last_modified"))
        if status == 304:
            return prev["text"], None, "304"
        with open(pdf, "rb") as f:
            if not f.read(4) == b"%PDF":
                raise FetchError(f"not a PDF: {url}")
        text = extract_pdf_text(pdf)
    finally:
        pdf.unlink(missing_ok=True)
    row = {"url": url, "notice_url": notice_url, "etag": headers.get("ETag"),
           "last_modified": headers.get("Last-Modified"), "length": length,
           "text_hash": hashlib.sha256(text.encode()).hexdigest(), "text": text}
    return text, row, "fetched"


def pdf_text(url):
    CACHE.mkdir(exist_ok=True)
    fid = re.sub(r"\W", "_", url.rsplit("/", 1)[-1])
    pdf, txt = CACHE / fid, CACHE / (fid + ".txt")
    if not txt.exists():
        data = get(url, binary=True)
        if not data.startswith(b"%PDF"):
            raise FetchError(f"not a PDF: {url}")
        pdf.write_bytes(data)
        p = subprocess.run(
            ["pdftotext", "-enc", "UTF-8", str(pdf), str(txt)],
            capture_output=True, text=True,
        )
        if p.returncode != 0:
            raise FetchError(f"pdftotext failed on {url}: {p.stderr.strip()}")
    return txt.read_text(errors="replace")



# ---------------------------------------------------------------- owner watch (SQLite)
legacy_watch_cities = aw_cities.legacy_watch_cities


def watch_city_state(watch):
    """city_key -> {enabled: bool, street: str} for a watch dict (new or legacy shape)."""
    if watch.get("cities") is not None:
        return {k: {"enabled": bool(v.get("enabled")), "street": (v.get("street") or "").strip()}
                for k, v in watch["cities"].items()}
    return legacy_watch_cities(watch)


def load_owner_watch(cfg):
    """Load the owner's watch: watches row + per-city rows (watch_cities). Returns dict or None.
    dict['cities'] = {city_key: {'enabled': bool, 'street': str}}."""
    email = ((cfg or {}).get("owner_email") or aw_cities.owner_email() or "").strip().lower()
    if not email or not DB.exists():
        return None
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    try:
        row = con.execute("SELECT * FROM watches WHERE email=?", (email,)).fetchone()
        if row is None:
            return None
        watch = dict(row)
        cities = {}
        has_wc = con.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='watch_cities'"
        ).fetchone()
        if has_wc:
            for r in con.execute(
                "SELECT city_key, enabled, street FROM watch_cities WHERE email=? ORDER BY city_key",
                (email,),
            ):
                cities[r["city_key"]] = {"enabled": bool(r["enabled"]), "street": r["street"] or ""}
    finally:
        con.close()
    if not cities:
        cities = legacy_watch_cities(watch)  # DB not migrated yet
    watch["cities"] = cities
    return watch


def overlay_cfg_from_watch(cfg, watch, scan_all=False):
    """Return a copy of cfg with the watch's cities/keywords/streets overlaid (config.json untouched).

    Each city gets watch_enabled (on the owner's watch) and street (only if that city is on).
    enabled = what run() scans: watch-enabled cities only, or EVERY supported city if scan_all.
    Streets stay city-scoped; keywords remain global.
    """
    import copy
    out = copy.deepcopy(aw_cities.expand_config(cfg))
    state = watch_city_state(watch)
    city_streets = {}
    for name, c in out["cities"].items():
        st = state.get(c.get("key")) or {}
        on = bool(st.get("enabled"))
        street = (st.get("street") or "").strip() if on else ""
        c["watch_enabled"] = on
        c["enabled"] = True if scan_all else on
        c["street"] = street
        city_streets[name] = street
    # Keywords: always honor the watch row (including empty = street-only).
    # Do NOT fall back to config.json defaults when the DB list is empty — that
    # made matching look like keywords "reset" after a wipe or intentional clear.
    if "keywords" in watch and watch.get("keywords") is not None:
        text = str(watch.get("keywords") or "").replace("\r\n", "\n").replace("\r", "\n").strip()
        if not text:
            out["keywords"] = []
        elif "\n" in text:
            out["keywords"] = [k.strip() for k in text.split("\n") if k.strip()]
        else:
            out["keywords"] = [k.strip() for k in re.split(r",+", text) if k.strip()]
    out["city_streets"] = city_streets
    # Flat list kept for summary/artifacts only (not used for cross-city matching)
    out["addresses"] = [v for v in city_streets.values() if v]
    out["_watch_email"] = watch.get("email")
    out["_watch_alerts_on"] = bool(watch.get("alerts_on"))
    return out


def watch_enabled_cities(cfg):
    return [c for c, cc in cfg["cities"].items() if cc.get("watch_enabled", cc.get("enabled"))]


def _hit_brief(h):
    return {k: h.get(k) for k in (
        "key", "city", "meeting_body", "notice_title", "meeting_datetime",
        "item_text", "excerpt", "link", "notice_url", "matched_terms", "score",
    )}


def write_run_artifacts(cfg, res, history):
    """Shared post-fetch: last_run, failures, summary, optional outbox email.
    Returns (exit_code, email_path_or_None, items, past_examples).

    Only hits in cities enabled on the watch (h['city_enabled'], default True) can be emailed.
    Any fetch failure -> no email at all (exit 3); hits from that partial run are withheld.
    """
    stamp = dt.datetime.now(TZ).strftime("%Y%m%d-%H%M%S")
    (STATE / "last_run.json").write_text(
        json.dumps({k: v for k, v in res.items() if not k.startswith("_")}, indent=2, default=str))
    if res.get("skipped_pdfs"):
        print(f"PMN PDF MISSING (skipped, {len(res['skipped_pdfs'])}): "
              f"city fetch still ok; alerts not withheld", file=sys.stderr)
        for s in res["skipped_pdfs"]:
            print("  - " + s, file=sys.stderr)
    if res["failures"]:
        (STATE / f"failures-{stamp}.txt").write_text("\n".join(res["failures"]))
        print(f"FAILURES ({len(res['failures'])}): see state/failures-{stamp}.txt", file=sys.stderr)
        for f in res["failures"]:
            print("  - " + f, file=sys.stderr)
        bad = [c["city"] for c in res.get("cities", []) if not c.get("fetch_ok")]
        if bad:
            print("FAILED CITIES: " + ", ".join(bad), file=sys.stderr)
    if res.get("fatal"):
        return 2, None, [], []
    print(
        f"notices scanned: {res['notices_scanned']}  "
        f"documents scanned: {res['docs_scanned']}  "
        f"raw hits: {len(res['hits'])}  "
        f"now (America/Denver): {res.get('now_denver')}"
    )
    email_hits = [h for h in res["hits"] if h.get("city_enabled", True)]
    off_hits = [h for h in res["hits"] if not h.get("city_enabled", True)]
    items, past_examples = pick_for_email(email_hits, history, cfg["max_items_per_email"])
    off_upcoming = []  # merged per city (same text in two off cities = two report lines)
    for city in dict.fromkeys(h["city"] for h in off_hits):
        up, _ = pick_for_email([h for h in off_hits if h["city"] == city], history, 10 ** 6)
        off_upcoming.extend(up)
    summary = {
        "upcoming_alert_candidates": [_hit_brief(h) for h in items],
        "past_examples_not_alerted": [_hit_brief(h) for h in past_examples[:10]],
        "report_only_hits_in_cities_off_on_watch": [_hit_brief(h) for h in off_upcoming[:20]],
        "watch_email": cfg.get("_watch_email"),
        "cities_enabled": watch_enabled_cities(cfg),
        "cities_scanned": [c["city"] for c in res.get("cities", [])],
        "addresses": cfg.get("addresses") or [],
        "email_withheld_due_to_failures": bool(res["failures"] and items),
    }
    (STATE / f"match-summary-{stamp}.json").write_text(json.dumps(summary, indent=2))
    if past_examples:
        print(f"\nEXAMPLE ONLY — not alerted ({len(past_examples)} past match(es)):")
        for h in past_examples[:10]:
            print(
                f"  - [example only — not alerted] {h['city']} | {h['meeting_datetime']} | "
                f"{h['item_text'][:120]} | matched={','.join(h['matched_terms'])}"
            )
    if off_upcoming:
        print(f"\nREPORT ONLY — city off on watch, never emailed ({len(off_upcoming)} upcoming hit(s)):")
        for h in off_upcoming[:10]:
            print(f"  - [report only] {h['city']} | {h['meeting_datetime']} | {h['item_text'][:120]}")
    if res["failures"]:
        if items:
            print(f"FETCH FAILURES: {len(items)} upcoming hit(s) withheld; NO email prepared from partial data.")
        else:
            print("FETCH FAILURES: no email prepared.")
        return 3, None, [], past_examples
    if not items:
        print("No new UPCOMING hits. Silence (no email).")
        return 0, None, items, past_examples
    email = build_email(cfg, items)
    email["items"] = [
        {k: h[k] for k in (
            "key", "keys", "city", "meeting_body", "notice_title",
            "meeting_datetime", "item_text", "excerpt", "link", "notice_url",
        )}
        for h in items
    ]
    ob = OUTBOX / f"email-{stamp}.json"
    ob.write_text(json.dumps(email, indent=2))
    print(f"Owner-only email prepared: {ob}\nTo: {email['to']}\nSubject: {email['subject']}\n\n{email['body']}")
    return 4, ob, items, past_examples


def build_run_summary(cfg, res, history, code, items):
    """Machine-readable per-city report for the daily owner run."""
    now = dt.datetime.now(TZ)
    by_city = {}
    for h in res.get("hits", []):
        by_city.setdefault(h["city"], []).append(h)
    emailed_keys = {k for it in items for k in it.get("keys", [it["key"]])} if code == 4 else set()
    per_city = []
    upcoming_all = []  # merged per city so the same text in two cities stays two items
    for c in res.get("cities", []):
        merged = merge_hits(by_city.get(c["city"], []), history)
        up = [h for h in merged if h.get("upcoming")]
        upcoming_all.extend(up)
        per_city.append({
            "city": c["city"],
            "key": c.get("key"),
            "metro": c.get("metro"),
            "enabled_on_watch": c.get("enabled_on_watch"),
            "street": c.get("street") or "",
            "notices_checked": c.get("notices_checked", 0),
            "documents_checked": c.get("documents_checked", 0),
            "fetch": "ok" if c.get("fetch_ok") else "failed",
            "failures": c.get("failures", []),
            "skipped": c.get("skipped", []),
            "hits": len(merged),
            "new_upcoming_hits": len(up),
            "past_example_hits": len(merged) - len(up),
            "emailed": sum(1 for h in up if h["key"] in emailed_keys) if c.get("enabled_on_watch") else 0,
        })
    outcome = "failure" if (code == 2 or res.get("failures")) else ("hit" if code == 4 else "silence")
    return {
        "run_time": now.isoformat(timespec="seconds"),
        "run_time_tz": "America/Denver",
        "outcome": outcome,
        "exit_code": code,
        "watch_email": cfg.get("_watch_email"),
        "cities_enabled_on_watch": watch_enabled_cities(cfg),
        "failed_cities": [c["city"] for c in per_city if c["fetch"] == "failed"],
        "fatal": res.get("fatal"),
        "per_city": per_city,
        "totals": {
            "cities_scanned": len(per_city),
            "cities_fetch_ok": sum(1 for c in per_city if c["fetch"] == "ok"),
            "notices_checked": res.get("notices_scanned", 0),
            "documents_checked": res.get("docs_scanned", 0),
            "hits": sum(c["hits"] for c in per_city),
            "new_upcoming_hits": sum(c["new_upcoming_hits"] for c in per_city),
            "emailed_items": len(items) if code == 4 else 0,
        },
        "email_prepared": code == 4,
        "hit_items": [
            dict(_hit_brief(h), enabled_on_watch=h.get("city_enabled", True),
                 emailed=bool(code == 4 and h.get("city_enabled", True) and h["key"] in emailed_keys))
            for h in upcoming_all
        ],
    }


def print_run_summary(summary):
    print(f"\nPer-city summary — run {summary['run_time']} (America/Denver), outcome={summary['outcome']}")
    print(f"  {'City':20} {'Watch':5} {'Notices':>7} {'Docs':>5} {'Fetch':6} {'Hits':>4} {'NewUp':>5} {'Emailed':>7}")
    for c in summary["per_city"]:
        print(
            f"  {c['city']:20} {'on' if c['enabled_on_watch'] else 'off':5} {c['notices_checked']:>7} "
            f"{c['documents_checked']:>5} {c['fetch']:6} {c['hits']:>4} {c['new_upcoming_hits']:>5} {c['emailed']:>7}"
        )
    t = summary["totals"]
    print(
        f"  {'TOTAL':20} {'':5} {t['notices_checked']:>7} {t['documents_checked']:>5} "
        f"{str(t['cities_fetch_ok']) + '/' + str(t['cities_scanned']):6} {t['hits']:>4} "
        f"{t['new_upcoming_hits']:>5} {t['emailed_items']:>7}"
    )
    if summary["failed_cities"]:
        print("  FAILED: " + ", ".join(summary["failed_cities"]))


# ---------------------------------------------------------------- run
def load_json(p, default):
    return json.loads(Path(p).read_text()) if Path(p).exists() else default


def hit_key(city, link, item):
    norm = re.sub(r"\W+", " ", item.lower()).strip()
    return hashlib.sha1(f"{city}|{link}|{norm}".encode()).hexdigest()


def _fetch_notice(base, row, cache_fps, cache_docs, fstats, missing_state=None):
    """Worker (thread pool): detail page + attachment texts for one notice. No DB access here.
    Returns (n | None, [(doc_name, url, text)], [error strings], [skipped-PDF items]).
    A skipped-PDF item is a 4-tuple ("404", url, notice_url, message) or a plain string."""
    errs, skipped = [], []
    try:
        n = parse_notice(base, row["notice_url"])
    except FetchError as ex:
        return None, [], [str(ex)], []
    n["fingerprint"] = fp = notice_fingerprint(n)
    docs = []
    if n["agenda"].strip():
        docs.append(("PMN notice text", row["notice_url"], n["agenda"]))
    unchanged = bool(n.get("edited")) and cache_fps.get(row["notice_url"]) == fp
    if unchanged and all(u in cache_docs for _, u in n["pdfs"]):
        with fstats["lock"]:
            fstats["notices_unchanged"] += 1
            fstats["docs_reused"] += len(n["pdfs"])
            fstats["checked"] += [u for _, u in n["pdfs"]]
        return n, docs + [(f, u, cache_docs[u]["text"]) for f, u in n["pdfs"]], errs, []
    for fname, purl in n["pdfs"]:
        row_miss = (missing_state or {}).get("rows", {}).get(purl)
        if missing_state and aw_store.should_skip_permanent_pdf(row_miss, missing_state["now"]):
            continue
        try:
            text, saved, how = fetch_document(purl, row["notice_url"], cache_docs.get(purl))
        except PdfMissing as ex:
            skipped.append(("404", purl, row["notice_url"], str(ex)))
            continue
        except FetchError as ex:
            errs.append(str(ex))
            continue
        with fstats["lock"]:
            if how == "304":
                fstats["docs_304"] += 1
                fstats["checked"].append(purl)
            else:
                fstats["docs_fetched"] += 1
                fstats["bytes_downloaded"] += saved["length"]
                fstats["save"].append(saved)
        if missing_state:
            with missing_state["lock"]:
                if purl in missing_state["rows"]:
                    missing_state["recovered"].add(purl)
                    missing_state["rows"].pop(purl, None)
        docs.append((fname, purl, text))
    return n, docs, errs, skipped


def _city_sources(cfg, res):
    """City agendas pages (aw_city_sources): appended to res['_docs'] for per-user matching only.
    Their failures go to res['city_source_failures'] (run log), never res['failures']: a city page
    that cannot be fetched never withholds PMN alerts and never emails the owner."""
    try:
        import aw_city_sources
        aw_city_sources.run_into(cfg, res)
    except Exception as ex:  # never change the PMN run
        res["city_sources"] = {"error": repr(ex)[:300]}
        res["city_source_failures"] = [f"city sources: {ex!r}"[:300]]
    cs = res.get("city_sources") or {}
    if "pages" in cs:
        print(f"  city sources: pages={cs['pages']} ok={cs['pages_ok']} upcoming meetings={cs['upcoming_rows']} "
              f"docs={cs['docs']} unreadable={cs.get('docs_unreadable', 0)} added={len(cs.get('added') or [])}")
    for a in cs.get("added") or []:
        print(f"  city source added: {a['city_key']} {a['url']} (from {a['from']})")
    for f in res.get("city_source_failures") or []:
        print(f"  CITY SOURCE FAILURE (report only): {f}")


def run(cfg):
    """Fetch + match every city with enabled=True in cfg['cities'], inside the daily PMN request
    budget (aw_guard cap_pmn_requests). Every GET attempt counts. When the budget is reached the
    remaining fetches fail fast (no HTTP), the run reports a fetch failure, and per-user alerts
    are withheld for that run."""
    cap, used = None, 0
    try:
        with sqlite3.connect(DB) as con:
            cap = aw_guard.get(con, "cap_pmn_requests")
            used = aw_guard.usage(con).get("pmn_requests", 0)
    except sqlite3.Error as ex:
        print(f"WARNING: guard settings unavailable ({ex!r}); using default PMN budget", file=sys.stderr)
        cap = aw_guard.FIELDS["cap_pmn_requests"][0]
    BUDGET.update(cap=cap, used=used, start=used, tripped=False)
    res = None
    try:
        res = _run(cfg)
        if not res.get("fatal"):
            _city_sources(cfg, res)
        return res
    finally:
        made = BUDGET["used"] - BUDGET["start"]
        info = {"cap": cap, "requests_this_run": made, "used_today": BUDGET["used"], "tripped": BUDGET["tripped"]}
        if res is not None:
            res["budget"] = info
        try:
            with sqlite3.connect(DB) as con:
                aw_guard.add(con, "pmn_requests", made)
                if BUDGET["tripped"]:
                    aw_guard.trip(con, "cap_pmn_requests", f"{BUDGET['used']} of {cap} PMN requests used")
        except sqlite3.Error as ex:
            print(f"WARNING: guard usage not saved ({ex!r})", file=sys.stderr)
        if BUDGET["tripped"]:
            print(f"BUDGET: daily PMN request cap {cap} reached; fetch stopped for today", file=sys.stderr)
        BUDGET.update(cap=None)


def _run(cfg):
    """Fetch + match every city with enabled=True in cfg['cities'].
    Returns res with global counts plus res['cities'] = per-city stats."""
    cfg = aw_cities.expand_config(cfg)
    base = cfg["pmn_base"]
    now = dt.datetime.now(TZ)
    lo = now - dt.timedelta(days=cfg["lookback_days"])
    hi = now + dt.timedelta(days=cfg["lookahead_days"])
    keyword_matchers = build_keyword_matchers(cfg)
    failures, skipped_pdfs, docs_scanned, notices_scanned = [], [], 0, 0
    all_hits = []
    notices_seen = []  # one per notice scanned -> aw_store.notices (admin archive)
    docs_seen = []     # one per notice: its document texts, for per-user alert matching (in memory only)
    # Stored notice fingerprints + document texts (skip unchanged notices / conditional GET attachments)
    cache_fps, cache_docs = {}, {}
    missing_pdfs = {}
    try:
        DB.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(DB) as con:
            aw_store.ensure_schema(con)
            cache_fps, cache_docs = aw_store.load_cache(con)
            missing_pdfs = aw_store.load_missing_pmn_pdfs(con)
    except sqlite3.Error as ex:
        print(f"WARNING: document cache unavailable ({ex!r}); fetching everything", file=sys.stderr)
    missing_state = {"lock": threading.Lock(), "rows": missing_pdfs, "recovered": set(), "now": now}
    fstats = {"lock": threading.Lock(), "notices_unchanged": 0, "docs_reused": 0, "docs_304": 0,
              "docs_fetched": 0, "bytes_downloaded": 0, "save": [], "checked": []}
    scan = [(city, ccfg) for city, ccfg in cfg["cities"].items() if ccfg.get("enabled", True)]

    def city_stats(city, ccfg):
        return {
            "city": city, "key": ccfg.get("key"), "metro": ccfg.get("metro"),
            "enabled_on_watch": bool(ccfg.get("watch_enabled", True)),
            "street": (cfg.get("city_streets") or {}).get(city) or ccfg.get("street") or "",
            "notices_checked": 0, "documents_checked": 0, "hits": 0,
            "fetch_ok": True, "failures": [], "skipped": [],
        }

    stats = [city_stats(city, ccfg) for city, ccfg in scan]

    def fail(st, msg):
        failures.append(msg)
        st["failures"].append(msg)
        st["fetch_ok"] = False

    def note_skipped_pdf(st, msg):
        skipped_pdfs.append(msg)
        st.setdefault("skipped", []).append(msg)
        print(msg, file=sys.stderr)

    ent_url = f"{base}/pmn/list/entities.html?id={cfg['government_type_id_municipality']}&limit=10"
    try:
        entities = parse_id_list(get(ent_url), "entities", ent_url)
    except FetchError as ex:
        for st in stats:
            st["fetch_ok"] = False
            st["failures"].append(f"not scanned: PMN municipality list failed: {ex}")
        return {"fatal": str(ex), "failures": [str(ex)], "hits": [], "cities": stats,
                "notices_scanned": 0, "docs_scanned": 0, "now_denver": now.isoformat()}

    for (city, ccfg), st in zip(scan, stats):
        city_enabled = st["enabled_on_watch"]
        # Street hits ONLY in this city's agendas; keywords stay global.
        matchers = list(keyword_matchers) + build_street_matchers(st["street"])
        eid = entities.get(ccfg["pmn_entity_name"])
        if not eid:
            fail(st, f"{city}: entity '{ccfg['pmn_entity_name']}' not found on PMN municipality list")
            continue
        pb_url = f"{base}/pmn/list/publicBodies.html?id={eid}&limit=10"
        try:
            bodies = parse_id_list(get(pb_url), "publicBodies", pb_url)
        except FetchError as ex:
            fail(st, f"{city}: {ex}")
            continue
        for bname in ccfg["public_bodies"]:
            bid = bodies.get(bname)
            if not bid:
                fail(st, f"{city}: public body '{bname}' not found on PMN (renamed?)")
                continue
            try:
                rows, list_url = list_notices(base, bid)
            except FetchError as ex:
                fail(st, f"{city} / {bname}: {ex}")
                continue
            todo = [row for row in rows if lo <= row["event"] <= hi]
            with ThreadPoolExecutor(max_workers=FETCH_WORKERS) as pool:
                fetched = list(pool.map(
                    lambda r: _fetch_notice(base, r, cache_fps, cache_docs, fstats, missing_state), todo))
            for row, (n, docs, errs, skipped) in zip(todo, fetched):
                notices_scanned += 1
                st["notices_checked"] += 1
                for e in errs:
                    fail(st, f"{city} / {bname}: {e}")
                for s in skipped:
                    if isinstance(s, tuple) and s and s[0] == "404":
                        _kind, purl, nurl, _msg = s
                        with missing_state["lock"]:
                            out = aw_store.record_pdf_404(missing_state["rows"], purl, nurl, now)
                        if out:
                            note_skipped_pdf(st, out)
                    else:
                        note_skipped_pdf(st, s)
                if n is None:
                    continue
                seen_items = set()
                hits_before = len(all_hits)
                for dname, durl, text in docs:
                    docs_scanned += 1
                    st["documents_checked"] += 1
                    for h in find_hits(text, matchers, doc_name=dname):
                        k = (h["item_text"].lower(), h["matched"])
                        if k in seen_items:
                            continue
                        seen_items.add(k)
                        upcoming = row["event"] >= now
                        h.update({
                            "city": city,
                            "city_key": ccfg.get("key"),
                            "city_enabled": city_enabled,
                            "meeting_body": n["body"] or bname,
                            "notice_title": n["title"],
                            "meeting_datetime": n["start"],
                            "event_iso": row["event"].isoformat(),
                            "upcoming": upcoming,
                            "document": dname,
                            "link": durl,
                            "notice_url": row["notice_url"],
                        })
                        h["key"] = hit_key(city, durl, h["item_text"])
                        all_hits.append(h)
                        st["hits"] += 1
                docs_seen.append({
                    "city": city, "city_key": ccfg.get("key"), "notice_url": row["notice_url"],
                    "meeting_body": n["body"] or bname, "notice_title": n["title"],
                    "meeting_datetime": n["start"], "event_iso": row["event"].isoformat(), "docs": docs,
                })
                notices_seen.append({
                    "notice_url": row["notice_url"], "city_key": ccfg.get("key"), "metro": ccfg.get("metro"),
                    "meeting_date": row["event"].isoformat(), "body": n["body"] or bname, "title": n["title"],
                    "pdf_urls": [u for _, u in n["pdfs"]],
                    "matched_terms": [h["matched"] for h in all_hits[hits_before:]],
                    "text": "\n\n".join(t for _, _, t in docs)[:aw_store.TEXT_LIMIT],
                    "fingerprint": n["fingerprint"],
                })
    try:
        with sqlite3.connect(DB) as con:
            aw_store.save_documents(con, fstats["save"], fstats["checked"])
            aw_store.save_missing_pmn_pdfs(con, missing_state["rows"], missing_state["recovered"])
    except sqlite3.Error as ex:
        print(f"WARNING: documents not saved ({ex!r})", file=sys.stderr)
    fetch_stats = {k: v for k, v in fstats.items() if k not in ("lock", "save", "checked")}
    return {
        "fetch_stats": fetch_stats,
        "failures": failures,
        "skipped_pdfs": skipped_pdfs,
        "permanent_missing_pdfs": sorted(
            u for u, r in missing_state["rows"].items() if (r.get("status") or "") == "permanent"),
        "hits": all_hits,
        "notices": notices_seen,
        "_docs": docs_seen,
        "cities": stats,
        "notices_scanned": notices_scanned,
        "docs_scanned": docs_scanned,
        "window": [lo.isoformat(), hi.isoformat()],
        "now_denver": now.isoformat(),
    }


def _words(t):
    return set(re.findall(r"[a-z0-9]{3,}", t.lower()))


def _overlap(a, b):
    return len(a & b) / max(1, min(len(a), len(b)))


def merge_hits(hits, history):
    """Merge fresh hits by key; prefer PDF links; return ranked list."""
    sent = {h["key"] for h in history}
    fresh = [h for h in hits if h["key"] not in sent]
    merged = {}
    for h in fresh:
        g = merged.setdefault(h["key"], dict(h, matched_terms=[], score=0, keys=[h["key"]]))
        if h["matched"] not in g["matched_terms"]:
            g["matched_terms"].append(h["matched"])
            g["score"] += h["weight"]
    ranked = sorted(
        merged.values(),
        key=lambda h: (h["score"], h["upcoming"], h["event_iso"]),
        reverse=True,
    )
    out = []
    for h in ranked:
        w = _words(h["item_text"])
        dup = None
        for o in out:
            same_notice = o["notice_url"] == h["notice_url"]
            ov = _overlap(w, _words(o["item_text"]))
            if (same_notice and ov >= 0.6) or ov >= 0.8:
                dup = o
                break
        if dup is not None:
            dup["keys"].append(h["key"])
            if (
                dup["notice_url"] == h["notice_url"]
                and "/pmn/files/" in h["link"]
                and "/pmn/files/" not in dup["link"]
            ):
                dup["link"] = h["link"]
            for t in h["matched_terms"]:
                if t not in dup["matched_terms"]:
                    dup["matched_terms"].append(t)
            continue
        out.append(h)
    return out


def pick_for_email(hits, history, n):
    """Alerts: upcoming only. Past matches are never emailed."""
    merged = merge_hits(hits, history)
    upcoming = [h for h in merged if h.get("upcoming")]
    past_examples = [h for h in merged if not h.get("upcoming")]
    return upcoming[:n], past_examples


def build_email(cfg, items):
    top = items[0]
    meeting_date = (
        top["meeting_datetime"].rsplit(" ", 2)[0]
        if top["meeting_datetime"]
        else top["event_iso"][:10]
    )
    subject = f"Agenda Watch hit: {top['city']} {meeting_date}"
    parts = [
        f"Agenda Watch found {len(items)} matching upcoming item(s) in public agendas posted on Utah Public Notice.",
        "",
    ]
    for i, h in enumerate(items, 1):
        parts += [
            f"{i}. {h['city']} - {h['meeting_body']}",
            f"   Meeting: {h['notice_title']}",
            f"   Date/time: {h['meeting_datetime']}",
            f"   Matched: {', '.join(h['matched_terms'])}",
            f"   Agenda item: {h['item_text']}",
            f"   Excerpt: \"{h['excerpt']}\"",
            f"   Source: {h['link']}",
            f"   PMN notice: {h['notice_url']}",
            "",
        ]
    parts += ["--", FOOTER]
    return {"to": cfg["owner_email"], "subject": subject, "body": "\n".join(parts)}


def log_run(started, code, res=None, summary=None, errors=""):
    """Write the fetch_runs row (+ notices, + retention purge) for one run-owner. Never raises:
    logging must not change the run's exit code or email behaviour."""
    try:
        res = res or {}
        per_city = {c["key"]: {"city": c["city"], "metro": c.get("metro"), "notices": c.get("notices_checked", 0),
                               "documents": c.get("documents_checked", 0), "hits": c.get("hits", 0),
                               "fetch": c.get("fetch"), "failures": c.get("failures", [])}
                    for c in (summary or {}).get("per_city", [])}
        errs = "\n".join([errors] if errors else []) or "\n".join(res.get("failures") or [])
        skipped = res.get("skipped_pdfs") or []
        if skipped:
            errs = "\n".join([e for e in [errs] if e] + skipped)
        city = [f if f.startswith("city source") else f"city source: {f}" for f in res.get("city_source_failures") or []]
        city += [f"city source added: {a['city_key']} {a['url']}" for a in (res.get("city_sources") or {}).get("added") or []]
        if city:
            errs = "\n".join([e for e in [errs] if e] + city)
        DB.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(DB) as con:
            aw_store.ensure_schema(con)
            aw_store.upsert_notices(con, res.get("notices") or [])
            purged = aw_store.purge(con)
            rid = aw_store.record_run(con, started=started, code=code,
                                      total_notices=res.get("notices_scanned", 0), per_city=per_city,
                                      errors=errs, purged=purged)
        print(f"  run log: fetch_runs id={rid} purged notices={purged[0]} runs={purged[1]}")
    except Exception as ex:  # pragma: no cover - defensive
        print(f"WARNING: run log not written: {ex!r}", file=sys.stderr)


# ---------------------------------------------------------------- per-user alerts
USER_PLAN = "user_alert_plan.json"


def users_with_alerts(cfg):
    """Non-owner watches with alerts on and at least one city enabled (emails)."""
    owner = ((cfg or {}).get("owner_email") or aw_cities.owner_email() or "").strip().lower()
    if not DB.exists():
        return []
    try:
        with sqlite3.connect(DB) as con:
            ws = aw_alerts.load_watches(con)
    except sqlite3.Error:
        return []
    return [w["email"] for w in ws if w["email"].lower() != owner and w["alerts_on"]
            and any(c["enabled"] for c in w["cities"].values())]


def _alert_seed_done(con):
    try:
        return bool(con.execute("SELECT 1 FROM schema_migrations WHERE id='alert_seed_v1'").fetchone())
    except sqlite3.Error:
        return False


def write_user_alert_plan(cfg, res, started):
    """After the scan: state/user_alert_plan.json for the web app to send (one email per user).
    Fetch failure -> withheld (nobody is emailed). Before the one-time dedupe seed has run, every
    current hit is recorded as 'seeded' instead (never a flood of old hits on rollout)."""
    out = {"run_started": started.isoformat(timespec="seconds"), "mode": "send", "users": [], "held_cities_off": {}}
    try:
        if res.get("fatal") or res.get("failures"):
            out["mode"] = "withheld"
            out["reason"] = "fetch failures: no user emails from a partial scan"
        else:
            now = dt.datetime.now(TZ)
            with sqlite3.connect(DB) as con:
                aw_alerts.ensure_schema(con)
                if not _alert_seed_done(con):
                    out["mode"] = "seeded"
                    out["seeded"] = aw_alerts.seed(con, res.get("_docs") or [], now)
                    con.execute("CREATE TABLE IF NOT EXISTS schema_migrations(id TEXT PRIMARY KEY, applied_at TEXT NOT NULL)")
                    con.execute("INSERT OR IGNORE INTO schema_migrations(id, applied_at) VALUES('alert_seed_v1', ?)",
                                (now.isoformat(timespec="seconds"),))
                else:
                    out.update(aw_alerts.plan(con, res.get("_docs") or [], cfg, now, cfg.get("max_items_per_email", 5),
                                                    link_only=res.get("city_unreadable") or []))
    except Exception as ex:  # never change the run's exit code
        out = {"run_started": started.isoformat(timespec="seconds"), "mode": "error", "error": repr(ex)[:300], "users": []}
    (STATE / USER_PLAN).write_text(json.dumps(out, indent=2, default=str))
    n = sum(len(u["items"]) for u in out.get("users", []))
    print(f"  per-user alerts: mode={out['mode']} users={len(out.get('users', []))} items={n}"
          + (f" held(cities off)={out['held_cities_off']}" if out.get("held_cities_off") else ""))
    return out


def main():
    cfg = aw_cities.expand_config(load_json(ROOT / "config.json", None))
    cmd = sys.argv[1] if len(sys.argv) > 1 else "run"
    STATE.mkdir(exist_ok=True)
    OUTBOX.mkdir(exist_ok=True)
    history = load_json(HISTORY, [])
    if cmd == "run":
        res = run(cfg)
        code, _, _, _ = write_run_artifacts(cfg, res, history)
        sys.exit(code)
    elif cmd == "run-owner":
        started = dt.datetime.now(TZ)
        watch = load_owner_watch(cfg)
        others = users_with_alerts(cfg)   # per-user alerts: any non-owner watch with alerts on + a city
        if watch is None and not others:
            print("SOFT_ERROR: no watch row for owner in data/agenda_watch.db", file=sys.stderr)
            log_run(started, 1, errors="no watch row for owner")
            sys.exit(1)
        if watch is None:
            watch = {"email": cfg.get("owner_email"), "alerts_on": 0, "keywords": None, "cities": {}}
        email = watch.get("email") or cfg.get("owner_email")
        state = watch_city_state(watch)
        names = aw_cities.key_to_name(cfg)
        on = [k for k in names if state.get(k, {}).get("enabled")]
        print(f"Owner watch: {email}")
        print(
            "  cities on: " + (", ".join(
                f"{names[k]}" + (f" [street {state[k]['street']!r}]" if state[k]["street"] else "")
                for k in on) or "(none)")
            + f"  alerts_on={bool(watch.get('alerts_on'))}"
        )
        if others:
            print(f"  other users with alerts on: {len(others)}")
        if not watch.get("alerts_on") and not others:
            print("ALERTS_OFF")
            log_run(started, 0, errors="ALERTS_OFF (no scan)")
            sys.exit(0)
        if not on and not others:
            print("SOFT_ERROR: alerts on but no city enabled", file=sys.stderr)
            log_run(started, 1, errors="alerts on but no city enabled")
            sys.exit(1)
        # Daily run scans EVERY supported city; only watch-enabled cities can be emailed.
        run_cfg = overlay_cfg_from_watch(cfg, watch, scan_all=True)
        # Do not permanently change config.json; overlay is in-memory only.
        print(
            f"  overlay: cities={watch_enabled_cities(run_cfg)} keywords={len(run_cfg['keywords'])} "
            f"city_streets={ {c: s for c, s in run_cfg['city_streets'].items() if s} }"
        )
        print(f"  scanning all {len(run_cfg['cities'])} supported cities (off cities: report only, never emailed)")
        res = run(run_cfg)
        code, _, items, _ = write_run_artifacts(run_cfg, res, history)
        summary = build_run_summary(run_cfg, res, history, code, items)
        if res.get("city_sources") is not None:
            summary["city_sources"] = res["city_sources"]
            summary["city_source_failures"] = res.get("city_source_failures") or []
        (STATE / SUMMARY_FILE).write_text(json.dumps(summary, indent=2, default=str))
        print_run_summary(summary)
        print(f"  summary JSON: state/{SUMMARY_FILE}")
        log_run(started, code, res=res, summary=summary)
        write_user_alert_plan(cfg, res, started)
        sys.exit(code)
    elif cmd == "mark-sent":
        email = load_json(sys.argv[2], None)
        msg_id = sys.argv[3] if len(sys.argv) > 3 else ""
        today = dt.datetime.now(TZ).date().isoformat()
        for it in email["items"]:
            history.append({
                "date": today, "city": it["city"], "item": it["item_text"], "link": it["link"],
                "meeting": it["meeting_datetime"], "key": it["key"], "gmail_message_id": msg_id,
            })
            for k in it.get("keys", []):
                if k != it["key"]:
                    history.append({
                        "date": today, "city": it["city"], "item": it["item_text"], "link": it["link"],
                        "meeting": it["meeting_datetime"], "key": k, "gmail_message_id": msg_id,
                        "duplicate_of": it["key"],
                    })
        HISTORY.write_text(json.dumps(history, indent=2))
        print(f"recorded {len(email['items'])} item(s) in {HISTORY}")
    else:
        print(__doc__)
        sys.exit(1)


if __name__ == "__main__":
    main()
