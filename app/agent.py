"""Ask Shopfront: a Strands agent on Amazon Bedrock with tools over the shop's door data.

The agent can only see what the tools return (Ring door visits and their labels, the expected
deliveries and receipts, posted hours), and it is told to answer from those and nothing else.
Every tool call and every Bedrock RequestId is returned with the answer, so the page can show
exactly what the answer rests on.

Provenance: every fact a tool hands the model says where it came from, so a guess can't be passed off
as something the camera saw. Each door visit carries `provenance` (one of `PROVENANCE`) and `said_by`
in words; orders, posted hours and staff taps say `owner`; the week's counts are split by provenance.
`ring` is Ring's own package_delivery alert, `bedrock` is Bedrock reading a frame, `rules` is a guess
from the time of day, `replay` is Shopfront's replayed webhook (not a Ring notification) and
`synthetic` is demo data no camera recorded (app/provenance.py says each one exactly). A visit whose
source can't be told is stamped `rules`: unknown is a guess, never a sighting.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Callable

from app import provenance as prov

SYSTEM = """You are Shopfront, the front-door assistant for {shop}, a small shop. Today is {today} ({weekday}).
You answer the owner's questions about what happened at the shop's door, using only the tools.
- Call tools for every fact. Never guess times, counts or suppliers.
- Every visit has a provenance. Only "ring" (Ring's own package_delivery alert) and "bedrock" (Amazon Bedrock
  looked at the frame) are things something saw. "rules" is a guess from time of day and event kind, "replay"
  is Shopfront's replayed webhook (not a Ring notification), "synthetic" is made-up demo data and
  "ring-playground" is a Ring Developer Playground scene (Ring's simulator, not the shop's door): never state
  those as facts; say "a guess", "a replayed event", "demo data" or "a Playground scene (Ring simulator)"
  whenever an answer rests on one. "owner"
  is what the owner entered (orders, posted hours, staff taps).
- Ring has no "door answered" signal. Say a courier was at the door, never that a parcel was received.
- When the owner quotes a carrier's scan ("attempted delivery" or "business closed"), call check_attempt with
  that claim and give its text; don't soften or strengthen it.
- When the owner wants to dispute or complain, call dispute_pack. Give its finding, say a draft is ready to
  review at pack_url (or give no_draft_reason), and never say anything was sent: Shopfront never sends it.
