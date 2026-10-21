"""Guided live proof: a Ring Developer Playground scene, end to end, with every request ID.

Event History (on_demand) -> the scene's frame (whole-second at_timestamp) -> Bedrock's label -> a "business
closed" check at that time -> a signed dispute pack zip.

Run it on the shop's Mac with a fresh Playground access token (they last about 30 minutes):

    RING_ACCESS_TOKEN=<token> .venv/bin/python -m scripts.ring_proof

It walks through seven steps and tells you when to act:

1. Devices and status: GET /v1/devices (the camera), GET /v1/devices/{id}/status, GET /v1/users/me.
2. A new door event: it reads Event History, asks you to press a scene (Package, Vehicle or Motion) for the
   test doorbell in the Playground, then polls Event History until Ring files the new event. A Playground
   scene plays a recorded clip over live view, so Ring files it as `on_demand` (API reference, Event
   History > Event types: "User or partner initiated a live view session"), usually about 30 s after the
   clip ends.
3. The frame: POST /v1/devices/{id}/media/image/download, {"type": "at_timestamp"} a few seconds into that
   event, in whole seconds, retrying 416/425 while Ring is still processing the recording (API reference,
   Media Clips > Download video after a webhook event). A Playground token can only download media recorded
   after it was issued, so the script records the token's issue time when it starts (the token's `iat` when it
   is a JWT, else the moment the script started, which is no earlier) and never asks for a frame before it.
4. The label: the frame goes to Amazon Bedrock through the same forced-tool classifier the app uses
   (app/classify/bedrock.py). Skip with --no-bedrock.
5. The check: a carrier's "business closed" scan at the scene's minute, checked the way the desk checks it.
   The day is read again through classify_day in Playground mode (docs/ring.md, section 11), so the scene is a
   door visit labelled "Playground scene (Ring simulator)", or what Bedrock said from its frame (the label from
   step 4, reused from the cache; other scenes that day aren't sent to Bedrock), then reconcile.check_closed_scan
   against the posted hours (SHOP_HOURS).
6. The pack: dispute.build_pack and as_zip on that check, with the scene's frame, signed (manifest.sig) with the
   app's pack key (app/pack_signing.py). The zip holds the frame and raw IDs, so it goes to evidence/raw/ too.
7. The record: evidence/ring-proof-<utc>.json holds every call's HTTP status and X-Request-ID, the frame's
   size, sha256 and Ring media headers, Bedrock's RequestId, the check's outcome and the pack's SHA-256s, and a
   `chain` naming the request ID behind each link. Tokens are never written; device, account and event IDs
   are short sha256 hashes. The frame itself goes to evidence/raw/, which git ignores: never commit it.

--dry-run runs the same steps against an httpx MockTransport built from the API reference's documented
response shapes and a fake Bedrock, without prompts or waiting, and writes its record to a temporary
folder (or --out), marked as a dry run. Mock calls are never evidence, so a dry run never writes evidence/.

Without RING_ACCESS_TOKEN it uses the token pasted on /connect (.data/ring-access-token), or the refresh
token in .env.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import re
import sys
import tempfile
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

import httpx

from app.config import settings
from app.ring.api import API_BASE, RingApiClient, RingAuth, RingError, RingStatus

OUT = Path("evidence")
EVENT_TYPES = "ding,motion,on_demand"
WAIT_FOR_EVENT = 240.0  # seconds to poll Event History for the new event
POLL_EVERY = 10.0
FRAME_RETRY = 120.0  # Ring's own limit: stop retrying 2 minutes after the requested time
LOOKBACK = timedelta(minutes=2)  # an event that started just before Enter was pressed still counts


def h(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()[:12]


class Redactor:
    """Hashes Ring's directed IDs (devices, accounts, events) and drops pre-signed query strings."""

    def __init__(self) -> None:
        self.secret_ids: dict[str, str] = {}  # raw id -> label, for ids whose format we can't pattern-match

    def add(self, value: str | None, label: str) -> str | None:
        if not value:
            return None
        self.secret_ids[value] = f"<{label}:{h(value)}>"
        return h(value)

    def url(self, url: str) -> str:
        url = url.replace(API_BASE, "")
        if "security_token=" in url or "X-Amz-" in url:  # a pre-signed media URL: host and path only
            url = url.split("?", 1)[0]
        for raw, label in self.secret_ids.items():
            url = url.replace(raw, label)
        return re.sub(r"ava1\.ring\.(device|account|user)\.[A-Za-z0-9_.-]+",
                      lambda m: f"<{m.group(1)}:{h(m.group(0))}>", url)


