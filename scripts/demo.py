"""The receiving-desk demo in one command.

    .venv/bin/python -m scripts.demo [--port 8000]

What it does, in order:

1. Picks the demo day: the most recent day before today when the demo shop is open and its synthetic
   door leaves a scan time the door can answer, so nothing it shows is in the future.
2. Copies samples/orders.csv with its dates moved to that day and imports it into a fresh data
   folder (.data/demo, git-ignored), the same way the CSV upload on /setup does.
3. Starts Shopfront on that folder with synthetic door events (no Ring account needed; every page
   says the events are synthetic).
4. Replays one signed Ring `package_delivery` webhook at 10:12, built and signed the way
   scripts/feed_webhook.py does it, the way Ring's Smart Alert would announce the flour courier. It is
   logged as a replay, never as Ring's own. The receiving desk must then show that alert as the flour
   order's courier (PO-1041); if it doesn't, the demo stops and says so instead of telling a story the
   page contradicts.
5. Picks a time when the door was busy but no one was at it, treats it as the carrier's
   "business closed" scan for the dairy order, asks the running app what the door shows and builds the
   dispute pack, and prints the links to both. The pack must have a draft; if the door doesn't support
   one, the demo stops and says why.

The server keeps running until Ctrl-C. Nothing is sent to any carrier or supplier.
"""

from __future__ import annotations

import argparse
import csv
import io
import os
import shutil
import socket
import subprocess
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

import httpx

ROOT = Path(__file__).resolve().parent.parent
SAMPLE = ROOT / "samples" / "orders.csv"
MARKER = ".shopfront-demo"

SHOP_NAME = "Corner Bakery"
SHOP_HOURS = "8-18,8-18,8-18,8-18,8-19,9-16,closed"  # Monday first
CARRIER = "Northline Parcel"  # made-up carrier; the owner types the real one on the dashboard
PACKAGE_AT = "10:12"          # Ring's package_delivery alert for the flour order
FLOUR_ORDER = "PO-1041"       # Hillside Mill, due 10:00-11:00
SCAN_ORDER = "PO-1042"        # the dairy order the carrier scanned "business closed"
SCAN_WINDOW = ("13:00", "16:00")  # its delivery window in samples/orders.csv
DEMO_HMAC_KEY = "demo-key"


def open_hours(spec: str, day: date) -> tuple[int, int] | None:
    """(open, close) hours for `day` from a Monday-first "8-18,...,closed" spec, or None if closed."""
    part = spec.split(",")[day.weekday()].strip().lower()
    if part == "closed":
        return None
    start, end = (int(x) for x in part.split("-"))
    return start, end


def demo_day(today: date, spec: str = SHOP_HOURS, usable=lambda d: True) -> date:
    """The most recent day before `today` when the shop was open (and `usable(day)` holds)."""
    for back in range(1, 29):
        d = today - timedelta(days=back)
        if open_hours(spec, d) and usable(d):
            return d
    raise ValueError("the demo hours have no open day")


def dated_orders(text: str, day: date) -> str:
    """The sample CSV with every row's `day` set to `day`."""
    rows = list(csv.DictReader(io.StringIO(text)))
    out = io.StringIO()
    w = csv.DictWriter(out, fieldnames=list(rows[0].keys()) if rows else ["day"], lineterminator="\n")
    w.writeheader()
    for r in rows:
        w.writerow({**r, "day": day.isoformat()})
    return out.getvalue()


