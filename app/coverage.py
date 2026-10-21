"""Was the camera watching around a carrier's scan? What Shopfront can actually show.

"Nothing recorded near the scan" only supports the owner if the doorbell was online and recording then.
Three things can say so:

1. Shopfront's own status checks. While Ring is live, the app asks `GET /v1/devices/{device_id}/status`
   (API reference, Device Discovery > Status: `attributes.online`, "whether the device is currently online")
   every few minutes and keeps each answer (app/ring/status.py). The endpoint only speaks for the moment it
   is asked, so only the time between two consecutive answers that both said online, taken close together
   (`app.ring.status.close`), counts as watching. A lone answer, a failed check, or two answers too far
   apart leave a gap, and the pack names it.
2. Ring's status notifications. The API reference documents `device_online` and `device_offline` webhooks
   (Notifications > Device Online, Notifications > Device Offline: "sent when a Ring device goes offline
   and is no longer reachable", payload `data.attributes.source` = device, `.timestamp` = ms), logged as
   they arrive (app/ring/webhooks.py). A `device_offline` before the scan with no `device_online` until
   after it, or one inside the scan's tolerance, means the camera may not have been watching. The Developer
   Playground sends no webhooks, so on the Playground only the status checks can say this.
3. The door's own events: Ring recorded motion or a press within `REACH` of the scan, so the doorbell was
   up and recording at those moments (not necessarily between them).

With none of them, coverage is unknown: "nothing recorded" can't be offered as evidence.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from app.models import ClassifiedVisit
from app.ring.status import DEFAULT_MINUTES, close
from app.ring.webhooks import ours

STATUS_DEFAULT_S = DEFAULT_MINUTES * 60

ONLINE, OFFLINE = "device_online", "device_offline"
REACH = timedelta(minutes=30)  # door events this close to the scan show the doorbell was recording

ONLINE_BY_STATUS = "online_by_status"  # Shopfront's status checks said online, close together, all through
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


def _spans(samples: list[dict]) -> list[tuple[datetime, datetime]]:
    """The stretches between consecutive samples that both said online and were close together, merged."""
    out: list[tuple[datetime, datetime]] = []
    for a, b in zip(samples, samples[1:]):
        if a["online"] is True and b["online"] is True and close(a, b):
            if out and out[-1][1] >= a["at"]:
                out[-1] = (out[-1][0], b["at"])
            else:
                out.append((a["at"], b["at"]))
    return out


def _gaps(spans: list[tuple[datetime, datetime]], lo: datetime, hi: datetime) -> list[tuple[datetime, datetime]]:
    """The parts of [lo, hi] no span covers."""
    gaps, at = [], lo
    for a, b in spans:
        if b <= at:
            continue
        if a > at:
            gaps.append((at, min(a, hi)))
        at = max(at, b)
        if at >= hi:
            break
    if at < hi:
        gaps.append((at, hi))
    return [g for g in gaps if g[1] > g[0]]


def _every(samples: list[dict]) -> str:
    secs = max((float(s.get("every_s") or 0) for s in samples), default=0) or STATUS_DEFAULT_S
    m = secs / 60
    return f"{m:.0f} minutes" if m >= 1.5 else "minute" if m >= 0.75 else f"{secs:.0f} seconds"


def check(scan: datetime, visits: list[ClassifiedVisit], status: list[dict] | None,
          tolerance: timedelta, reach: timedelta = REACH) -> dict:
    """Coverage around `scan` (aware, shop local): its state, one sentence for the pack, and the facts.

    `status` holds Ring's device_online/device_offline notices (`notices`) and Shopfront's own status checks
    (StatusLog.samples, marked "sampled")."""
    tz = scan.tzinfo
    u = scan.astimezone(UTC)
    lo, hi = u - tolerance, u + tolerance
    status = status or []
    samples = sorted((n for n in status if n.get("sampled")), key=lambda n: n["at"].astimezone(UTC))
    status = [n for n in status if not n.get("sampled")]
    before = [n for n in status if n["at"].astimezone(UTC) <= lo]
    inside = [n for n in status if lo < n["at"].astimezone(UTC) <= hi]
    down = [n for n in ([before[-1]] if before else []) + inside if not n["online"]]
    around = sorted((v.event.timestamp for v in visits if abs(v.event.timestamp.astimezone(UTC) - u) <= reach),
                    key=lambda t: abs(t.astimezone(UTC) - u))
    utc_samples = [{**s, "at": s["at"].astimezone(UTC)} for s in samples]
    gaps = _gaps(_spans(utc_samples), lo, hi)
    near = [s for s in utc_samples if lo - timedelta(hours=1) <= s["at"] <= hi + timedelta(hours=1)]
    window = f"from {_hhmm(lo, tz)} to {_hhmm(hi, tz)}"
    facts = {"events_around": len(around), "reach_min": int(reach.total_seconds() // 60),
             "status_notices": len(status), "offline_from": None, "offline_until": None,
             "status_samples": len(near), "status_covered": bool(samples) and not gaps,
             "window": [lo.astimezone(tz).isoformat(timespec="minutes"), hi.astimezone(tz).isoformat(timespec="minutes")],
             "status_gaps": [[a.astimezone(tz).isoformat(timespec="minutes"), b.astimezone(tz).isoformat(timespec="minutes")]
                             for a, b in gaps] if samples else None}
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
    last_before = [s for s in utc_samples if s["at"] <= lo][-1:]
    off = [s for s in last_before + [s for s in utc_samples if lo < s["at"] <= hi] if s["online"] is False]
    if off:
        start = off[0]["at"]
        back = next((s["at"] for s in utc_samples if s["at"] > off[-1]["at"] and s["online"] is True), None)
        facts.update(offline_from=start.astimezone(tz).isoformat(timespec="minutes"),
                     offline_until=back.astimezone(tz).isoformat(timespec="minutes") if back else None)
        return {**facts, "state": CAMERA_DOWN,
                "text": f"Ring's status check said the doorbell was offline at {_hhmm(start, tz)}"
                        + (f" and online again at {_hhmm(back, tz)}" if back else "")
                        + f", so the camera may not have been watching at {_hhmm(scan, tz)}."}
    if samples and not gaps:
        return {**facts, "state": ONLINE_BY_STATUS,
                "text": f"Shopfront asked Ring every {_every(near)} whether the doorbell was online, and every "
                        f"answer {window} said online, so the camera was watching {window} "
                        f"({int(tolerance.total_seconds() // 60)} minutes either side of the {_hhmm(scan, tz)} "
                        f"scan)."}
    if samples:
        g0, g1 = gaps[0]
        missing = (f" Shopfront's status checks with Ring don't cover {_hhmm(g0, tz)} to {_hhmm(g1, tz)}"
                   + (f" (or {len(gaps) - 1} other gap{'' if len(gaps) == 2 else 's'})" if len(gaps) > 1 else "")
                   + ", so an outage then wouldn't show.")
    elif not status:
        missing = (" Shopfront has no Ring device_online/device_offline notices or status checks for this doorbell "
                   "around then, so an outage between door events wouldn't show.")
    else:
        missing = ""
    if around:
        n = len(around)
        return {**facts, "state": WATCHING,
                "text": f"The doorbell recorded {n} door event{'' if n == 1 else 's'} within "
                        f"{facts['reach_min']} minutes of {_hhmm(scan, tz)} (nearest at {_hhmm(around[0], tz)}), "
                        f"so it was recording around then.{missing}"}
    if before and before[-1]["online"]:
        return {**facts, "state": ONLINE_BY_RING,
                "text": f"The doorbell recorded nothing within {facts['reach_min']} minutes of {_hhmm(scan, tz)}, "
                        f"but Ring's last status notice before then said it was online (device_online at "
                        f"{_hhmm(before[-1]['at'], tz)}) and no device_offline followed."}
    return {**facts, "state": UNKNOWN,
            "text": f"The doorbell recorded nothing within {facts['reach_min']} minutes of {_hhmm(scan, tz)}, and "
                    f"nothing shows it was online {window}, so the camera may not have been watching."
                    + (missing if samples else "")}
