"""Was the camera watching around a carrier's scan? What Shopfront can actually show.

"Nothing recorded near the scan" only supports the owner if the doorbell was online and recording then.
Two things can say so:

1. Ring's own status notifications. The API reference documents `device_online` and `device_offline`
   webhooks (Notifications > Device Online, Notifications > Device Offline: "sent when a Ring device goes
   offline and is no longer reachable", payload `data.attributes.source` = device, `.timestamp` = ms) and
   `GET /v1/devices/{device_id}/status` (Device Discovery > Status: `attributes.online`, "whether the
   device is currently online"). The status endpoint only says what is true *now*, so it can't answer
   for a scan in the past; the notifications, logged as they arrive (app/ring/webhooks.py logs every
   verified notification, whatever its type), can. A `device_offline` before the scan with no
   `device_online` until after it, or one inside the scan's tolerance, means the camera may not have been
   watching.
2. The door's own events: Ring recorded motion or a press within `REACH` of the scan, so the doorbell was
   up and recording around then.

With neither, coverage is unknown: the door recorded nothing around the scan and nothing says the camera
was online, so "nothing recorded" can't be offered as evidence. Notifications only exist when Ring sends
webhooks to this Shopfront (the Developer Playground sends none), so the pack says when there are none.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from app.models import ClassifiedVisit
from app.ring.webhooks import ours

ONLINE, OFFLINE = "device_online", "device_offline"
REACH = timedelta(minutes=30)  # door events this close to the scan show the doorbell was recording

WATCHING = "door_events"  # the door recorded events around the scan
ONLINE_BY_RING = "online_by_ring"  # no events, but Ring's last status notice said online
CAMERA_DOWN = "offline"  # Ring said offline at some point within the tolerance of the scan
UNKNOWN = "unknown"  # no events around the scan and no status notice saying online


def notices(rows: list[dict], tz: ZoneInfo, device_id: str = "", account_id: str = "") -> list[dict]:
    """The shop doorbell's device_online / device_offline notifications from the webhook log, oldest first:
    {"at": aware datetime, "online": bool, "replayed": bool}."""
    out = []
    for r in rows:
        if r.get("type") not in (ONLINE, OFFLINE) or not ours(r, device_id, account_id):
            continue
        try:
            at = datetime.fromtimestamp(int(r["timestamp"]) / 1000, tz=tz)
        except (KeyError, TypeError, ValueError, OverflowError, OSError):
            continue
        out.append({"at": at, "online": r["type"] == ONLINE, "replayed": bool(r.get("replayed"))})
    return sorted(out, key=lambda n: n["at"].astimezone(UTC))


def _hhmm(t: datetime, tz) -> str:
    return t.astimezone(tz).strftime("%H:%M")


def check(scan: datetime, visits: list[ClassifiedVisit], status: list[dict] | None,
          tolerance: timedelta, reach: timedelta = REACH) -> dict:
    """Coverage around `scan` (aware, shop local): its state, one sentence for the pack, and the facts."""
    tz = scan.tzinfo
    u = scan.astimezone(UTC)
    lo, hi = u - tolerance, u + tolerance
    status = status or []
    before = [n for n in status if n["at"].astimezone(UTC) <= lo]
    inside = [n for n in status if lo < n["at"].astimezone(UTC) <= hi]
    down = [n for n in ([before[-1]] if before else []) + inside if not n["online"]]
    around = sorted((v.event.timestamp for v in visits if abs(v.event.timestamp.astimezone(UTC) - u) <= reach),
                    key=lambda t: abs(t.astimezone(UTC) - u))
    facts = {"events_around": len(around), "reach_min": int(reach.total_seconds() // 60),
             "status_notices": len(status), "offline_from": None, "offline_until": None}
    if down:
        start = down[-1]["at"]
        back = next((n["at"] for n in status if n["online"] and n["at"].astimezone(UTC) > start.astimezone(UTC)),
                    None)
        who = "A replayed status notice (Shopfront's feeder, not Ring) said" if any(
            n["replayed"] for n in down) else "Ring reported"
        span = f"offline from {_hhmm(start, tz)}" + (f" until {_hhmm(back, tz)}" if back else
                                                       ", with no device_online notice after it")
        facts.update(offline_from=start.isoformat(timespec="minutes"),
                     offline_until=back.isoformat(timespec="minutes") if back else None)
        return {**facts, "state": CAMERA_DOWN,
                "text": f"{who} the doorbell {span} (device_offline), so the camera may not have been watching "
                        f"at {_hhmm(scan, tz)}."}
    no_notices = (" Shopfront has no Ring device_online/device_offline notices for this doorbell, so an outage "
                  "between door events wouldn't show.") if not status else ""
    if around:
        n = len(around)
        return {**facts, "state": WATCHING,
                "text": f"The doorbell recorded {n} door event{'' if n == 1 else 's'} within "
                        f"{facts['reach_min']} minutes of {_hhmm(scan, tz)} (nearest at {_hhmm(around[0], tz)}), "
                        f"so it was recording around then.{no_notices}"}
    if before and before[-1]["online"]:
        return {**facts, "state": ONLINE_BY_RING,
                "text": f"The doorbell recorded nothing within {facts['reach_min']} minutes of {_hhmm(scan, tz)}, "
                        f"but Ring's last status notice before then said it was online (device_online at "
                        f"{_hhmm(before[-1]['at'], tz)}) and no device_offline followed."}
    return {**facts, "state": UNKNOWN,
            "text": f"The doorbell recorded nothing within {facts['reach_min']} minutes of {_hhmm(scan, tz)}, and "
                    f"Shopfront has no Ring status notice saying it was online, so the camera may not have been "
                    f"watching."}