class CallLog:
    """An httpx response hook: method, redacted URL, status and X-Request-ID of every call, in order."""

    def __init__(self, redactor: Redactor) -> None:
        self.calls: list[dict] = []
        self.redactor = redactor

    def __call__(self, resp: httpx.Response) -> None:
        req = resp.request
        self.calls.append({"method": req.method, "url": str(req.url), "status": resp.status_code,
                           "x_request_id": resp.headers.get("X-Request-ID"),
                           "at": datetime.now(timezone.utc).isoformat(timespec="seconds")})

    def redacted(self) -> list[dict]:
        return [{**c, "url": self.redactor.url(c["url"])} for c in self.calls]


def _error(exc: RingError) -> dict:
    return {"status": exc.status.value, "http_status": exc.http_status, "code": exc.code,
            "request_id": exc.request_id}


def say(text: str = "") -> None:
    print(text, flush=True)


def _last_history_rid(calls: CallLog) -> str | None:
    """The X-Request-ID of the latest Event History call: the one whose answer held the new event."""
    return next((c["x_request_id"] for c in reversed(calls.calls) if "/v1/history/" in c["url"]), None)


def check_and_pack(ring: RingApiClient, classifier, event, frame_bytes: bytes, label, *, work: Path, signer,
                   now: datetime, playground: bool) -> tuple[dict, dict, bytes]:
    """Steps 5 and 6: the day through classify_day (the scene's label reused from the cache, no second Bedrock
    call), a "business closed" check at the scene's minute, and the signed pack zip. Returns (check, pack, zip)."""
    from app import dispute, pack_signing
    from app import reconcile as rc
    from app.briefing import classify_day
    from app.cache import ClassificationCache
    from app.classify.rules import RulesClassifier
    from app.deliveries import FrameStore
    from app.hours import parse_hours, posted_hours

    hours = parse_hours(settings.shop_hours)
    local = event.timestamp.astimezone(settings.tz)
    day = local.date()
    cache = ClassificationCache(work / "classifications.json")
    frames = FrameStore(work)
    if label is not None and label.source == "bedrock":  # what Bedrock said in step 4, as the app would cache it
        cache.put(event.id, {"type": label.visit_type.value, "confidence": label.confidence, "reason": label.reason,
                             "model": classifier.model_id, "request_id": label.request_id})
        frames.save(event.id, frame_bytes)
    clf = classifier if classifier is not None else RulesClassifier()
    visits = classify_day(ring, clf, day, hours, cache, frames=frames, playground=playground,
                          focus=lambda e: "the scene this proof pressed" if e.id == event.id else None)
    if not frames.path(event.id):
        frames.save(event.id, frame_bytes)  # the pack's receipt: the frame Ring sent for this scene
    oh = posted_hours(hours, day)
    scan = local.replace(second=0, microsecond=0)
    check = rc.check_closed_scan(scan, visits, oh, now=now)
    pack = dispute.build_pack(settings.shop_name, day, visits, frames,
                              f"{oh[0]:%H:%M}–{oh[1]:%H:%M}" if oh else "closed", check, tz=settings.tz, made_at=now)
    body = pack_signing.sign_zip(dispute.as_zip(pack, frames), signer,
                                 f"shopfront-dispute-{day.isoformat()}-{scan:%H%M}.zip")
    return check, pack, body


