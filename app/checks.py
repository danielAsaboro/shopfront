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


def carrier_record(checks: list[dict], month: date, cost: float | None = None, currency: str = "$",
                   disputable: set[str] | frozenset[str] = frozenset()) -> list[dict]:
    """One line per carrier and claim for `month`, most checks first. With a missed delivery's `cost`, each
    line also says what the scans the door disputes (`disputable` outcomes) put at stake."""
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
        disputed = sum(1 for c in cs if c["outcome"] in disputable)
        stake = f"{currency}{cost * disputed:,.2f}" if cost is not None and disputed else None
        out.append({"carrier": name, "claim": claim, "n": n, "text": text,
                    "headline": _headline(name, claim, n, m, cs, stake), "disputed": disputed, "at_stake": stake,
                    "demo": demo, "all_demo": demo == n, "checks": sorted(cs, key=lambda c: (c["day"], c["at"]))})
    return sorted(out, key=lambda r: (-r["n"], r["carrier"].lower()))


def _headline(name: str, claim: str, n: int, month: str, cs: list[dict], stake: str | None) -> str:
    """'Northline Parcel: 3 "business closed" scans in October, all during posted hours, about $51.60 at stake':
    the pattern a carrier's account manager or a supplier can act on, in the shop's plain words."""
    what = '"business closed" scan' if claim == rc.BUSINESS_CLOSED else "delivery-attempt scan"
    line = f"{name}: {n} {what}{'' if n == 1 else 's'} in {month}"
    if claim == rc.BUSINESS_CLOSED:
        open_n = sum(1 for c in cs if c["posted_open"])
        line += (", all during posted hours" if open_n == n and n > 1 else ", during posted hours" if open_n == n
                 else ", none during posted hours" if not open_n else f", {open_n} during posted hours")
    else:
        seen = sum(1 for c in cs if c["outcome"] == rc.COURIER_AT_DOOR)
        line += f", a courier seen near {'none' if not seen else 'all' if seen == n else seen} of them"
    return line + (f", about {stake} at stake" if stake else "")


# What the door showed, for a reader outside the shop (a carrier's account manager, a supplier): third person,
# no guess stated as a fact, nothing that accuses anyone (app/wording.py)
REPORT_LINE = {
    rc.OPEN_DOOR_BUSY: "Open by posted hours; the door was in use around the scan",
    rc.OPEN_NO_COURIER: "Open by posted hours; the door was in use, and no courier was recorded near the scan",
    rc.OPEN_COURIER_CAME: "Open by posted hours; a courier was at the door near the scan",
    rc.OPEN_BY_HOURS_ONLY: "Open by posted hours; the door recorded nothing around the scan",
    rc.POSTED_CLOSED: "Closed by posted hours, as the scan says",
    rc.COURIER_LATER: "No courier near the scan; one came to the door later that day",
    rc.UNVERIFIED: "Door activity near the scan that nothing identified",
    rc.CAMERA_OFFLINE: "The doorbell was reported offline around the scan",
    rc.NO_COVERAGE: "Nothing recorded around the scan, and no sign the camera was watching",
    rc.COURIER_AT_DOOR: "A courier was at the door near the scan",
    rc.SOMEONE_NOT_COURIER: "A visitor near the scan, not a courier",
    rc.NOTHING_RECORDED: "Nothing recorded near the scan",
    rc.NO_DOOR_DATA: "No door data that day",
}


def month_report(checks: list[dict], month: date, cost: float | None, currency: str, default: bool,
                 disputable: set[str]) -> dict:
    """The month's carrier record as one printable page (/report?month=YYYY-MM): per carrier, the pattern line
    and every kept check with what the door showed. Order amounts are listed as written, never added up."""
    carriers = carrier_record(checks, month, cost, currency, disputable)
    for c in carriers:
        c["rows"] = [{**k, "line": REPORT_LINE.get(k["outcome"], k["outcome"].replace("_", " ")),
                      "disputed": k["outcome"] in disputable} for k in c["checks"]]
    rows = in_month(checks, month)
    return {"month": f"{month:%B %Y}", "month_key": f"{month:%Y-%m}", "carriers": carriers,
            "money": month_money(checks, month, None, cost, currency, default, disputable),
            "checks": len(rows), "demo": sum(1 for c in rows if c["demo"]),
            "orders": sorted({f"{c['order_ref']} ({c['amount']})" if c["amount"] else c["order_ref"]
                              for c in rows if c["order_ref"]})}


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
