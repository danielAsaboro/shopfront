"""Amazon Bedrock vision classifier (Converse API with an image block)."""

from __future__ import annotations

import json
import logging
from datetime import time

import boto3

from app.classify.base import Classifier
from app.classify.stub import StubClassifier
from app.models import ClassifiedVisit, DoorEvent, VisitType

log = logging.getLogger(__name__)

PROMPT = """You are looking at a snapshot from a small shop's front-door camera.
Event: {kind} at {ts} (shop local time). Posted hours today: {hours}.
Classify the visit as exactly one of: customer, delivery, staff, after_hours, unknown.
- delivery: courier uniform, parcel, hand truck, delivery vehicle
- staff: someone unlocking the door, wearing the shop apron/uniform, arriving before open
- after_hours: activity while the shop is closed that is not staff or a delivery
Reply with JSON only: {{"type": "...", "confidence": 0.0-1.0, "reason": "short"}}"""


class BedrockClassifier(Classifier):
    name = "bedrock"

    def __init__(self, region: str, model_id: str):
        self.model_id = model_id
        self.client = boto3.client("bedrock-runtime", region_name=region)
        self.fallback = StubClassifier()

    def classify(self, event: DoorEvent, open_hours: tuple[time, time] | None) -> ClassifiedVisit:
        if not event.snapshot:
            # No image to look at (e.g. mock data): fall back to rules.
            return self.fallback.classify(event, open_hours)

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
            return ClassifiedVisit(event, VisitType(data["type"]), float(data.get("confidence", 0.5)),
                                   data.get("reason", ""), self.name)
        except Exception as exc:  # keep the briefing working if one call fails
            log.warning("Bedrock classify failed for %s: %s", event.id, exc)
            return self.fallback.classify(event, open_hours)
