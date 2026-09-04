"""Parser for the OSM ``opening_hours`` grammar.

Google Places hands you opening hours pre-digested (``periods`` plus a boolean
``open_now``). OSM stores them as a small domain-specific language, so to reach
feature parity we have to parse it ourselves.

The full grammar is large (holidays, sunset offsets, week-of-month selectors,
month ranges). This implements the subset that covers the overwhelming majority
of real POI values, and -- importantly -- reports ``None`` rather than guessing
when it meets something outside that subset. A wrong "open now" is worse than an
absent one: it sends someone across town to a locked door.

Two details that matter in the Gulf specifically:

* **Overnight spans.** ``18:00-02:00`` is extremely common for cafes and
  restaurants here. The closing time belongs to the *next* day, so a naive
  ``start <= now <= end`` comparison reports such places as closed all evening.
* **The weekend is not Sa-Su.** Saudi business weeks run Sunday-Thursday, and
  OSM values like ``Sa-Th`` wrap around the end of the week array. Day ranges
  are therefore resolved modulo 7 instead of as a plain slice.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

DAYS = ["Mo", "Tu", "We", "Th", "Fr", "Sa", "Su"]
DAY_INDEX = {d.lower(): i for i, d in enumerate(DAYS)}
# Arabic day names, for rendering back to an Arabic-language UI.
DAYS_AR = ["الاثنين", "الثلاثاء", "الأربعاء", "الخميس", "الجمعة", "السبت", "الأحد"]

_TIME = r"\d{1,2}:\d{2}"
_TIME_RANGE = re.compile(rf"({_TIME})\s*-\s*({_TIME})")
_DAY_RANGE = re.compile(r"(mo|tu|we|th|fr|sa|su)\s*-\s*(mo|tu|we|th|fr|sa|su)", re.I)
_DAY_SINGLE = re.compile(r"\b(mo|tu|we|th|fr|sa|su)\b", re.I)
# Selectors we knowingly do not model. Their presence makes the value
# unparseable rather than merely unusual, so we bail out instead of guessing.
_UNSUPPORTED = re.compile(r"(sunrise|sunset|dawn|dusk|week\s|\[|\bJan\b|\bFeb\b|\bMar\b|\bApr\b|\bMay\b|\bJun\b|\bJul\b|\bAug\b|\bSep\b|\bOct\b|\bNov\b|\bDec\b)", re.I)


@dataclass(frozen=True)
class Interval:
    """A span of minutes within one day. ``end`` may exceed 1440 when it wraps."""

    day: int      # 0 = Monday
    start: int    # minutes from midnight
    end: int      # minutes from midnight; > 1440 means it runs past midnight

    @property
    def wraps(self) -> bool:
        return self.end > 24 * 60


def _to_minutes(hhmm: str) -> int:
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


def _expand_days(spec: str) -> list[int]:
    """Resolve the day selector of one rule to a list of weekday indices."""
    days: set[int] = set()
    consumed = spec
    for m in _DAY_RANGE.finditer(spec):
        a, b = DAY_INDEX[m.group(1).lower()], DAY_INDEX[m.group(2).lower()]
        # Modulo walk so Sa-Th (5 -> 3) wraps through Sunday instead of being empty.
        i = a
        while True:
            days.add(i)
            if i == b:
                break
            i = (i + 1) % 7
        consumed = consumed.replace(m.group(0), " ")
    for m in _DAY_SINGLE.finditer(consumed):
        days.add(DAY_INDEX[m.group(1).lower()])
    return sorted(days)


def parse(spec: str | None) -> list[Interval] | None:
    """Parse an ``opening_hours`` value, or return None if unsupported.

    None means "we do not know", and callers must render it as unknown rather
    than as closed.
    """
    if not spec or not spec.strip():
        return None
    text = spec.strip()
    if text in {"24/7", "Mo-Su 00:00-24:00", "00:00-24:00"}:
        return [Interval(d, 0, 24 * 60) for d in range(7)]
    if _UNSUPPORTED.search(text):
        return None

    intervals: list[Interval] = []
    for rule in text.split(";"):
        rule = rule.strip()
        if not rule:
            continue
        if re.search(r"\b(ph|sh)\b", rule, re.I):
            continue  # public/school holidays: out of scope, ignore the rule
        days = _expand_days(rule) or list(range(7))
        if re.search(r"\b(off|closed)\b", rule, re.I):
            # An explicit closure overrides anything an earlier rule opened.
            intervals = [iv for iv in intervals if iv.day not in days]
            continue
        ranges = _TIME_RANGE.findall(rule)
        if not ranges:
            return None
        for start_s, end_s in ranges:
            start, end = _to_minutes(start_s), _to_minutes(end_s)
            if end <= start:
                end += 24 * 60  # spans midnight
            for d in days:
                intervals.append(Interval(d, start, end))
    return intervals or None


def is_open(spec: str | None, when: datetime) -> bool | None:
    """True/False if known, None if the value could not be parsed."""
    intervals = parse(spec)
    if intervals is None:
        return None
    minute = when.hour * 60 + when.minute
    today, yesterday = when.weekday(), (when.weekday() - 1) % 7
    for iv in intervals:
        if iv.day == today and iv.start <= minute < min(iv.end, 24 * 60):
            return True
        # A span that started yesterday and runs past midnight still covers us.
        if iv.wraps and iv.day == yesterday and minute < iv.end - 24 * 60:
            return True
    return False


def next_change(spec: str | None, when: datetime) -> datetime | None:
    """When the open/closed state next flips, searching up to a week ahead."""
    intervals = parse(spec)
    if not intervals:
        return None
    state = is_open(spec, when)
    probe = when.replace(second=0, microsecond=0)
    for _ in range(7 * 24 * 60 // 5):
        probe += timedelta(minutes=5)
        if is_open(spec, probe) != state:
            return probe
    return None


def weekly_schedule(spec: str | None) -> list[dict[str, object]] | None:
    """Render the week as display rows, Monday first, with Arabic day labels."""
    intervals = parse(spec)
    if intervals is None:
        return None
    out: list[dict[str, object]] = []
    for d in range(7):
        spans = sorted((iv.start, iv.end) for iv in intervals if iv.day == d)
        out.append(
            {
                "day": d,
                "day_en": DAYS[d],
                "day_ar": DAYS_AR[d],
                "periods": [
                    {"open": _fmt(s), "close": _fmt(e % (24 * 60) if e != 24 * 60 else 1440)}
                    for s, e in spans
                ],
                "closed": not spans,
            }
        )
    return out


def _fmt(minutes: int) -> str:
    if minutes >= 24 * 60:
        return "24:00"
    return f"{minutes // 60:02d}:{minutes % 60:02d}"
