"""Live-view sessions from Ring's WHEP stream (the Developer Playground's simulator plays here).

The browser shows the stream and posts a few JPEG frames per session. Each frame can go to
Bedrock; the session becomes one door visit, labelled by its most confident frame. Sessions are
appended to a JSON-lines log so the briefing can include them.

What a live frame is, exactly: the owner's browser draws Ring's WebRTC video onto a canvas and posts
the JPEG. Shopfront can't tell those bytes apart from any other JPEG, so a live frame is never called a
"Ring frame". Its provenance is `BROWSER_FRAME`, its door event source is `ring-live-browser`, and
only the browser that started the session (it holds the session's key) can post frames to it. Ring's
own image download (`latest_in_range`) would be better evidence, but a Developer Playground token gets
403 FORBIDDEN there (docs/ring.md, X-Request-ID 843c63da-…), so it isn't used.

Sessions that are never stopped (a closed tab) expire after `SESSION_TTL` and are logged as expired.
"""

from __future__ import annotations

import hmac
import json
import secrets
import threading
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from app.classify.base import Classifier
from app.classify.bedrock import BedrockClassifier
from app.files import private_dir, read_jsonl, write_private
from app.models import ClassifiedVisit, DoorEvent, VisitType

MAX_FRAMES_CLASSIFIED = 6  # Bedrock calls per session, at most
MAX_FRAME_BYTES = 2_000_000
MAX_ACTIVE = 4  # live sessions open at once
SESSION_TTL = timedelta(minutes=5)  # the page itself stops at 58 s, before Ring's 60 s limit
SOURCE = "ring-live-browser"
BROWSER_FRAME = "captured in the owner's browser from a Ring live-view session"


def frame_provenance(event_id: str) -> str | None:
    """How a frame Shopfront holds was captured, for the frames that need saying: live-view ones."""
    return BROWSER_FRAME if event_id.startswith("live-") else None


class LiveError(Exception):
    def __init__(self, code: str, detail: str = ""):
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code  # UNKNOWN_SESSION | BAD_FRAME | BAD_KEY | TOO_MANY_SESSIONS


@dataclass
class LiveSession:
    id: str
    device_id: str
    started: datetime
    session_url: str
    key: str = ""  # the browser that started it proves itself with this on every frame
    ended: datetime | None = None
    frames_seen: int = 0
    labels: list[dict] = field(default_factory=list)
    expired: bool = False

    def best(self) -> dict | None:
        known = [x for x in self.labels if x["type"] != VisitType.UNKNOWN.value]
        pool = known or self.labels
        return max(pool, key=lambda x: x["confidence"]) if pool else None

    def row(self) -> dict:
        best = self.best() or {"type": VisitType.UNKNOWN.value, "confidence": 0.0,
                               "reason": "no frames labelled", "source": "none", "request_id": None}
        out = {"id": self.id, "device_id": self.device_id, "started": self.started.isoformat(),
               "ended": (self.ended or self.started).isoformat(), "frames_seen": self.frames_seen,
               "frames_labelled": len(self.labels), "label": best, "frame_provenance": BROWSER_FRAME,
               "bedrock_request_ids": [x["request_id"] for x in self.labels if x.get("request_id")]}
        if self.expired:
            out["expired"] = True
        return out