def pick_scan_time(day: date, events: list[datetime], hours: tuple[int, int], couriers: list[datetime],
                   tz: ZoneInfo, prefer: tuple[str, str] | None = None) -> str | None:
    """A "business closed" scan time the door can honestly answer, or None: inside posted hours, with door
    activity within 30 minutes (so the door was recording) but nothing within 12 (more than the scan
    check's 10-minute tolerance, so nothing near it could have been the courier), and well after every
    courier Ring confirmed that day (a scan before one reads "a courier came later"). Prefers the scanned
    order's window `prefer` ("13:00", "16:00"), then a busier door."""
    open_h, close_h = hours

    def score(t: datetime) -> tuple | None:
        gaps = [abs((e - t).total_seconds()) / 60 for e in events]
        nearby = sum(g <= 30 for g in gaps)
        if not nearby or any(g <= 12 for g in gaps) or any((t - c).total_seconds() / 60 <= 40 for c in couriers):
            return None
        in_window = prefer is not None and prefer[0] <= t.strftime("%H:%M") < prefer[1]
        return (in_window, min(nearby, 2))

    best = None
    for m in range((open_h + 1) * 60, (close_h - 1) * 60):
        t = datetime(day.year, day.month, day.day, m // 60, m % 60, tzinfo=tz)
        s = score(t)
        if s is not None and (best is None or s > best[0]):
            best = (s, t)
    return best[1].strftime("%H:%M") if best else None


def _package_at(day: date, tz: ZoneInfo) -> datetime:
    return datetime.combine(day, datetime.strptime(PACKAGE_AT, "%H:%M").time(), tzinfo=tz)


def has_scan_slot(day: date, tz: ZoneInfo, window: tuple[str, str] = SCAN_WINDOW) -> bool:
    """Whether the demo's synthetic door that day leaves a scan time the door can answer (some days are
    too busy). The same generator, hours and time zone the demo server uses."""
    from app.hours import parse_hours
    from app.ring.synthetic import SyntheticRingClient

    hours = open_hours(SHOP_HOURS, day)
    if not hours:
        return False
    events = [e.timestamp for e in SyntheticRingClient(tz, parse_hours(SHOP_HOURS)).events_for_day(day)]
    return pick_scan_time(day, events + [_package_at(day, tz)], hours, [_package_at(day, tz)],
                                          tz, window) is not None


def _port_busy(port: int) -> bool:
    with socket.socket() as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", port)) == 0


def _wait_up(base: str, server: subprocess.Popen, seconds: float = 30) -> None:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if server.poll() is not None:
            raise SystemExit("Shopfront didn't start; its output is above.")
        try:
            if httpx.get(f"{base}/healthz", timeout=2).status_code == 200:
                return
        except httpx.HTTPError:
            pass
        time.sleep(0.3)
    raise SystemExit(f"Shopfront didn't answer on {base} within {seconds:.0f} seconds.")


def _fresh(data: Path) -> None:
    """Start from an empty data folder, but only ever wipe one this script made."""
    if data.exists():
        if not (data / MARKER).exists():
            raise SystemExit(f"{data} exists and wasn't made by the demo; pass another --data-dir.")
        shutil.rmtree(data)
    data.mkdir(parents=True)
    (data / MARKER).write_text("made by scripts/demo.py; deleted on the next demo run\n")


def _show(path: Path) -> str:
    return str(path.relative_to(ROOT)) if path.is_relative_to(ROOT) else str(path)


def _import_orders(data: Path, text: str) -> int:
    from app.deliveries import DeliveryBook
    from app.orders import parse_orders

    parsed = parse_orders(text)
    if parsed["errors"]:
        raise SystemExit("the sample orders didn't import: " + "; ".join(parsed["errors"]))
    DeliveryBook(data / "expected-deliveries.json").import_rows(parsed["rows"])
    return len(parsed["rows"])


class DemoMismatch(SystemExit):
    """The pages don't say what the demo is about to tell the viewer."""


def feed_package_alert(client, day: date, tz: ZoneInfo, key: str) -> None:
    """POST the signed package_delivery replay at PACKAGE_AT, exactly as scripts/feed_webhook.py would."""
    from scripts.feed_webhook import payload, signed

    raw, sig = signed(payload("motion_detected", _package_at(day, tz), "replay-doorbell", "package_delivery"), key)
    r = client.post("/webhooks/ring", content=raw, headers={"X-Signature": sig, "Content-Type": "application/json"})
    if r.status_code != 200:
        raise DemoMismatch(f"The signed webhook replay was refused: {r.status_code} {r.text}")


def walk(client, day: date, tz: ZoneInfo, orders_text: str) -> dict:
    """The demo's story, read back from the running app: the flour order's courier on the receiving desk,
    the dairy order's "business closed" scan, its check and its dispute pack. `client` is anything with
    httpx's get/post on the app (an httpx.Client with base_url, or Starlette's TestClient). Raises
    DemoMismatch when the app doesn't say what the demo would print."""
    hours = open_hours(SHOP_HOURS, day)
    assert hours
    desk = client.get("/api/deliveries", params={"date": day.isoformat()}).json()
    flour = next(x for x in desk["expected"] if x["order_ref"] == FLOUR_ORDER)
    v = flour.get("visit") or {}
    if flour["status"] != "seen" or v.get("time") != PACKAGE_AT or v.get("labelled_by") != "ring":
        raise DemoMismatch(f"The receiving desk doesn't show Ring's {PACKAGE_AT} alert as {FLOUR_ORDER}'s courier "
                           f"(it shows {flour['status']} {v.get('time', '')}), so the demo won't claim it.")
    events = [datetime.fromisoformat(e["time"]) for e in
              client.get("/api/events", params={"date": day.isoformat()}).json()]
    scan = pick_scan_time(day, events, hours, [_package_at(day, tz)], tz, SCAN_WINDOW)
    if scan is None:
        raise DemoMismatch(f"The synthetic door on {day} is too busy to leave a scan time the door can answer.")
    order = next(r for r in csv.DictReader(io.StringIO(orders_text)) if r["order_ref"] == SCAN_ORDER)
    check = client.get("/api/attempt", params={"at": f"{day.isoformat()}T{scan}", "claim": "business_closed"}).json()
    q = {"date": day.isoformat(), "carrier": CARRIER, "tracking": order["tracking"]}
    pack = client.get("/api/dispute", params={**q, "at": scan, "claim": "business_closed"}).json()
    if not pack.get("draft"):
        raise DemoMismatch(f"The door doesn't support a dispute of the {scan} scan ({check.get('outcome')}): "
                           f"{pack.get('no_draft_reason') or check.get('text')}")
    return {"hours": hours, "flour": flour, "scan": scan, "order": order, "check": check, "pack": pack,
            "query": q}


def main() -> None:
    ap = argparse.ArgumentParser(description="Run the Shopfront receiving-desk demo.")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--data-dir", default=".data/demo", help="wiped and refilled on every run")
    a = ap.parse_args()

    from app.config import settings  # reads .env, so the demo uses the owner's time zone

    tz = settings.tz
    base = f"http://127.0.0.1:{a.port}"
    if _port_busy(a.port):
        raise SystemExit(f"Something is already listening on port {a.port}. Stop it, or pass --port 8010.")

    day = demo_day(datetime.now(tz).date(), usable=lambda d: has_scan_slot(d, tz))
    data = (ROOT / a.data_dir) if not Path(a.data_dir).is_absolute() else Path(a.data_dir)
    _fresh(data)
    orders_csv = data / f"orders-{day.isoformat()}.csv"
    orders_csv.write_text(dated_orders(SAMPLE.read_text(), day))
    n = _import_orders(data, orders_csv.read_text())

    key = os.getenv("RING_HMAC_KEY") or settings.ring_hmac_key or DEMO_HMAC_KEY
    env = {**os.environ, "SHOPFRONT_DATA_DIR": str(data), "RING_MODE": "synthetic", "RING_HMAC_KEY": key,
           "SHOP_NAME": SHOP_NAME, "SHOP_HOURS": SHOP_HOURS, "SHOP_TIMEZONE": settings.timezone,
           "RING_ACCESS_TOKEN": "", "RING_REFRESH_TOKEN": ""}
    server = subprocess.Popen([sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1",
                               "--port", str(a.port), "--log-level", "warning"], cwd=ROOT, env=env)
    try:
        _wait_up(base, server)
        with httpx.Client(base_url=base, timeout=30, trust_env=False) as client:  # no proxy for localhost
            feed_package_alert(client, day, tz, key)
            story = walk(client, day, tz, orders_csv.read_text())
        hours, scan, order, check, q = (story[k] for k in ("hours", "scan", "order", "check", "query"))
        desk = f"{base}/?{urlencode({'date': day.isoformat()})}"
        check_url = f"{base}/?{urlencode({**q, 'attempt': scan, 'claim': 'business_closed'})}#atth"
        pack_url = f"{base}/dispute?{urlencode({**q, 'at': scan, 'claim': 'business_closed'})}"
        print(f"""
Shopfront demo is running at {base}
{SHOP_NAME}, {day:%A %d %B %Y}, open {hours[0]:02d}:00-{hours[1]:02d}:00. Door events are synthetic and the
Ring webhook is a signed replay: every page says so. Nothing here is sent to anyone.

1. The receiving desk. {n} orders were imported from samples/orders.csv (dated copy: {_show(orders_csv)}).
   The flour courier ({FLOUR_ORDER}, {story['flour']['supplier']}) was announced at {PACKAGE_AT} by Ring's
   package_delivery Smart Alert; the desk shows it as that order's courier.
   {desk}

2. {CARRIER} scanned the dairy order {SCAN_ORDER} (tracking {order['tracking']}) "business closed" at {scan}.
   Check that scan against the posted hours and the door:
   {check_url}
   Shopfront's answer: {check.get('text') or check}

3. The dispute pack: the finding, the door log around {scan}, the posted hours, what it can't show,
   and a draft to {story['pack']['draft']['to']} you copy and send yourself:
   {pack_url}

Ctrl-C stops the server. {_show(data)} is wiped on the next demo run; nothing else in .data is touched.
""", flush=True)
        server.wait()
    except KeyboardInterrupt:
        pass
    finally:
        if server.poll() is None:
            server.terminate()
            try:
                server.wait(timeout=10)
            except subprocess.TimeoutExpired:
                server.kill()


if __name__ == "__main__":
    main()
