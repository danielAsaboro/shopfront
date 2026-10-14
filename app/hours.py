"""Posted opening hours, Monday first. Set SHOP_HOURS to override, e.g.
SHOP_HOURS="8-18,8-18,8-18,8-18,8-19,9-16,closed"."""

from __future__ import annotations

from datetime import date, time

DEFAULT = "8-18,8-18,8-18,8-18,8-19,9-16,closed"


def parse_hours(spec: str) -> dict[int, tuple[int, int] | None]:
    parts = [p.strip().lower() for p in (spec or DEFAULT).split(",")]
    if len(parts) != 7:
        raise ValueError("SHOP_HOURS needs 7 comma-separated entries, Monday first")
    out: dict[int, tuple[int, int] | None] = {}
    for i, p in enumerate(parts):
        if p == "closed":
            out[i] = None
            continue
        open_h, close_h = (int(x) for x in p.split("-"))
        if not 0 <= open_h < close_h <= 24:
            raise ValueError(f"bad hours {p!r}")
        out[i] = (open_h, close_h)
    return out


def posted_hours(hours: dict[int, tuple[int, int] | None], day: date) -> tuple[time, time] | None:
    h = hours[day.weekday()]
    if not h:
        return None
    return time(h[0]), (time(23, 59, 59) if h[1] == 24 else time(h[1]))
