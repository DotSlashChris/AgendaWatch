"""Supported-city registry for Agenda Watch (shared by the matcher/fetcher and the web app).

config.json holds the hierarchy  states -> <state> -> metros -> <metro> -> cities -> <key>.
Each city has a stable short key (ch, slc, sandy, ...) used in the DB (watch_cities.city_key)
and in form field names (city_<key>, street_<key>).

flatten_cities(cfg) returns an ordered dict  city_name -> {key, name, state, metro,
pmn_entity_name, public_bodies, enabled}  which is the shape the fetcher has always used
as cfg["cities"].
"""
import json
import os
import re
from collections import OrderedDict
from pathlib import Path

ROOT = Path(__file__).resolve().parent
MAX_CITIES = 10
LEGACY_KEYS = {"Cottonwood Heights": "ch", "Salt Lake City": "slc"}



def owner_email() -> str:
    """Owner watch identity from secret AW_OWNER_EMAIL. Empty if the secret is missing."""
    return (os.environ.get("AW_OWNER_EMAIL") or "").strip().lower()


def admin_emails() -> list:
    """Admin accounts from secret AW_ADMIN_EMAILS (comma-separated). Empty if missing — fail closed."""
    return [e.strip().lower() for e in (os.environ.get("AW_ADMIN_EMAILS") or "").split(",") if e.strip()]


def flatten_cities(cfg):
    out = OrderedDict()
    for state, sd in (cfg.get("states") or {}).items():
        for metro, md in (sd.get("metros") or {}).items():
            for key, c in (md.get("cities") or {}).items():
                out[c["name"]] = {
                    "key": key,
                    "name": c["name"],
                    "state": state,
                    "metro": metro,
                    "pmn_entity_name": c.get("pmn_entity_name", c["name"]),
                    "public_bodies": list(c.get("public_bodies") or []),
                    "enabled": bool(c.get("enabled", True)),
                }
    return out


def expand_config(cfg):
    """Return cfg with cfg['cities'] (name -> city dict) derived from the state/metro tree.
    A cfg that already has a flat 'cities' map (legacy / tests) is left as is."""
    if "cities" not in cfg and "states" in cfg:
        cfg = dict(cfg)
        cfg["cities"] = flatten_cities(cfg)
    for name, c in cfg.get("cities", {}).items():
        c.setdefault("name", name)
        c.setdefault("key", LEGACY_KEYS.get(name, name.lower().replace(" ", "")))
    return cfg


def load_config(path=None):
    p = Path(path) if path else ROOT / "config.json"
    cfg = expand_config(json.loads(p.read_text()))
    # Identity lives in env secrets, not in the committed config.
    cfg["owner_email"] = owner_email()
    cfg["admin_emails"] = admin_emails()
    return cfg


def city_list(cfg):
    """[{key, name, state, metro}] in display order."""
    return [
        {k: c[k] for k in ("key", "name", "state", "metro")}
        for c in expand_config(cfg)["cities"].values()
    ]


def city_slug(name):
    """Public page slug from the city name: 'St. George' -> 'st-george', 'Park City' -> 'park-city'."""
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


def city_pages(cfg):
    """[{slug, name, key}] for every catalog city, in display order (public /cities/<slug> pages)."""
    return [{"slug": city_slug(c["name"]), "name": c["name"], "key": c["key"]} for c in city_list(cfg)]


def sitemap_xml(cfg, site="https://agendawatch.app"):
    """sitemap.xml: the home page plus every catalog city page (served by the app and, generated, by the edge)."""
    urls = [site + "/"] + [f"{site}/cities/{p['slug']}" for p in city_pages(cfg)]
    return ('<?xml version="1.0" encoding="UTF-8"?>\n<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
            + "".join(f"  <url><loc>{u}</loc></url>\n" for u in urls) + "</urlset>\n")


def metro_groups(cfg):
    """[(metro, [city, ...]), ...] in display order."""
    groups = OrderedDict()
    for c in city_list(cfg):
        groups.setdefault(c["metro"], []).append(c)
    return list(groups.items())


def key_to_name(cfg):
    return {c["key"]: c["name"] for c in city_list(cfg)}


def name_to_key(cfg):
    return {c["name"]: c["key"] for c in city_list(cfg)}


def legacy_watch_cities(watch):
    """Per-city state from the pre-watch_cities columns (city_ch/city_slc/street_ch/street_slc,
    and before that address -> CH street, address2 -> SLC street)."""
    street_ch = (watch.get("street_ch") or "").strip()
    street_slc = (watch.get("street_slc") or "").strip()
    if not street_ch and not street_slc:
        street_ch = (watch.get("address") or "").strip()
        street_slc = (watch.get("address2") or "").strip()
    return {
        "ch": {"enabled": bool(watch.get("city_ch")), "street": street_ch},
        "slc": {"enabled": bool(watch.get("city_slc")), "street": street_slc},
    }


def area_name(metro):
    """Short public area name for a metro group: 'Ogden-Clearfield metro' -> 'Ogden'."""
    m = metro[: -len(" metro")] if metro.endswith(" metro") else metro
    return m.split("-")[0].strip()


def area_phrase(cfg):
    """'Salt Lake, Ogden, Provo and Heber' from the catalog's metro groups, in display order."""
    names = [area_name(m) for m, _ in metro_groups(cfg)]
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]
