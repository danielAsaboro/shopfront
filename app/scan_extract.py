"""Read a carrier's tracking page from a photo or screenshot, to prefill "The carrier's tracking says".

The owner uploads a JPEG or PNG; Bedrock Converse gets it as an image block with one forced tool,
`read_tracking_page` (carrier, tracking number, the status text and scan time exactly as shown, the
scan date), the same way the frame classifier answers (app/classify/bedrock.py). Only a valid call of
that tool is used; nothing is parsed out of free text.

What comes back is a prefill, never a check: the owner sees every value next to "as read from your
screenshot", corrects what's wrong, and presses "Check the door" themselves. Nothing is submitted for
them. The screenshot isn't stored.

Without AWS credentials there is no reading, and the page says so in plain words; the typed form
works the same either way.
"""

from __future__ import annotations

import logging
import re
from datetime import date, datetime
from typing import Callable

from app.classify.bedrock import INJECTION_GUARD, LabelRejected, converse_tool, tool_input
from app.reconcile import ATTEMPTED, BUSINESS_CLOSED

log = logging.getLogger(__name__)

TOOL_NAME = "read_tracking_page"
# Bedrock Converse takes images up to 3.75 MB (Amazon Bedrock User Guide, Converse API, ImageBlock).
MAX_BYTES = 3_750_000
FORMATS = {b"\xff\xd8\xff": "jpeg", b"\x89PNG\r\n\x1a\n": "png"}
LIMITS = {"carrier": 40, "tracking_number": 60, "status_text": 200, "scan_time": 40, "scan_date": 10}

NO_BEDROCK = ("Reading a screenshot needs an AWS sign-in with Bedrock access, and this computer doesn't "
              "have one. Type what the tracking page says in the form instead; it works the same.")

SYSTEM = (
    "You read one photo or screenshot of a parcel carrier's tracking page for a small shop owner, who will "
    "check every value before using it. Copy text exactly as it appears; do not correct, translate or "
    "guess. If a value isn't shown, give an empty string. Report the most recent scan only. "
    + INJECTION_GUARD
)

PROMPT = """Read the most recent scan on this tracking page with the read_tracking_page tool.
- carrier: the carrier's name as printed on the page, or "" if none is shown
- tracking_number: the tracking number exactly as shown, or ""
- status_text: the latest scan's status line, word for word (for example a "business closed" or "delivery attempted" line)
- scan_time: the latest scan's time, exactly as shown (for example "12:40 PM"), or ""
- scan_date: the latest scan's date as YYYY-MM-DD if the page shows the date, or "" """

TOOL_SPEC = {"toolSpec": {
    "name": TOOL_NAME,
    "description": "Record the latest scan shown on a carrier tracking page. Call exactly once.",
    "inputSchema": {"json": {
        "type": "object",
        "properties": {
            "carrier": {"type": "string", "maxLength": LIMITS["carrier"]},
            "tracking_number": {"type": "string", "maxLength": LIMITS["tracking_number"]},
            "status_text": {"type": "string", "maxLength": LIMITS["status_text"],
                            "description": "The latest status line, verbatim."},
            "scan_time": {"type": "string", "maxLength": LIMITS["scan_time"], "description": "Verbatim."},
            "scan_date": {"type": "string", "maxLength": LIMITS["scan_date"],
                          "description": "YYYY-MM-DD, or empty if the page shows no date."},
        },
        "required": list(LIMITS),
        "additionalProperties": False,
    }},
}}


class ScanReadError(Exception):
    """Why a screenshot wasn't read, as a code for the API and a sentence for the owner."""

    def __init__(self, code: str, detail: str, http_status: int):
        super().__init__(detail)
        self.code, self.detail, self.http_status = code, detail, http_status


def image_format(data: bytes) -> str | None:
    """'jpeg' or 'png' by the file's own first bytes (never by its name or declared type), else None."""
    for magic, fmt in FORMATS.items():
        if data.startswith(magic):
            return fmt
    return None


def check_upload(data: bytes) -> str:
    if not data:
        raise ScanReadError("EMPTY", "That file is empty. Choose the screenshot again.", 400)
    if len(data) > MAX_BYTES:
        raise ScanReadError("TOO_BIG", "That picture is over 3.75 MB. Crop it to the tracking details, or "
                            "save it smaller, and try again.", 413)
    fmt = image_format(data)
    if fmt is None:
        raise ScanReadError("NOT_AN_IMAGE", "Shopfront reads JPEG or PNG screenshots only.", 415)
    return fmt


