"""Nov 13 2026: Sandy City Historic Commission removed from the PMN source list."""
from pathlib import Path

import aw_cities

ROOT = Path(__file__).resolve().parents[1]
GONE = "Sandy City Historic Commission"
SANDY_BODIES = [
    "City Council",
    "Planning Commission",
    "Board of Adjustments",
    "Community Development Department Administrative Hearing",
    "Architectural Review Committee",
]


def test_sandy_historic_commission_removed_other_sandy_bodies_kept():
    cfg = aw_cities.load_config(ROOT / "config.json")
    sandy = next(c for c in cfg["cities"].values() if c["key"] == "sandy")
    assert sandy["name"] == "Sandy" and sandy["pmn_entity_name"] == "Sandy"
    assert sandy["public_bodies"] == SANDY_BODIES
    assert GONE not in sandy["public_bodies"]
    # no other city still lists that body
    assert GONE not in [b for c in cfg["cities"].values() for b in c["public_bodies"]]
