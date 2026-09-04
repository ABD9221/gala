"""opening_hours parsing, including the Gulf-specific cases."""
from datetime import datetime

import pytest

from gala.hours import is_open, next_change, parse, weekly_schedule

# 2026-09-04 is a Friday, 09-05 a Saturday, 09-06 a Sunday.
FRIDAY = datetime(2026, 9, 4, 10, 0)
SATURDAY = datetime(2026, 9, 5, 10, 0)


def test_24_7():
    assert is_open("24/7", datetime(2026, 9, 4, 3, 0)) is True


@pytest.mark.parametrize("spec,when,expected", [
    # The Saudi business week wraps the end of the day array: Sa-Th must
    # exclude Friday and include Saturday.
    ("Sa-Th 08:00-23:00", FRIDAY, False),
    ("Sa-Th 08:00-23:00", SATURDAY, True),
    ("Su-Th 09:00-16:00", FRIDAY, False),
    # Overnight spans belong to the previous day.
    ("Mo-Su 18:00-02:00", datetime(2026, 9, 4, 1, 0), True),
    ("Mo-Su 18:00-02:00", datetime(2026, 9, 4, 15, 0), False),
    ("Mo-Su 18:00-02:00", datetime(2026, 9, 4, 23, 30), True),
    # Split shifts.
    ("Mo-Fr 09:00-12:00,16:00-21:00", datetime(2026, 9, 4, 13, 0), False),
    ("Mo-Fr 09:00-12:00,16:00-21:00", datetime(2026, 9, 4, 17, 0), True),
    # An explicit closure overrides an earlier rule.
    ("Mo-Fr 09:00-17:00; Sa off", SATURDAY, False),
    # Single-digit hours appear in real OSM data.
    ("Mo-Th,Su 8:00-17:00", datetime(2026, 9, 6, 9, 0), True),
])
def test_real_world_specs(spec, when, expected):
    assert is_open(spec, when) is expected


@pytest.mark.parametrize("spec", ["sunset-22:00", "12pm-1am", "Mar-Oct 09:00-18:00", None, ""])
def test_unsupported_reports_unknown_not_closed(spec):
    """A wrong "closed" sends someone to a locked door; None is the honest answer."""
    assert parse(spec) is None
    assert is_open(spec, FRIDAY) is None


def test_boundaries_are_half_open():
    assert is_open("Mo-Su 09:00-17:00", datetime(2026, 9, 4, 9, 0)) is True
    assert is_open("Mo-Su 09:00-17:00", datetime(2026, 9, 4, 17, 0)) is False


def test_next_change_finds_the_opening():
    assert next_change("Sa-Th 08:00-23:00", FRIDAY) == datetime(2026, 9, 5, 8, 0)


def test_weekly_schedule_marks_the_saudi_weekend():
    rows = weekly_schedule("Sa-Th 08:00-23:00")
    friday = next(r for r in rows if r["day_en"] == "Fr")
    assert friday["closed"] is True
    assert friday["day_ar"] == "الجمعة"
    assert sum(not r["closed"] for r in rows) == 6
