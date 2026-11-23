"""Tests for the offline re-check helper's pre-run history rewind (tests/offline_rescan.py only)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import offline_rescan as o  # noqa: E402

START = "2026-10-23T08:21:54-06:00"
OLD = {"date": "2026-10-16", "key": "old1", "city": "Cottonwood Heights"}
A = {"date": "2026-10-23", "key": "newA", "city": "Cottonwood Heights"}
B = {"date": "2026-10-23", "key": "newB", "city": "Cottonwood Heights", "duplicate_of": "newA"}
OTHER_TODAY = {"date": "2026-10-23", "key": "zzz", "city": "Sandy"}


def test_entries_added_by_this_run_are_excluded_earlier_days_kept():
    keep, out = o.pre_run_history([OLD, A, B, OTHER_TODAY], {"newA", "newB"}, START)
    assert [e["key"] for e in keep] == ["old1", "zzz"]
    assert [e["key"] for e in out] == ["newA", "newB"]


def test_duplicate_of_run_key_is_excluded():
    keep, out = o.pre_run_history([B], {"newA"}, START)
    assert out == [B] and keep == []


def test_real_timestamp_wins_over_date_rule():
    before = dict(A, created_at="2026-10-23T07:00:00-06:00")
    after = dict(OTHER_TODAY, created_at="2026-10-23T08:23:32-06:00")
    keep, out = o.pre_run_history([before, after], {"newA"}, START)
    assert keep == [before] and out == [after]


def test_nothing_excluded_when_run_has_no_matching_keys():
    keep, out = o.pre_run_history([OLD, A], set(), START)
    assert keep == [OLD, A] and out == []
