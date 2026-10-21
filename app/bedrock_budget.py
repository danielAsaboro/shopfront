"""What Bedrock is asked about, and what it costs (docs/bedrock.md, section 11).

**Focus.** A doorbell on a busy street files hundreds of events a day; labelling every one with a vision model
pays to tell customers apart, which the receiving desk doesn't need. A frame goes to Bedrock only when the event is
- inside an expected delivery's window (the order's window widened by `REACH` either side; an order with no
  window covers its whole day), or
- within `REACH` of a carrier scan being checked (on the desk, in a pack, or kept in the carrier record), or
- on a day the owner asked to label (the "Label the rest with Bedrock" button under "Every door event").
Everything else is labelled by the time-of-day rules, marked as guesses as always, with a reason saying it was
held back. No Ring frame is downloaded for it either. A label Bedrock already gave is reused from the cache.

**Cost.** BedrockClassifier records each Converse call's `usage` (inputTokens, outputTokens) in
`.data/bedrock-calls.jsonl` with the event's shop-local day. `day_cost` adds them up for a day and prices them
with BEDROCK_PRICE_INPUT_PER_MTOK / BEDROCK_PRICE_OUTPUT_PER_MTOK (USD per million tokens). The defaults are
the list price of the default model, Claude Haiku 4.5 ($1 in, $5 out per million); they are not verified
against the AWS Bedrock pricing page, so the footer says "price not verified" until the owner sets them.
"""

from __future__ import annotations

import json
import threading
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Callable, Iterable
from zoneinfo import ZoneInfo

from app.files import read_jsonl, write_private

REACH = timedelta(minutes=10)
HELD_REASON = "not sent to Bedrock: no expected delivery or checked scan near this time"


class LabelRequests:
    """Days the owner asked Bedrock to label in full: DATA/bedrock-requests.json."""

    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.Lock()

    def days(self) -> set[str]:
        try:
            rows = json.loads(self.path.read_text())
        except (OSError, ValueError):
            return set()
        return set(rows) if isinstance(rows, list) else set()

    def ask(self, day: date) -> None:
        with self._lock:
            write_private(self.path, json.dumps(sorted(self.days() | {day.isoformat()})))

    def asked(self, day: date) -> bool:
        return day.isoformat() in self.days()


def windows(day: date, orders: Iterable, scans: Iterable[datetime], tz: ZoneInfo,
            asked: bool = False) -> list[tuple[datetime, datetime, str]]:
    """The (start, end, why) spans of `day` whose door events Bedrock may look at."""
    start_of_day = datetime.combine(day, time.min, tz)
    end_of_day = datetime.combine(day + timedelta(days=1), time.min, tz)
    if asked:
        return [(start_of_day, end_of_day, "the owner asked Bedrock to label this day")]
    spans = []
    for e in orders:
        w = e.window()
        what = e.order_ref or e.supplier
        if w is None:
            spans.append((start_of_day, end_of_day, f"{what} is expected today, no window given"))
        else:
            spans.append((datetime.combine(day, w[0], tz) - REACH, datetime.combine(day, w[1], tz) + REACH,
                          f"inside {what}'s delivery window {w[0]:%H:%M}–{w[1]:%H:%M}"))
    for s in scans:
        s = s.replace(tzinfo=tz) if s.tzinfo is None else s.astimezone(tz)
        if s - REACH < end_of_day and s + REACH >= start_of_day:
            spans.append((s - REACH, s + REACH, f"within 10 minutes of the {s:%H:%M} scan being checked"))
    return spans


class Focus:
    """`focus(event)` -> why Bedrock may look at it, or None. Built per request from the orders, the scans
    being checked and the owner's requests."""

    def __init__(self, tz: ZoneInfo, orders: Callable[[date], list], scans: Callable[[], list[datetime]],
                 asked: Callable[[date], bool]):
        self.tz, self.orders, self.scans, self.asked = tz, orders, scans, asked
        self._spans: dict[date, list] = {}

    def __call__(self, event) -> str | None:
        at = event.timestamp.astimezone(self.tz)
        d = at.date()
        if d not in self._spans:
            self._spans[d] = windows(d, self.orders(d), self.scans(), self.tz, self.asked(d))
        return next((why for start, end, why in self._spans[d] if start <= at <= end), None)


# --- cost ------------------------------------------------------------------------------------------------
def tokens(row: dict) -> tuple[int, int]:
    u = row.get("usage") if isinstance(row.get("usage"), dict) else {}
    try:
        return int(u.get("inputTokens") or 0), int(u.get("outputTokens") or 0)
    except (TypeError, ValueError):
        return 0, 0


def day_cost(path: Path, day: date, price_in: float, price_out: float, tz: ZoneInfo) -> dict:
    """Bedrock frame-labelling calls for door events of `day`: calls, tokens and the estimated USD."""
    calls = tin = tout = 0
    for row in read_jsonl(path):
        when = row.get("event_day")
        if when is None:  # recorded before event_day was: the day the call was made
            try:
                when = datetime.fromisoformat(str(row.get("at"))).astimezone(tz).date().isoformat()
            except ValueError:
                continue
        if when != day.isoformat():
            continue
        i, o = tokens(row)
        calls += 1
        tin += i
        tout += o
    return {"calls": calls, "input_tokens": tin, "output_tokens": tout,
            "usd": round(tin / 1e6 * price_in + tout / 1e6 * price_out, 4)}


def cost_line(c: dict, verified: bool) -> str:
    if not c["calls"]:
        return "Bedrock: no frame-labelling calls for this day, so no cost."
    note = "" if verified else " (price not verified: set BEDROCK_PRICE_INPUT_PER_MTOK and _OUTPUT_PER_MTOK)"
    return (f"Bedrock: {c['calls']} call{'s' if c['calls'] != 1 else ''}, {c['input_tokens']:,} tokens in, "
            f"{c['output_tokens']:,} out, about ${c['usd']:.4f}{note}")
