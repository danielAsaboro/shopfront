"""Scan checks the owner kept, and the record they add up to.

A check on the receiving desk is computed on the spot and kept only when the owner presses "Keep in
<carrier>'s record" (a POST, owner-only and same-site like every other write; app/owner.py). The book
holds what the check found then (outcome, posted hours, the order the tracking number named), not the
door events: the record is the owner's history of what carriers scanned and what the door answered.

From the kept checks the desk shows, for the month on screen:

- one line per carrier: "Northline Parcel scanned you closed 3 times in October, every time during posted
  hours", with any demo checks said to be demo;
- the money: disputed scans times a missed delivery's cost (the owner's number, or the survey default with
  its source), next to the orders the door confirmed that month. Order amounts are shown as written and
  never summed (app/deliveries.py, Expected.amount).
"""

from __future__ import annotations

import json
import secrets
import threading
from collections import defaultdict
from datetime import date, datetime
from pathlib import Path

from app import reconcile as rc
from app.files import write_private

CLOSED_PHRASE = {rc.BUSINESS_CLOSED: "scanned you closed", rc.ATTEMPTED: "scanned a delivery attempt"}
NO_CARRIER = "A carrier you didn't name"


class CheckBook:
    """DATA/checks.json: the checks the owner kept, oldest first."""

    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.RLock()  # requests run in a thread pool; each change is read-modify-write

    def all(self) -> list[dict]:
        if not self.path.exists():
            return []
        return json.loads(self.path.read_text())

    def _write(self, rows: list[dict]) -> None:
        write_private(self.path, json.dumps(rows, indent=1))

    def find(self, day: str, at: str, claim: str, carrier: str, tracking: str) -> dict | None:
        key = _key(day, at, claim, carrier, tracking)
        return next((c for c in self.all() if _key(c["day"], c["at"], c["claim"], c["carrier"], c["tracking"])
                     == key), None)

    def keep(self, check: dict, carrier: str, tracking: str, order=None, demo: bool = False,
             saved_at: datetime | None = None) -> dict:
        """Keep `check` (a reconcile result). Keeping the same scan again replaces it, so a re-run after
        Ring sent more of the day updates the record instead of counting the scan twice."""
        if check["outcome"] in rc.REFUSED:
            raise ValueError("a refused check has no finding to keep")
        at = datetime.fromisoformat(check["attempt_at"])
        row = {"id": secrets.token_hex(6), "day": at.date().isoformat(), "at": f"{at:%H:%M}",
               "claim": check["claim"], "carrier": carrier.strip()[:40], "tracking": tracking.strip()[:60],
               "outcome": check["outcome"], "posted_hours": check.get("posted_hours") or "",
               "posted_open": check["outcome"] != rc.POSTED_CLOSED if check["claim"] == rc.BUSINESS_CLOSED else None,
               "order_ref": getattr(order, "order_ref", "") or "", "supplier": getattr(order, "supplier", "") or "",
               "amount": getattr(order, "amount", "") or "", "demo": bool(demo),
               "saved_at": (saved_at or datetime.now(at.tzinfo)).isoformat(timespec="seconds")}
        with self._lock:
            rows = self.all()
            old = self.find(row["day"], row["at"], row["claim"], row["carrier"], row["tracking"])
            if old:
                row["id"] = old["id"]
                rows = [r for r in rows if r["id"] != old["id"]]
            self._write(rows + [row])
        return row

    def remove(self, check_id: str) -> dict | None:
        with self._lock:
            rows = self.all()
            gone = next((r for r in rows if r["id"] == check_id), None)
            if gone:
                self._write([r for r in rows if r["id"] != check_id])
            return gone


def _key(day: str, at: str, claim: str, carrier: str, tracking: str) -> tuple:
    return day, at, claim, carrier.strip().lower(), tracking.strip()


def in_month(checks: list[dict], month: date) -> list[dict]:
    return [c for c in checks if c["day"][:7] == f"{month:%Y-%m}"]


def _times(n: int) -> str:
    return "once" if n == 1 else "twice" if n == 2 else f"{n} times"


def carrier_record(checks: list[dict], month: date) -> list[dict]:
    """One line per carrier and claim for `month`, most checks first."""
    groups: dict[tuple, list[dict]] = defaultdict(list)
    for c in in_month(checks, month):
        groups[(c["carrier"].strip().lower(), c["claim"])].append(c)
    out = []
    for (_, claim), cs in groups.items():
        name = cs[-1]["carrier"].strip() or NO_CARRIER
        n, m = len(cs), f"{month:%B}"
        if claim == rc.BUSINESS_CLOSED:
            open_n = sum(1 for c in cs if c["posted_open"])
            during = ("every time during posted hours" if open_n == n and n > 1 else
                      "during posted hours" if open_n == n else
                      "never during posted hours" if not open_n else f"{open_n} of them during posted hours")
            text = f"{name} scanned you closed {_times(n)} in {m}, {during}."
        else:
            seen = sum(1 for c in cs if c["outcome"] == rc.COURIER_AT_DOOR)
            text = (f"{name} scanned a delivery attempt {_times(n)} in {m}; Ring or Bedrock saw a courier at the "
                    f"door near {'none' if not seen else 'all' if seen == n else seen} of them.")
        demo = sum(1 for c in cs if c["demo"])
        out.append({"carrier": name, "claim": claim, "n": n, "text": text,
                    "demo": demo, "all_demo": demo == n, "checks": sorted(cs, key=lambda c: (c["day"], c["at"]))})
    return sorted(out, key=lambda r: (-r["n"], r["carrier"].lower()))


def month_money(checks: list[dict], month: date, confirmed_orders: int | None, cost: float | None,
                currency: str, default: bool, disputable: set[str]) -> dict | None:
    """Disputed scans times a missed delivery's cost, next to the orders the door confirmed this month.
    No cost (no number and no survey figure for the currency) means no money, only counts."""
    disputed = [c for c in in_month(checks, month) if c["outcome"] in disputable]
    if not disputed and not confirmed_orders:
        return None

    def money(n: int) -> str | None:
        return None if cost is None else f"{currency}{cost * n:,.2f}"
    return {"month": f"{month:%B}", "disputed": len(disputed), "at_stake": money(len(disputed)),
            "confirmed": confirmed_orders, "confirmed_value": money(confirmed_orders or 0),
            "each": None if cost is None else f"{currency}{cost:.2f}", "default": default,
            "demo": sum(1 for c in disputed if c["demo"])}