- When a visit has a frame_url, end your answer with it on its own line as: Frame: <frame_url>
- If the data doesn't answer the question, say what is missing in one sentence.
Answer in two or three short sentences, plain English, times as HH:MM."""


class AgentUnavailable(Exception):
    """Ask can't run. `reason` is a named state; `message` is one plain sentence the page can show;
    str(exc) keeps the technical detail for logs and the Details toggle."""

    def __init__(self, detail: str, reason: str = "AGENT_FAILED", message: str | None = None):
        super().__init__(detail)
        self.reason = reason
        self.message = message or MESSAGES.get(reason, MESSAGES["AGENT_FAILED"])


MESSAGES = {
    "NO_AWS_SIGN_IN": "Ask needs an AWS sign-in with Bedrock access; it isn't available on this machine.",
    "BEDROCK_UNREACHABLE": "Ask couldn't reach Amazon Bedrock from this machine; check the internet connection "
                           "and try again.",
    "NO_MODEL_ACCESS": "This AWS account can't use the Bedrock model Ask needs; turn on model access in the "
                       "Bedrock console.",
    "BEDROCK_BUSY": "Amazon Bedrock is busy right now; try again in a minute.",
    "AGENT_NOT_INSTALLED": "Ask isn't installed on this machine.",
    "AGENT_FAILED": "Ask couldn't answer that just now; the door data on this page is unaffected.",
}

_SIGN_IN = {"NoCredentialsError", "PartialCredentialsError", "CredentialRetrievalError", "TokenRetrievalError",
            "SSOTokenLoadError", "UnauthorizedSSOTokenError", "ExpiredTokenException", "ExpiredToken",
            "UnrecognizedClientException", "InvalidClientTokenId", "InvalidSignatureException"}
_UNREACHABLE = {"EndpointConnectionError", "ConnectTimeoutError", "ReadTimeoutError", "ProxyConnectionError",
                "ConnectionClosedError", "SSLError", "ConnectionError"}
_NO_ACCESS = {"AccessDeniedException", "ResourceNotFoundException", "ValidationException"}
_BUSY = {"ThrottlingException", "ServiceQuotaExceededException", "ModelThrottledException",
         "ServiceUnavailableException", "ModelNotReadyException"}


def _chain(exc: BaseException):
    seen = set()
    while exc is not None and id(exc) not in seen:
        seen.add(id(exc))
        yield exc
        exc = exc.__cause__ or exc.__context__  # type: ignore[assignment]


def unavailable(exc: BaseException) -> AgentUnavailable:
    """Name what went wrong with a Bedrock call, looking through the agent loop's wrapping."""
    detail = f"{type(exc).__name__}: {exc}"[:300]
    for e in _chain(exc):
        names = {type(e).__name__}
        code, msg = "", str(e)
        response = getattr(e, "response", None)
        if isinstance(response, dict):
            code = str(response.get("Error", {}).get("Code", ""))
            names.add(code)
        if names & _SIGN_IN or "security token included in the request is expired" in msg:
            return AgentUnavailable(detail, "NO_AWS_SIGN_IN")
        if names & _UNREACHABLE or "request blocked" in msg or "Could not connect to the endpoint" in msg:
            return AgentUnavailable(detail, "BEDROCK_UNREACHABLE")
        if names & _BUSY:
            return AgentUnavailable(detail, "BEDROCK_BUSY")
        if names & _NO_ACCESS:
            return AgentUnavailable(detail, "NO_MODEL_ACCESS")
    return AgentUnavailable(detail, "AGENT_FAILED")


@dataclass
class Trace:
    tools: list[dict] = field(default_factory=list)
    results: list = field(default_factory=list)  # what each tool returned, in call order; kept off the API
    bedrock_request_ids: list[str] = field(default_factory=list)


PROVENANCE = ("ring", "bedrock", "rules", "replay", "synthetic", "ring-playground", "owner")
_FROM_KIND = {prov.RING_ALERT: "ring", prov.BEDROCK_RING: "bedrock", prov.BEDROCK_LIVE: "bedrock",
              prov.RULES: "rules", prov.REPLAY: "replay", prov.SYNTHETIC: "synthetic",
              prov.PLAYGROUND_KIND: "ring-playground", prov.BEDROCK_PLAYGROUND: "ring-playground"}


def visit_provenance(row: dict) -> str:
    """ring | bedrock | rules | replay | synthetic | ring-playground for a visit row; anything unrecognised is
    'rules'. A scene Bedrock labelled is still ring-playground: whatever Bedrock saw, it wasn't the shop's door."""
    return _FROM_KIND.get(prov.of(row), "rules")


def stamp(x):
    """Put `provenance` and `said_by` on every visit row in a tool result, however deeply it sits.
    A visit row is a dict that carries a label (`labelled_by`) or an event source (`event_source`)."""
    if isinstance(x, list):
        return [stamp(i) for i in x]
    if not isinstance(x, dict):
        return x
    out = {k: stamp(v) for k, v in x.items()}
    if "labelled_by" in x or "event_source" in x:
        p = visit_provenance(x)
        out["provenance"] = p
        out["said_by"] = prov.SAID_BY.get(prov.of(x), prov.SAID_BY[prov.RULES])
    return out


def unstamped(x, path: str = "$") -> list[str]:
    """Paths of visit rows without a known provenance: what `stamp` must leave empty."""
    if isinstance(x, list):
        return [p for i, v in enumerate(x) for p in unstamped(v, f"{path}[{i}]")]
    if not isinstance(x, dict):
        return []
    bad = [path] if ("labelled_by" in x or "event_source" in x) and x.get("provenance") not in PROVENANCE else []
    return bad + [p for k, v in x.items() for p in unstamped(v, f"{path}.{k}")]


