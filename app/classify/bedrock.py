"""Amazon Bedrock vision classifier (Converse API with an image block).

Needs the event's JPEG frame (fetched from Ring by the briefing). With no frame, or when a call
fails, the event is classified by the rules and the reason says why, so nothing is silently mixed.
"""

from __future__ import annotations

import json
import logging
import time as clock
from datetime import datetime, time, timezone
from pathlib import Path

import boto3

from app.classify.base import Classifier
from app.classify.rules import RulesClassifier
from app.models import ClassifiedVisit, DoorEvent, VisitType

log = logging.getLogger(__name__)

PROMPT = """You are looking at a frame from a small shop's front-door camera.
Event: {kind} at {ts} (shop local time). Posted hours today: {hours}.
Classify the visit as exactly one of: customer, delivery, staff, after_hours, unknown.
- delivery: courier uniform, parcel, hand truck, delivery vehicle
- staff: someone unlocking the door, wearing the shop apron/uniform, arriving before open
- after_hours: activity while the shop is closed that is not staff or a delivery
- unknown: nobody visible, or you can't tell
Reply with JSON only: {{"type": "...", "confidence": 0.0-1.0, "reason": "short"}}"""


class BedrockClassifier(Classifier):
    name = "bedrock"

    def __init__(self, region: str, model_id: str, client=None, record_path: Path | None = None):
        self.model_id = model_id
        self.client = client or boto3.client("bedrock-runtime", region_name=region)
        self.fallback = RulesClassifier()
        self.record_path = record_path  # one JSON line per call: the raw evidence behind each label

    def _record(self, row: dict) -> None:
        if not self.record_path:
            return
        try:
            self.record_path.parent.mkdir(parents=True, exist_ok=True)
            with self.record_path.open("a") as f:
                f.write(json.dumps(row) + "\n")
        except OSError as exc:  # evidence logging must never break classification
            log.warning("could not record Bedrock call: %s", exc)

    def classify(self, event: DoorEvent, open_hours: tuple[time, time] | None,
                 no_frame_reason: str = "no frame") -> ClassifiedVisit:
        if not event.snapshot:
            return self.fallback.classify(event, open_hours, note=no_frame_reason)

        hours = f"{open_hours[0]:%H:%M}-{open_hours[1]:%H:%M}" if open_hours else "closed"
        prompt = PROMPT.format(kind=event.kind, ts=event.timestamp.isoformat(), hours=hours)
        rec = {"at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "event_id": event.id,
               "model_id": self.model_id, "frame_bytes": len(event.snapshot)}
        started = clock.perf_counter()
        try:
            resp = self.client.converse(
                modelId=self.model_id,
                messages=[{"role": "user", "content": [
                    {"image": {"format": "jpeg", "source": {"bytes": event.snapshot}}},
                    {"text": prompt},
                ]}],
                inferenceConfig={"maxTokens": 200, "temperature": 0},
            )
            request_id = resp.get("ResponseMetadata", {}).get("RequestId")
            rec.update(wall_ms=round((clock.perf_counter() - started) * 1000), request_id=request_id,
                       latency_ms=resp.get("metrics", {}).get("latencyMs"), usage=resp.get("usage"),
                       stop_reason=resp.get("stopReason"))
            text = resp["output"]["message"]["content"][0]["text"]
            rec["raw_text"] = text
            data = json.loads(text[text.find("{"): text.rfind("}") + 1])
            visit = ClassifiedVisit(event, VisitType(data["type"]), float(data.get("confidence", 0.5)),
                                    str(data.get("reason", "")), self.name)
            visit.request_id = request_id
            rec["label"] = visit.visit_type.value
            self._record(rec)
            return visit
        except Exception as exc:  # one bad call must not take down the briefing
            log.warning("Bedrock classify failed for %s: %s", event.id, exc)
            rec.setdefault("wall_ms", round((clock.perf_counter() - started) * 1000))
            rec["error"] = f"{type(exc).__name__}: {exc}"[:300]
            self._record(rec)
            return self.fallback.classify(event, open_hours, note=f"Bedrock error: {type(exc).__name__}")
