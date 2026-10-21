"""The receiving-desk demo in one command.

    .venv/bin/python -m scripts.demo [--port 8000]

What it does, in order:

1. Picks the demo day: the most recent day before today when the demo shop is open, so nothing
   it shows is in the future.
2. Copies samples/orders.csv with its dates moved to that day and imports it into a fresh data
   folder (.data/demo, git-ignored), the same way the CSV upload on /setup does.
3. Starts Shopfront on that folder with synthetic door events (no Ring account needed; every page
   says the events are synthetic).
4. Replays one signed Ring `package_delivery` webhook at 10:12 through scripts/feed_webhook.py,
   the way Ring's Smart Alert would announce the flour courier. It is logged as a replay, never as
   Ring's own.
5. Picks a time when the door was busy but no one was at it, treats it as the carrier's
   "business closed" scan for the dairy order, asks the running app what the door shows, and
   prints the links to check it and to build the dispute pack.

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
PACKAGE_AT = "10:12"          # Ring's package_delivery alert for the flour order (PO-1041)
SCAN_ORDER = "PO-1042"        # the dairy order the carrier scanned "business closed"
DEMO_HMAC_KEY = "demo-key"


def open_hours(spec: str, day: date) -> tuple[int, int] | None:
    """(open, close) hours for `day` from a Monday-first "8-18,...,closed" spec, or None if closed."""
    part = spec.split(",")[day.weekday()].strip().lower()
    if part == "closed":
        return None
    start, end = (int(x) for x in part.split("-"))
    return start, end


def demo_day(today: date, spec: str = SHOP_HOURS) -> date:
    """The most recent day before `today` when the shop was open."""
    for back in range(1, 8):
        d = today - timedelta(days=back)
        if open_hours(spec, d):
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


def pick_scan_time(day: date, events: list[datetime], hours: tuple[int, int], avoid: list[datetime],
                   tz: ZoneInfo) -> str:
    """A time inside posted hours with door activity within 30 minutes but nothing within 12, so the
    door shows an open, busy shop and no courier near the scan. Prefers the afternoon delivery window."""
    open_h, close_h = hours

    def ok(t: datetime) -> bool:
        gaps = [abs((e - t).total_seconds()) / 60 for e in events]
        return (all(g > 12 for g in gaps) and sum(g <= 30 for g in gaps) >= 2
                and all(abs((a - t).total_seconds()) / 60 > 40 for a in avoid))

    def slots(lo: int, hi: int):
        for h in range(lo, hi):
            for m in range(0, 60, 5):
                yield datetime(day.year, day.month, day.day, h, m, tzinfo=tz)

    preferred = list(slots(max(open_h, 13), min(close_h - 1, 16)))
    for t in preferred + list(slots(open_h + 1, close_h - 1)):
        if ok(t):
            return t.strftime("%H:%M")
    return f"{min(max(open_h + 1, 14), close_h - 1):02d}:30"


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


def _import_orders(data: Path, text: str) -> int:
    from app.deliveries import DeliveryBook
    from app.orders import parse_orders

    parsed = parse_orders(text)
    if parsed["errors"]:
        raise SystemExit("the sample orders didn't import: " + "; ".join(parsed["errors"]))
    DeliveryBook(data / "expected-deliveries.json").import_rows(parsed["rows"])
    return len(parsed["rows"])


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

    day = demo_day(datetime.now(tz).date())
    hours = open_hours(SHOP_HOURS, day)
    assert hours
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
        fed = subprocess.run([sys.executable, "-m", "scripts.feed_webhook", "--sub-type", "package_delivery",
                              "--at", f"{day.isoformat()}T{PACKAGE_AT}", "--url", f"{base}/webhooks/ring"],
                             cwd=ROOT, env=env, capture_output=True, text=True)
        if fed.returncode != 0 or not fed.stdout.startswith("200"):
            raise SystemExit("The signed webhook replay was refused:\n" + fed.stdout + fed.stderr)

        events = [datetime.fromisoformat(e["time"]) for e in httpx.get(
            f"{base}/api/events", params={"date": day.isoformat()}, timeout=30).json()]
        package = datetime.combine(day, datetime.strptime(PACKAGE_AT, "%H:%M").time(), tzinfo=tz)
        scan = pick_scan_time(day, events, hours, [package], tz)
        order = next(r for r in csv.DictReader(io.StringIO(orders_csv.read_text())) if r["order_ref"] == SCAN_ORDER)
        check = httpx.get(f"{base}/api/attempt", params={"at": f"{day.isoformat()}T{scan}",
                                                          "claim": "business_closed"}, timeout=30).json()

        q = {"date": day.isoformat(), "carrier": CARRIER, "tracking": order["tracking"]}
        desk = f"{base}/?{urlencode({'date': day.isoformat()})}"
        check_url = f"{base}/?{urlencode({**q, 'attempt': scan, 'claim': 'business_closed'})}#atth"
        pack_url = f"{base}/dispute?{urlencode({**q, 'at': scan, 'claim': 'business_closed'})}"
        print(f"""
Shopfront demo is running at {base}
{SHOP_NAME}, {day:%A %d %B %Y}, open {hours[0]:02d}:00-{hours[1]:02d}:00. Door events are synthetic and the
Ring webhook is a signed replay: every page says so. Nothing here is sent to anyone.

1. The receiving desk. {n} orders were imported from samples/orders.csv (dated copy: {orders_csv}).
   The flour courier (PO-1041) was announced at {PACKAGE_AT} by Ring's package_delivery Smart Alert.
   {desk}

2. {CARRIER} scanned the dairy order {SCAN_ORDER} (tracking {order['tracking']}) "business closed" at {scan}.
   Check that scan against the posted hours and the door:
   {check_url}
   Shopfront's answer: {check.get('text') or check}

3. The dispute pack: the finding, the door log around {scan}, the posted hours, what it can't show,
   and a draft you copy and send yourself:
   {pack_url}

Ctrl-C stops the server. {data} is wiped on the next demo run; your own .data is untouched.
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