def validate(data: dict) -> dict:
    """The tool input, checked: every field a string within its limit, nothing extra."""
    if set(data) != set(LIMITS):
        raise LabelRejected(f"fields {sorted(data)}")
    out = {}
    for k, cap in LIMITS.items():
        v = data[k]
        if not isinstance(v, str):
            raise LabelRejected(f"{k} is not text")
        out[k] = " ".join(v.split())[:cap]
    return out


_CLOCK = re.compile(r"\b(\d{1,2})[:.h](\d{2})\s*([ap])?\.?\s*(m\.?)?(?![\w])", re.IGNORECASE)


def clock_from(text: str) -> str:
    """'12:40 PM' -> '12:40' (24-hour HH:MM for the form), '4:05 p.m.' -> '16:05'; '' when unclear."""
    m = _CLOCK.search(text or "")
    if not m:
        return ""
    h, mi, half = int(m.group(1)), int(m.group(2)), (m.group(3) or "").lower()
    if mi > 59 or h > 23 or (half and not 1 <= h <= 12):
        return ""
    if half == "p" and h != 12:
        h += 12
    elif half == "a" and h == 12:
        h = 0
    return f"{h:02d}:{mi:02d}"


def claim_from(status: str) -> str | None:
    """Which of the form's scans the status line reads as; None leaves the owner to choose."""
    s = (status or "").lower()
    if "closed" in s:
        return BUSINESS_CLOSED
    if "attempt" in s:
        return ATTEMPTED
    return None


def day_from(text: str, today: date) -> str:
    try:
        d = date.fromisoformat((text or "").strip())
    except ValueError:
        return ""
    return d.isoformat() if date(2000, 1, 1) <= d <= today else ""


def prefill(read: dict, today: date) -> dict:
    """What goes into the form for the owner to confirm, next to what the page said."""
    return {"carrier": read["carrier"], "tracking": read["tracking_number"],
            "claim": claim_from(read["status_text"]), "attempt": clock_from(read["scan_time"]),
            "date": day_from(read["scan_date"], today), "as_read": read}


class ScanReader:
    """Bedrock reading of tracking-page screenshots. `available()` is False without AWS credentials."""

    def __init__(self, region: str, model_id: str, client=None, available: Callable[[], bool] | None = None):
        self.region, self.model_id = region, model_id
        self._client = client
        self._available = available
        self._tool_state: dict = {}

    def available(self) -> bool:
        if self._client is not None:
            return True
        if self._available is not None:
            return self._available()
        from app.classify import _aws_creds_available
        return _aws_creds_available(self.region)

    def client(self):
        if self._client is None:
            import boto3
            self._client = boto3.client("bedrock-runtime", region_name=self.region)
        return self._client

    def read(self, data: bytes, today: date | None = None) -> dict:
        fmt = check_upload(data)
        if not self.available():
            raise ScanReadError("NO_BEDROCK", NO_BEDROCK, 503)
        content = [{"image": {"format": fmt, "source": {"bytes": data}}}, {"text": PROMPT}]
        try:
            resp = converse_tool(self.client(), self.model_id, SYSTEM, content, TOOL_SPEC, 400, self._tool_state)
        except Exception as exc:
            log.warning("Bedrock couldn't read the screenshot: %s", exc)
            raise ScanReadError("BEDROCK_ERROR", "Bedrock couldn't read the screenshot just now "
                                f"({type(exc).__name__}). Type the values in the form instead.", 502) from exc
        try:
            read = validate(tool_input(resp, TOOL_NAME))
        except LabelRejected as exc:
            raise ScanReadError("NOT_READ", "Shopfront couldn't make out a tracking scan in that picture. "
                                "Type the values in the form instead.", 422) from exc
        if not any(read.values()):
            raise ScanReadError("NOT_READ", "That picture doesn't look like a tracking page. Type the values "
                                "in the form instead.", 422)
        out = prefill(read, today or datetime.now().date())
        out["request_id"] = (resp.get("ResponseMetadata") or {}).get("RequestId")
        return out
