"""Posted opening hours, Monday first. Set SHOP_HOURS to override, e.g.
SHOP_HOURS="8-18,8-18,8-18,8-18,8-19,9-16,closed".

Each day is `open-close` on a 24-hour clock with optional minutes ("7:30-15"), or "closed". Owners can
type it the way they write it on the door ("7am-3pm", "07:30-15:00", "9 to 17"); `normalize_range`
turns that into the stored form or says, in plain words, what's wrong. Closing at 24 means open to the
end of the day, inclusive. Overnight hours (closing after midnight) aren't supported.
"""

from __future__ import annotations

import re
from datetime import date, time

DEFAULT = "8-18,8-18,8-18,8-18,8-19,9-16,closed"
END_OF_DAY = 24 * 60

_CLOCK = re.compile(r"^(\d{1,2})(?:[:.](\d{2}))?\s*([ap])?\.?\s*m?\.?$")
_SPLIT = re.compile(r"\s*(?:-|–|—|\bto\b)\s*")


def _minutes(value: str, *, closing: bool = False) -> int:
    """'7', '07:30', '7.30am', '3 PM', '24' -> minutes since midnight. ValueError if it isn't a time."""
    m = _CLOCK.match(value.strip().lower())
    if not m:
        raise ValueError(value)
    h, mm, ampm = int(m.group(1)), int(m.group(2) or 0), m.group(3)
    if ampm:
        if not 1 <= h <= 12:
            raise ValueError(value)
        h = h % 12 + (12 if ampm == "p" else 0)
    if mm > 59 or h > 24 or (h == 24 and (mm or not closing)):
        raise ValueError(value)
    return h * 60 + mm


def _fmt(minutes: int) -> str:
    h, m = divmod(minutes, 60)
    return f"{h}:{m:02d}" if m else str(h)


def _range(value: str) -> tuple[int, int]:
    """'7:30-15' -> (450, 900). Plain-language ValueError otherwise."""
    shown = value.strip()
    parts = _SPLIT.split(shown.lower())
    if len(parts) != 2 or not all(parts):
        raise ValueError(f"'{shown}' isn't a range of hours; write it like 8-18, 7:30-15 or 7am-3pm")
    try:
        lo = _minutes(parts[0])
        hi = _minutes(parts[1], closing=True)
    except ValueError as exc:
        raise ValueError(f"'{exc}' in '{shown}' isn't a time; use 0-24 or am/pm, like 7:30 or 3pm") from None
    if hi <= lo:
        raise ValueError(f"'{shown}' closes before it opens; overnight hours aren't supported")
    return lo, hi


def normalize_range(value: str) -> str:
    """An owner's 'open-close' as stored: '07:30-15:00' -> '7:30-15', '7am-3pm' -> '7-15'."""
    lo, hi = _range(value)
    return f"{_fmt(lo)}-{_fmt(hi)}"


def normalize_clock(value: str) -> str:
    """A delivery-window time as HH:MM: '9', '9:00 AM', '2:30 pm' -> '09:00', '14:30'. Blank stays blank."""
    value = (value or "").strip()
    if not value:
        return ""
    try:
        mins = _minutes(value)
    except ValueError:
        raise ValueError(f"'{value}' isn't a time; use HH:MM like 09:00 or 2:30 pm") from None
    return f"{mins // 60:02d}:{mins % 60:02d}"


def parse_hours(spec: str) -> dict[int, tuple[int, int] | None]:
    """SHOP_HOURS -> {weekday: (open, close) in minutes since midnight, or None when closed}."""
    parts = [p.strip().lower() for p in (spec or DEFAULT).split(",")]
    if len(parts) != 7:
        raise ValueError("SHOP_HOURS needs 7 comma-separated entries, Monday first")
    out: dict[int, tuple[int, int] | None] = {}
    for i, p in enumerate(parts):
        out[i] = None if p in ("closed", "") else _range(p)
    return out


def posted_hours(hours: dict[int, tuple[int, int] | None], day: date) -> tuple[time, time] | None:
    """(open, close) that day. Code compares open <= t < close, so closing at 24 is time.max."""
    h = hours[day.weekday()]
    if not h:
        return None
    lo, hi = h
    return time(lo // 60, lo % 60), (time.max if hi >= END_OF_DAY else time(hi // 60, hi % 60))