class LiveLog:
    def __init__(self, data_dir: Path, tz: ZoneInfo):
        self.path = data_dir / "live-sessions.jsonl"
        self.frames_dir = data_dir / "live-frames"
        self.tz = tz
        self.active: dict[str, LiveSession] = {}
        self._lock = threading.Lock()

    def _expire(self) -> None:
        now = datetime.now(self.tz)
        for s in [s for s in self.active.values() if now - s.started > SESSION_TTL]:
            s.expired = True
            self._finish(s, s.started + SESSION_TTL)

    def start(self, device_id: str, session_url: str) -> LiveSession:
        with self._lock:
            self._expire()
            if len(self.active) >= MAX_ACTIVE:
                raise LiveError("TOO_MANY_SESSIONS", "stop a live view before starting another")
            s = LiveSession(id=f"live-{uuid.uuid4().hex[:12]}", device_id=device_id, started=datetime.now(self.tz),
                            session_url=session_url, key=secrets.token_urlsafe(24))
            self.active[s.id] = s
            return s

    def get(self, session_id: str) -> LiveSession:
        with self._lock:
            self._expire()
            if session_id not in self.active:
                raise LiveError("UNKNOWN_SESSION", session_id)
            return self.active[session_id]

    def frame(self, session_id: str, jpeg: bytes, classifier: Classifier, open_hours, key: str | None = None) -> dict:
        s = self.get(session_id)
        if not key or not hmac.compare_digest(key, s.key):
            raise LiveError("BAD_KEY", "frames come only from the browser that started this live view")
        if not jpeg.startswith(b"\xff\xd8") or len(jpeg) > MAX_FRAME_BYTES:
            raise LiveError("BAD_FRAME", "expected a JPEG under 2 MB")
        s.frames_seen += 1
        private_dir(self.frames_dir)
        if s.frames_seen == 1:
            write_private(self.frames_dir / f"{s.id}.jpg", jpeg)
        if len(s.labels) >= MAX_FRAMES_CLASSIFIED:
            return {"session": s.id, "frames_seen": s.frames_seen, "label": s.best(), "classified": False}
        # keep every frame Bedrock is asked about, so the labelled evaluation set is the real input
        write_private(self.frames_dir / f"{s.id}-f{s.frames_seen}.jpg", jpeg)
        ev = DoorEvent(id=f"{s.id}-f{s.frames_seen}", device_id=s.device_id, timestamp=datetime.now(self.tz),
                       kind="live_view", source=SOURCE, snapshot=jpeg)
        if isinstance(classifier, BedrockClassifier):
            v = classifier.classify(ev, open_hours, no_frame_reason="empty frame")
        else:  # time-of-day rules can't read a picture, so say so instead of guessing
            v = ClassifiedVisit(ev, VisitType.UNKNOWN, 0.0,
                                "frames need Bedrock to be labelled (CLASSIFIER is rules)", "rules")
        row = {"type": v.visit_type.value, "confidence": v.confidence, "reason": v.reason,
               "source": v.source, "request_id": v.request_id, "frame": s.frames_seen}
        s.labels.append(row)
        return {"session": s.id, "frames_seen": s.frames_seen, "label": row, "classified": True}

    def _finish(self, s: LiveSession, ended: datetime) -> dict:
        self.active.pop(s.id, None)
        s.ended = ended
        row = s.row()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a") as f:
            f.write(json.dumps(row) + "\n")
        return row

    def stop(self, session_id: str) -> dict:
        with self._lock:
            self._expire()
            s = self.active.get(session_id)
            if s is None:
                raise LiveError("UNKNOWN_SESSION", session_id)
            return self._finish(s, datetime.now(self.tz))

    def rows(self) -> list[dict]:
        with self._lock:
            self._expire()
            return read_jsonl(self.path) + [s.row() for s in self.active.values()]

    def visits_for_day(self, day: date) -> list[ClassifiedVisit]:
        out = []
        for r in self.rows():
            try:
                started = datetime.fromisoformat(r["started"]).astimezone(self.tz)
                if started.date() != day or not r["frames_seen"]:
                    continue  # a session that never delivered video isn't a visit
                lab = r["label"]
                ev = DoorEvent(id=r["id"], device_id=r["device_id"], timestamp=started, kind="live_view",
                               source=SOURCE)
                out.append(ClassifiedVisit(ev, VisitType(lab["type"]), lab["confidence"],
                                           f"{lab['reason']} (live view, {r['frames_labelled']} frame(s) labelled)",
                                           lab["source"], lab.get("request_id")))
            except (KeyError, TypeError, ValueError):
                continue  # a row from a damaged log isn't a visit
        return out
