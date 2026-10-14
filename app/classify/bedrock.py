"""Amazon Bedrock vision classifier (Converse API with an image block).

Needs the event's JPEG frame (fetched from Ring by the briefing). With no frame, or when a call
fails, the event is classified by the rules and the reason says why, so nothing is silently mixed.
"""

from __future__ import annotations

import json
import logging
from datetime import time

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

    def __init__(self, region: str, model_id: str, client=None):
        self.model_id = model_id
        self.client = client or boto3.client("bedrock-runtime", region_name=region)
        self.fallback = RulesClassifier()

    def classify(self, event: DoorEvent, open_hours: tuple[time, time] | None,
                 no_frame_reason: str = "no frame") -> ClassifiedVisit:
        if not event.snapshot:
            return self.fallback.classify(event, open_hours, note=no_frame_reason)

        hours = f"{open_hours[0]:%H:%M}-{open_hours[1]:%H:%M}" if open_hours else "closed"
        prompt = PROMPT.format(kind=event.kind, ts=event.timestamp.isoformat(), hours=hours)
        try:
            resp = self.client.converse(
                modelId=self.model_id,
                messages=[{"role": "user", "content": [
                    {"image": {"format": "jpeg", "source": {"bytes": event.snapshot}}},
                    {"text": prompt},
                ]}],
                inferenceConfig={"maxTokens": 200, "temperature": 0},
            )
            text = resp["output"]["message"]["content"][0]["text"]
            data = json.loads(text[text.find("{"): text.rfind("}") + 1])
            visit = ClassifiedVisit(event, VisitType(data["type"]), float(data.get("confidence", 0.5)),
                                    str(data.get("reason", "")), self.name)
            visit.request_id = resp.get("ResponseMetadata", {}).get("RequestId")
            return visit
        except Exception as exc:  # one bad call must not take down the briefing
            log.warning("Bedrock classify failed for %s: %s", event.id, exc)
            return self.fallback.classify(event, open_hours, note=f"Bedrock error: {type(exc).__name__}")