def run(ring: RingApiClient, classifier, *, calls: CallLog, ask: Callable[[str], str],
        sleep: Callable[[float], None], clock: Callable[[], float], raw_dir: Path | None,
        wait_for: float = WAIT_FOR_EVENT, poll_every: float = POLL_EVERY, signer=None,
        pack_dir: Path | None = None, playground: bool = True) -> dict:
    """The steps before the record. Returns the record (with an "error" key when a step failed)."""
    red = calls.redactor
    record: dict = {"captured_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    "auth_mode": ring.auth.mode}
    if ring.auth.issued_at:  # media before this can't be downloaded with a Playground token
        record["token_issued_utc"] = datetime.fromtimestamp(ring.auth.issued_at, timezone.utc).isoformat(
            timespec="seconds")
        record["token_issued_from"] = ring.auth.issued_from
    try:
        say("Step 1 of 7: your Ring devices")
        devices = ring.list_devices()
        for d in devices:
            red.add(d.get("id"), "device")
        record["devices"] = [{"id_hash": h(d["id"]), "type": d.get("type"),
                              "attributes_keys": sorted((d.get("attributes") or {}).keys())} for d in devices]
        device = ring.device_id
        red.add(device, "device")
        record["device_id_hash"] = h(device)
        record["online"] = ring.online()
        record["account_id_hash"] = red.add(ring.account_id(), "account")
        say(f"  {len(devices)} device(s); using camera {h(device)} (hashed), online: {record['online']}")
        if ring.auth.mode == "playground":
            floor = datetime.fromtimestamp(ring.auth.media_floor_ms() / 1000, timezone.utc)
            say(f"  This Playground token can download frames recorded from {floor:%H:%M:%S} UTC "
                f"({ring.auth.issued_from}); the scene you press next is after that.")

        say("\nStep 2 of 7: a new event in Ring's Event History")
        start = datetime.fromtimestamp(clock(), timezone.utc)
        before = {e.id for e in ring.recent_events(start - LOOKBACK)}
        say(f"  Event History has {len(before)} event(s) from the last few minutes.")
        ask("  Now open the Ring Developer Playground, pick the test doorbell and press a scene\n"
            "  (Package, Vehicle or Motion). Let the clip play, then press Enter here... ")
        pressed = datetime.fromtimestamp(clock(), timezone.utc)
        deadline = clock() + wait_for
        new = []
        while True:
            new = [e for e in ring.recent_events(pressed - LOOKBACK) if e.id not in before]
            if new or clock() + poll_every > deadline:
                break
            say(f"  nothing new yet ({round(clock() - pressed.timestamp())} s); Ring files a live view about "
                f"30 s after it ends. Checking again in {poll_every:.0f} s.")
            sleep(poll_every)
        record["waited_s"] = round(clock() - pressed.timestamp())
        if not new:
            record["error"] = {"status": "NO_NEW_EVENT",
                               "detail": f"no new Event History item within {wait_for:.0f} s of pressing Enter"}
            say(f"  No new event after {wait_for:.0f} s. Press a scene again and re-run.")
            return record
        event = new[-1]
        record["event"] = {"id_hash": red.add(event.id, "event"), "event_type": event.kind,
                           "start_utc": event.timestamp.astimezone(timezone.utc).isoformat(timespec="seconds"),
                           "end_utc": event.ends.astimezone(timezone.utc).isoformat(timespec="seconds")
                           if event.ends else None,
                           "new_events": len(new)}
        say(f"  Found it: {'an' if event.kind[0] in 'aeiou' else 'a'} {event.kind} event at {event.timestamp:%H:%M:%S} "
            f"({'a live view: what a Playground scene files' if event.kind == 'on_demand' else 'a door event'}).")

        say("\nStep 3 of 7: the frame")
        frame = ring.frame(event, retry_for=FRAME_RETRY)
        record["frame"] = {"requested_utc": datetime.fromtimestamp(frame.requested_ms / 1000, timezone.utc)
                           .isoformat(timespec="seconds"),
                           "offset_into_event_ms": frame.requested_ms - int(event.timestamp.timestamp() * 1000),
                           "bytes": len(frame.content), "sha256": hashlib.sha256(frame.content).hexdigest(),
                           "content_type": frame.content_type, "x_media_timestamp": frame.media_timestamp,
                           "x_media_origin": frame.media_origin, "x_request_id": frame.request_id,
                           "attempts": frame.attempts, "whole_second": frame.requested_ms % 1000 == 0}
        chain = record["chain"] = {"event_history_x_request_id": _last_history_rid(calls),
                                   "frame_x_request_id": frame.request_id}
        if raw_dir is not None:
            raw_dir.mkdir(parents=True, exist_ok=True)
            path = raw_dir / f"ring-frame-{pressed:%Y%m%dT%H%M%SZ}.jpg"
            path.write_bytes(frame.content)
            say(f"  Saved {len(frame.content)} bytes to {path} (git ignores it: never commit a frame).")
        else:
            say(f"  Got {len(frame.content)} bytes (dry run: not saved).")

        say("\nStep 4 of 7: Bedrock's label")
        v = None
        if classifier is None:
            say("  Skipped (--no-bedrock).")
        else:
            from app.hours import parse_hours, posted_hours

            event.snapshot = frame.content
            v = classifier.classify(event, posted_hours(parse_hours(settings.shop_hours), event.timestamp.date()))
            event.snapshot = None
            record["bedrock"] = {"model": getattr(classifier, "model_id", None), "labelled_by": v.source,
                                 "label": v.visit_type.value if v.source == "bedrock" else None,
                                 "confidence": v.confidence if v.source == "bedrock" else None,
                                 "reason": v.reason, "request_id": v.request_id}
            chain["bedrock_request_id"] = v.request_id
            if v.source != "bedrock":
                record["error"] = {"status": "BEDROCK_NO_LABEL", "detail": v.reason}
                say(f"  Bedrock didn't label it: {v.reason}")
                return record
            say(f"  {v.visit_type.value} ({v.confidence:.2f}): {v.reason}  [RequestId {v.request_id}]")

        say('\nStep 5 of 7: a "business closed" check at that time')
        if signer is None:
            from app.pack_signing import MemorySigner

            signer = MemorySigner()
        history_calls = sum(1 for c in calls.calls if "/v1/history/" in c["url"])
        with tempfile.TemporaryDirectory(prefix="ring-proof-pack-") as work:
            check, pack, body = check_and_pack(ring, classifier, event, frame.content, v, work=Path(work),
                                               signer=signer, now=datetime.fromtimestamp(clock(), settings.tz),
                                               playground=playground)
        visit = check.get("visit") or {}
        record["check"] = {"claim": check["claim"], "scan_local": check["attempt"], "posted_hours": check["posted_hours"],
                           "playground_mode": playground, "outcome": check["outcome"], "text": check["text"],
                           "activity": check.get("activity"),
                           "cited": {"label": visit.get("type"), "labelled_by": visit.get("labelled_by"),
                                     "event_source": visit.get("event_source"),
                                     "bedrock_request_id": visit.get("bedrock_request_id"),
                                     "is_this_scene": visit.get("event_id") == event.id} if visit else None,
                           "event_history_x_request_ids": [c["x_request_id"] for c in calls.calls
                                                           if "/v1/history/" in c["url"]][history_calls:]}
        chain["check_outcome"] = check["outcome"]
        say(f"  {check['outcome']}: {check['text']}")

        say("\nStep 6 of 7: the dispute pack")
        names = zipfile.ZipFile(io.BytesIO(body)).namelist()
        record["pack"] = {"zip_sha256": hashlib.sha256(body).hexdigest(), "zip_bytes": len(body),
                          "manifest_sha256": hashlib.sha256(zipfile.ZipFile(io.BytesIO(body)).read(
                              "manifest.sha256")).hexdigest(), "files": names,
                          "key_id": signer.jwk()["kid"], "signer": signer.name, "watermark": pack["watermark"],
                          "event_sources": pack["event_sources"],
                          "frame_sha256": (pack["frame"] or {}).get("sha256"),
                          "frame_is_step_3s": (pack["frame"] or {}).get("sha256") == record["frame"]["sha256"]}
        chain["pack_zip_sha256"] = record["pack"]["zip_sha256"]
        if pack_dir is not None:
            pack_dir.mkdir(parents=True, exist_ok=True)
            path = pack_dir / f"ring-proof-pack-{pressed:%Y%m%dT%H%M%SZ}.zip"
            path.write_bytes(body)
            say(f"  Saved the signed pack ({len(body)} bytes) to {path}"
                + (": it holds the frame and raw IDs, so it stays out of git." if raw_dir is not None else "."))

    except RingError as exc:
        record["error"] = _error(exc)
        if exc.status == RingStatus.BEFORE_TOKEN:  # never asked: say why in words
            say(f"  {exc.detail} Press a scene again and re-run.")
        else:
            say(f"  Ring said {exc.status.value}" + (f" ({exc.code})" if exc.code else "")
                + f", X-Request-ID {exc.request_id}.")
    return record


# --- dry run: the documented shapes, no network ---------------------------------------------------------
DRY_DEVICE = "ava1.ring.device.DRYRUN"
DRY_ACCOUNT = "ava1.ring.account.DRYRUN"
CLIP_S = 45.0  # a Playground scene's clip, played between the prompt and Enter


def dry_start() -> float:
    """The dry run's clock: 12:40 shop time on the latest Wednesday before today, inside the default posted
    hours, so the check's outcome doesn't depend on when the test runs."""
    today = datetime.now(settings.tz).date()
    wed = today - timedelta(days=(today.weekday() - 2) % 7 or 7)
    return datetime.combine(wed, datetime.min.time(), settings.tz).replace(hour=12, minute=40).timestamp()


class FakeClock:
    def __init__(self, start: float | None = None) -> None:
        self.now = datetime.now(timezone.utc).timestamp() if start is None else start

    def time(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


def dry_world(clock: FakeClock) -> tuple[Callable[[httpx.Request], httpx.Response], Callable[[str], str], dict]:
    """A MockTransport handler shaped like the API reference, and the "press Enter" that presses a scene.
    The scene's on_demand event is filed one poll later; its frame is RECORDING_NOT_READY once."""
    state: dict = {"pressed_at": None, "polls_after": 0, "frame_asks": 0, "image_bodies": []}
    n = {"rid": 0}

    def rid() -> dict:
        n["rid"] += 1
        return {"X-Request-ID": f"dry-run-{n['rid']:04d}"}

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if req.url.host != "api.amazonvision.com":  # the pre-signed media URL
            return httpx.Response(200, content=b"\xff\xd8\xff\xe0dry-run-jpeg",
                                  headers={"content-type": "image/jpeg", "X-Media-Origin": "recording",
                                           "X-Media-Timestamp": str(state["frame_ms"]), **rid()})
        if path == "/v1/devices":
            return httpx.Response(200, headers=rid(), json={
                "data": [{"type": "devices", "id": DRY_DEVICE, "attributes": {"name": "Playground doorbell"},
                          "relationships": {"capabilities": {"data": {"type": "device-capabilities", "id": "c1"}}}}],
                "included": [{"type": "device-capabilities", "id": "c1", "attributes": {"video": {
                    "configurations": ["resolution_mode"], "codecs": ["AVC"], "ratio": "16:9",
                    "max_resolution": 1080, "supported_resolutions": [1080]}}}]})
        if path == f"/v1/devices/{DRY_DEVICE}/status":
            return httpx.Response(200, headers=rid(), json={"data": {"type": "device-status", "id": "s1",
                                                                     "attributes": {"online": True}}})
        if path == "/v1/users/me":
            return httpx.Response(200, headers=rid(), json={"data": {"type": "users", "id": DRY_ACCOUNT}})
        if path == f"/v1/history/devices/{DRY_DEVICE}/events":
            items = []
            if state["pressed_at"] is not None:
                state["polls_after"] += 1
                if state["polls_after"] > 1:  # filed a little after the clip ends
                    start = int(state["pressed_at"] * 1000) - 31_000
                    items = [{"type": "history-events", "id": "dry-run-event-1",
                              "attributes": {"event_type": "on_demand", "is_third_party_reviewed": False,
                                             "start": start, "end": start + 31_742},
                              "relationships": {"source": {"data": {"type": "devices", "id": DRY_DEVICE}}}}]
            return httpx.Response(200, headers=rid(), json={"data": items, "links": {}})
        if path == f"/v1/devices/{DRY_DEVICE}/media/image/download":
            body = json.loads(req.content)
            state["image_bodies"].append(body)
            state["frame_ms"] = body["timestamp"]
            state["frame_asks"] += 1
            if state["frame_asks"] == 1:
                return httpx.Response(425, headers=rid(), json={"errors": [{
                    "status": "425", "code": "RECORDING_NOT_READY",
                    "detail": "The request was made before any recording was available"}]})
            return httpx.Response(303, headers={"Location": "https://download-us.dry-run.test/v1/download"
                                                "?security_token=x&req_type=image_download", **rid()})
        return httpx.Response(404, headers=rid(), json={"errors": [{"status": "404", "code": "DEVICE_NOT_FOUND"}]})

    def press(prompt: str) -> str:
        say(prompt + "(dry run: pressing a scene)")
        clock.sleep(CLIP_S)  # the clip plays before Enter is pressed
        state["pressed_at"] = clock.time()
        return ""

    return handler, press, state


class DryBedrock:
    """A Converse stand-in that answers through the forced tool, as Bedrock does."""

    def converse(self, **kw):
        from app.classify.bedrock import TOOL_NAME

        return {"output": {"message": {"role": "assistant", "content": [{"toolUse": {
                    "toolUseId": "dry", "name": TOOL_NAME,
                    "input": {"type": "delivery", "confidence": 0.9, "reason": "dry run: no model was called"}}}]}},
                "stopReason": "tool_use", "ResponseMetadata": {"RequestId": "dry-run-bedrock-0001"}}


def _token() -> str:
    pasted = Path(settings.data_dir) / "ring-access-token"
    return settings.ring_access_token or (pasted.read_text().strip() if pasted.exists() else "")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Guided live proof against the Ring Developer Playground.")
    p.add_argument("--dry-run", action="store_true",
                   help="run every step against a MockTransport and a fake Bedrock; writes no evidence")
    p.add_argument("--no-bedrock", action="store_true", help="skip step 4 (the Bedrock label)")
    p.add_argument("--bedrock", action="store_true", help=argparse.SUPPRESS)  # the old flag: Bedrock is now the default
    p.add_argument("--wait", type=float, default=WAIT_FOR_EVENT,
                   help=f"seconds to wait for the new event (default {WAIT_FOR_EVENT:.0f})")
    p.add_argument("--out", type=Path, default=None,
                   help="folder for the record (default evidence/, or a temporary folder with --dry-run)")
    args = p.parse_args(argv)

    redactor = Redactor()
    calls = CallLog(redactor)
    if args.dry_run:
        clock = FakeClock(dry_start())
        handler, ask, _ = dry_world(clock)
        http = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True,
                            event_hooks={"response": [calls]})
        auth = RingAuth(access_token="dry-run-token", http=http, issued_at=clock.time(),
                        issued_from="when this script started")
        sleep, now = clock.sleep, clock.time
        out = args.out or Path(tempfile.mkdtemp(prefix="ring-proof-dry-run-"))
        raw_dir = None
        pack_dir = out
        bedrock_client = DryBedrock()
    else:
        access = _token()
        if not (access or settings.ring_refresh_token):
            say("No Ring token. In the Ring Developer Playground, copy the access token and run:\n"
                "  RING_ACCESS_TOKEN=<token> .venv/bin/python -m scripts.ring_proof")
            return 2
        http = httpx.Client(timeout=30, follow_redirects=True, event_hooks={"response": [calls]})
        import time as _time

        started = _time.time()  # the token was issued no later than this: its media floor unless it says (iat)
        try:
            auth = RingAuth(access_token=access, refresh_token="" if access else settings.ring_refresh_token,
                            client_id=settings.ring_client_id, client_secret=settings.ring_client_secret, http=http,
                            issued_at=started, issued_from="when this script started")
        except RingError as exc:
            say(f"Ring isn't configured: {exc.detail}")
            return 2
        sleep, now, ask = _time.sleep, _time.time, input
        out = args.out or OUT
        raw_dir = pack_dir = out / "raw"
        bedrock_client = None

    ring = RingApiClient(auth=auth, tz=settings.tz, device_id="" if args.dry_run else settings.ring_device_id,
                         event_types=EVENT_TYPES, http=http)
    ring.sleep, ring.clock = sleep, now
    classifier = None
    if not args.no_bedrock:
        from app.classify.bedrock import BedrockClassifier

        classifier = BedrockClassifier(settings.aws_region, settings.bedrock_model_id, client=bedrock_client,
                                       record_path=None if args.dry_run
                                       else Path(settings.data_dir) / "bedrock-calls.jsonl")

    from app.briefing import playground_mode
    from app.pack_signing import MemorySigner, make_signer

    try:  # the app's own pack key (a KMS key when SHOPFRONT_SIGNING=kms), so the pack checks against its URL
        signer = MemorySigner() if args.dry_run else make_signer(settings, Path(settings.data_dir))
    except ValueError as exc:
        say(f"Pack signing isn't configured: {exc}")
        return 2
    record = run(ring, classifier, calls=calls, ask=ask, sleep=sleep, clock=now, raw_dir=raw_dir,
                 wait_for=args.wait, signer=signer, pack_dir=pack_dir,
                 playground=playground_mode(ring, settings.ring_playground))
    record["calls"] = calls.redacted()
    if args.dry_run:
        record = {"dry_run": True, "note": "MockTransport and a fake Bedrock: not evidence of anything live",
                  **record}

    say("\nStep 7 of 7: the record")
    out.mkdir(parents=True, exist_ok=True)
    stamp = datetime.fromtimestamp(now(), timezone.utc)
    path = out / f"ring-proof-{stamp:%Y%m%dT%H%M%SZ}.json"
    text = json.dumps(record, indent=2) + "\n"
    leaked = [raw for raw in redactor.secret_ids if raw in text]
    if leaked:  # belt and braces: CLAUDE.md, never an unhashed device or account ID in evidence/
        say("  Refusing to write: an unhashed Ring ID would be in the record.")
        return 3
    path.write_text(text)
    ids = [c["x_request_id"] for c in record["calls"] if c.get("x_request_id")]
    say(f"  Wrote {path}: {len(record['calls'])} Ring calls, {len(ids)} X-Request-IDs"
        + (f", Bedrock RequestId {record['bedrock']['request_id']}" if (record.get("bedrock") or {}).get("request_id")
           else "") + ".")
    for link, value in (record.get("chain") or {}).items():
        say(f"    {link}: {value}")
    if "error" in record:
        say(f"  Not complete: {record['error']['status']}.")
        return 1
    if not args.dry_run:
        say("  Commit that JSON file (never the frame in evidence/raw/), and add its IDs to the README proof table.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