def _owner(rows: list) -> list:
    return [{**r, "provenance": "owner"} if isinstance(r, dict) else r for r in rows]


class ShopTools:
    """Plain functions over the app's data; agent tools wrap these and record each call."""

    def __init__(self, visits_for_day: Callable[[date], list[dict]], receipts_for_day: Callable[[date], dict],
                 hours_for_day: Callable[[date], str], today: date,
                 attempt_checker: Callable[..., dict] | None = None,
                 pack_builder: Callable[[date, str, str, str], dict] | None = None):
        self._visits, self._receipts, self._hours, self.today = visits_for_day, receipts_for_day, hours_for_day, today
        self._attempt, self._pack = attempt_checker, pack_builder

    def _day(self, value: str) -> date:
        value = (value or "").strip().lower()
        if value in ("", "today"):
            return self.today
        if value == "yesterday":
            return self.today - timedelta(days=1)
        d = date.fromisoformat(value)
        if d > self.today:
            raise ValueError("that date is in the future")
        return d

    def door_visits(self, day: str) -> dict:
        d = self._day(day)
        return {"date": d.isoformat(), "posted_hours": self._hours(d), "posted_hours_provenance": "owner",
                "visits": stamp(self._visits(d))}

    def deliveries(self, day: str) -> dict:
        """Orders are the owner's (`provenance: owner`); the courier matched to each says its own."""
        d = self._day(day)
        r = stamp(self._receipts(d))
        if isinstance(r.get("expected"), list):
            r["expected"] = _owner(r["expected"])
        return {"date": d.isoformat(), **r}

    def check_attempt(self, day: str, at: str, claim: str = "attempted") -> dict:
        if not self._attempt:
            raise ValueError("attempt checks aren't available")
        d = self._day(day)
        t = datetime.strptime(at.strip(), "%H:%M").time()
        claim = (claim or "attempted").strip().lower().replace(" ", "_")
        if claim not in ("attempted", "business_closed"):
            raise ValueError("claim must be attempted or business_closed")
        if claim == "attempted":
            out = self._attempt(datetime.combine(d, t))
        else:
            out = self._attempt(datetime.combine(d, t), claim)
        return {**stamp(out), "scan_provenance": "owner", "posted_hours_provenance": "owner"}

    def dispute_pack(self, day: str, at: str = "", claim: str = "", order_ref: str = "") -> dict:
        if not self._pack:
            raise ValueError("dispute packs aren't available")
        d = self._day(day)
        if at:
            datetime.strptime(at.strip(), "%H:%M")
        if not at and not order_ref:
            raise ValueError("give the carrier's scan time or an order reference")
        out = stamp(self._pack(d, at.strip(), (claim or "").strip().lower().replace(" ", "_"), order_ref.strip()))
        if isinstance(out.get("order"), dict):
            out["order"] = {**out["order"], "provenance": "owner"}
        return out

    def week(self, end_day: str) -> list[dict]:
        end = self._day(end_day)
        out = []
        for i in range(6, -1, -1):
            d = end - timedelta(days=i)
            v = stamp(self._visits(d))
            types = ("customer", "delivery", "doorbell_press", "staff", "after_hours")
            out.append({"date": d.isoformat(), "weekday": d.strftime("%a"), "posted_hours": self._hours(d),
                        "posted_hours_provenance": "owner", **{t: sum(1 for x in v if x["type"] == t) for t in types},
                        "by_provenance": {t: {p: n for p in PROVENANCE
                                              if (n := sum(1 for x in v if x["type"] == t and x["provenance"] == p))}
                                          for t in types}})
        return out


