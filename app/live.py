"""Live-view sessions from Ring's WHEP stream (the Developer Playground's simulator plays here).

The browser shows the stream and posts a few JPEG frames per session. Each frame can go to
Bedrock; the session becomes one door visit, labelled by its most confident frame. Sessions are
appended to a JSON-lines log so the briefing can include them.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from app.classify.base import Classifier
from app.classify.bedrock import BedrockClassifier
from app.models import ClassifiedVisit, DoorEvent, VisitType

MAX_FRAMES_CLASSIFIED = 6  # Bedrock calls per session, at most
MAX_FRAME_BYTES = 2_000_000


class LiveError(Exception):
    def __init__(self, code: str, detail: str = ""):
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code  # UNKNOWN_SESSION | SESSION_CLOSED | BAD_FRAME


@dataclass
class LiveSession:
    id: str
    device_id: str
    started: datetime
    session_url: str
    ended: datetime | None = None
    frames_seen: int = 0
    labels: list[dict] = field(default_factory=list)

    def best(self) -> dict | None:
        known = [x for x in self.labels if x["type"] != VisitType.UNKNOWN.value]
        pool = known or self.labels
        return max(pool, key=lambda x: x["confidence"]) if pool else None

    def row(self) -> dict:
        best = self.best() or {"type": VisitType.UNKNOWN.value, "confidence": 0.0,
                               "reason": "no frames labelled", "source": "none", "request_id": None}
        return {"id": self.id, "device_id": self.device_id, "started": self.started.isoformat(),
                "ended": (self.ended or self.started).isoformat(), "frames_seen": self.frames_seen,
                "frames_labelled": len(self.labels), "label": best,
                "bedrock_request_ids": [x["request_id"] for x in self.labels if x.get("request_id")]}


class LiveLog:
    def __init__(self, data_dir: Path, tz: ZoneInfo):
        self.path = data_dir / "live-sessions.jsonl"
        self.frames_dir = data_dir / "live-frames"
        self.tz = tz
        self.active: dict[str, LiveSession] = {}

    def start(self, device_id: str, session_url: str) -> LiveSession:
        s = LiveSession(id=f"live-{uuid.uuid4().hex[:12]}", device_id=device_id,
                        started=datetime.now(self.tz), session_url=session_url)
        self.active[s.id] = s
        return s

    def get(self, session_id: str) -> LiveSession:
        if session_id not in self.active:
            raise LiveError("UNKNOWN_SESSION", session_id)
        return self.active[session_id]

    def frame(self, session_id: str, jpeg: bytes, classifier: Classifier,
              open_hours) -> dict:
        s = self.get(session_id)
        if not jpeg.startswith(b"\xff\xd8") or len(jpeg) > MAX_FRAME_BYTES:
            raise LiveError("BAD_FRAME", "expected a JPEG under 2 MB")
        s.frames_seen += 1
        if s.frames_seen == 1:
            self.frames_dir.mkdir(parents=True, exist_ok=True)
            (self.frames_dir / f"{s.id}.jpg").write_bytes(jpeg)
        if len(s.labels) >= MAX_FRAMES_CLASSIFIED:
            return {"session": s.id, "frames_seen": s.frames_seen, "label": s.best(), "classified": False}
        ev = DoorEvent(id=f"{s.id}-f{s.frames_seen}", device_id=s.device_id, timestamp=datetime.now(self.tz),
                       kind="live_view", source="ring-live", snapshot=jpeg)
        if isinstance(classifier, BedrockClassifier):
            v = classifier.classify(ev, open_hours, no_frame_reason="empty frame")
        else:  # time-of-day rules can't read a picture, so say so instead of guessing
            v = ClassifiedVisit(ev, VisitType.UNKNOWN, 0.0,
                                "frames need Bedrock to be labelled (CLASSIFIER is rules)", "rules")
        row = {"type": v.visit_type.value, "confidence": v.confidence, "reason": v.reason,
               "source": v.source, "request_id": v.request_id, "frame": s.frames_seen}
        s.labels.append(row)
        return {"session": s.id, "frames_seen": s.frames_seen, "label": row, "classified": True}

    def stop(self, session_id: str) -> dict:
        s = self.active.pop(session_id, None)
        if s is None:
            raise LiveError("UNKNOWN_SESSION", session_id)
        s.ended = datetime.now(self.tz)
        row = s.row()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a") as f:
            f.write(json.dumps(row) + "\n")
        return row

    def rows(self) -> list[dict]:
        done = [json.loads(x) for x in self.path.read_text().splitlines()] if self.path.exists() else []
        return done + [s.row() for s in self.active.values()]

    def visits_for_day(self, day: date) -> list[ClassifiedVisit]:
        out = []
        for r in self.rows():
            started = datetime.fromisoformat(r["started"]).astimezone(self.tz)
            if started.date() != day:
                continue
            lab = r["label"]
            ev = DoorEvent(id=r["id"], device_id=r["device_id"], timestamp=started, kind="live_view",
                           source="ring-live")
            out.append(ClassifiedVisit(ev, VisitType(lab["type"]), lab["confidence"],
                                       f"{lab['reason']} (live view, {r['frames_labelled']} frame(s) labelled)",
                                       lab["source"], lab.get("request_id")))
        return out
