"""Can Ask Shopfront answer from this machine? Checked before the page offers a question box.

AWS credentials on the machine aren't enough: the network may not reach Bedrock, or the account may not
have the model. So the first page that needs to know makes one Bedrock Converse call that can't produce
an answer worth paying for (a blank message, one output token at most) and reads how Bedrock refuses it:

- a validation error about the blank message (or, rarely, an answer): Bedrock is reachable and the model
  is there, so Ask is offered;
- anything else is named the way app/agent.py names a failed question (NO_AWS_SIGN_IN,
  BEDROCK_UNREACHABLE, NO_MODEL_ACCESS, ...), and the page says Ask isn't available instead of
  showing a box that can only fail.

The answer is kept for a while (`ttl`), and a question that fails for one of those reasons marks it at
once. Tests pass `call` so nothing reaches the network.
"""

from __future__ import annotations

import threading
import time

from app.agent import unavailable

# Failures that won't fix themselves in a minute: stop offering the box until the next check
LASTING = {"NO_AWS_SIGN_IN", "BEDROCK_UNREACHABLE", "NO_MODEL_ACCESS", "AGENT_NOT_INSTALLED"}


def _converse_blank(region: str, model_id: str) -> None:
    import boto3
    from botocore.config import Config

    client = boto3.client("bedrock-runtime", region_name=region,
                          config=Config(connect_timeout=2, read_timeout=4, retries={"max_attempts": 1}))
    client.converse(modelId=model_id, messages=[{"role": "user", "content": [{"text": ""}]}],
                    inferenceConfig={"maxTokens": 1})


def _blank_message_refused(exc: BaseException) -> bool:
    """Bedrock's ValidationException for the blank message itself (not for the model id) means it got
    past sign-in and model access."""
    response = getattr(exc, "response", None)
    err = response.get("Error", {}) if isinstance(response, dict) else {}
    msg = str(err.get("Message", exc)).lower()
    return err.get("Code") == "ValidationException" and "model" not in msg


class AskProbe:
    def __init__(self, region: str, model_id: str, ttl: float = 600, call=None):
        self.region, self.model_id, self.ttl = region, model_id, ttl
        self._call = call or _converse_blank
        self._lock = threading.Lock()
        self._state: str | None = None
        self._checked = 0.0

    def state(self) -> str | None:
        """None when Ask can answer; otherwise the named reason it can't."""
        with self._lock:
            if self._checked and time.monotonic() - self._checked < self.ttl:
                return self._state
            self._state = self._probe()
            self._checked = time.monotonic()
            return self._state

    def _probe(self) -> str | None:
        try:
            self._call(self.region, self.model_id)
        except Exception as exc:  # noqa: BLE001 - every failure is named below, none is raised to the page
            if _blank_message_refused(exc):
                return None
            return unavailable(exc).reason
        return None

    def mark(self, reason: str) -> None:
        """A real question just failed: if it won't fix itself, stop offering the box now."""
        if reason in LASTING:
            with self._lock:
                self._state, self._checked = reason, time.monotonic()