def build_agent(tools: ShopTools, trace: Trace, shop: str, region: str, model_id: str):
    try:
        from strands import Agent, tool
        from strands.models import BedrockModel
    except ImportError as exc:  # pragma: no cover - requirements include it
        raise AgentUnavailable("strands-agents is not installed", "AGENT_NOT_INSTALLED") from exc

    def recorded(name: str, fn, **args):
        trace.tools.append({"tool": name, "args": args})
        try:
            out = fn(**args)
        except ValueError as exc:
            out = {"error": str(exc)}
        trace.results.append(out)
        return out

    @tool
    def door_visits(day: str) -> dict:
        """Every visit the Ring doorbell saw on one day, with its label, who labelled it, why, and a frame_url when Shopfront holds the Ring frame.

        Args:
            day: YYYY-MM-DD, "today" or "yesterday".
        """
        return recorded("door_visits", tools.door_visits, day=day)

    @tool
    def deliveries(day: str) -> dict:
        """The deliveries the owner expected on one day, each matched to the courier the doorbell saw (or "not_seen"), plus couriers nobody expected.

        Args:
            day: YYYY-MM-DD, "today" or "yesterday".
        """
        return recorded("deliveries", tools.deliveries, day=day)

    @tool
    def week(end_day: str) -> list:
        """Counts of customers, deliveries, staff and after-hours visits for the 7 days ending on end_day, with posted hours.

        Args:
            end_day: YYYY-MM-DD, "today" or "yesterday".
        """
        return recorded("week", tools.week, end_day=end_day)

    @tool
    def check_attempt(day: str, at: str, claim: str = "attempted") -> dict:
        """Check a carrier's tracking scan against the doorbell. "attempted": was a courier at the door near then? "business_closed": was the shop open by its posted hours, was the door active around the scan, and did a courier come?

        Args:
            day: YYYY-MM-DD, "today" or "yesterday".
            at: the carrier's time, HH:MM in shop local time.
            claim: "attempted" or "business_closed".
        """
        return recorded("check_attempt", tools.check_attempt, day=day, at=at, claim=claim)

    @tool
    def dispute_pack(day: str, at: str = "", claim: str = "", order_ref: str = "") -> dict:
        """Put together the evidence for a dispute: the finding, the order, the Ring frame with its hash, what the evidence doesn't show, and a drafted message for the owner to review. It never sends anything.

        Args:
            day: YYYY-MM-DD, "today" or "yesterday".
            at: the carrier's scan time, HH:MM, if disputing a scan.
            claim: "attempted" or "business_closed", if disputing a scan.
            order_ref: the order or PO number, if the dispute is about an order.
        """
        return recorded("dispute_pack", tools.dispute_pack, day=day, at=at, claim=claim, order_ref=order_ref)

    model = BedrockModel(model_id=model_id, region_name=region, temperature=0, max_tokens=400)

    def on_response(parsed=None, **_):
        rid = (parsed or {}).get("ResponseMetadata", {}).get("RequestId")
        if rid:
            trace.bedrock_request_ids.append(rid)

    model.client.meta.events.register("after-call.bedrock-runtime.*", on_response)
    today = tools.today
    return Agent(model=model, tools=[door_visits, deliveries, week, check_attempt, dispute_pack], callback_handler=None,
                 system_prompt=SYSTEM.format(shop=shop, today=today.isoformat(), weekday=today.strftime("%A")))


def ask(question: str, tools: ShopTools, shop: str, region: str, model_id: str,
        agent_factory=build_agent, keep_results: bool = False) -> dict:
    question = question.strip()
    if not question:
        raise ValueError("ask a question")
    trace = Trace()
    started = datetime.now()
    try:
        agent = agent_factory(tools, trace, shop, region, model_id)
        answer = str(agent(question[:500])).strip()
    except AgentUnavailable:
        raise
    except Exception as exc:  # credentials, access to the model, throttling: name it, don't fake an answer
        raise unavailable(exc) from exc
    frame = next((ln.split(":", 1)[1].strip() for ln in answer.splitlines() if ln.startswith("Frame:")), None)
    text = "\n".join(ln for ln in answer.splitlines() if not ln.startswith("Frame:")).strip()
    return {"question": question, "answer": text, "frame_url": frame if frame and frame.startswith("/frames/") else None,
            "tools": trace.tools, "bedrock_request_ids": trace.bedrock_request_ids, "model": model_id,
            "seconds": round((datetime.now() - started).total_seconds(), 1),
            **({"tool_results": trace.results} if keep_results else {})}
