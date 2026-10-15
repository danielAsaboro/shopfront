# Delivery receipts: what they prove, how they fail, how they're measured

A receipt is one sentence: **a courier was at the shop door at this time, and here is the Ring frame.**
Everything below is about keeping that sentence true.

## What a receipt does and doesn't prove

| It proves | It doesn't prove |
|---|---|
| The doorbell recorded an event at that time (Ring X-Request-ID on the call that returned it) | That the parcel was handed over. Ring has no "door answered" signal |
| Bedrock, looking at the Ring frame, called the person a courier (Bedrock RequestId) | Whose courier it was. A frame shows a uniform or a parcel, not a supplier, so a receipt lists the other expected suppliers the visit also fits |
| Nothing was recorded near a carrier's "attempted" time | That nobody came. Motion detection can miss people, so this outcome is worded as support, not proof |

Labels made by the time-of-day rules (no frame looked at) are never called a courier in an attempt check.

## Matching expected deliveries to couriers

All of a day's expected deliveries are assigned at once (Hungarian method, `app/deliveries.py`), in this
order of priority: match as many suppliers as there are couriers; then as many of those inside their
window as possible; then the closest times; then the earliest courier. The earlier one-supplier-at-a-time
matcher failed two cases that property tests found (`tests/test_matcher_properties.py`):

- a wide window took the only courier that fitted a narrower one, pushing that supplier outside its window;
- a windowed supplier took a courier outside its window while a supplier with no window went unmatched.

Properties checked on random days: one door visit never backs two receipts; only courier-labelled visits
become receipts; every courier is either a receipt or listed as unexpected; nobody is "not seen" while a
courier is unmatched; the in-window count equals the best possible (brute force); the result doesn't
depend on the order events arrive in.

## Checking a carrier's attempt time

`GET /api/attempt?at=YYYY-MM-DDTHH:MM` (shop local time), and the agent's `check_attempt` tool, compare a
carrier's "delivery attempted" time with door events within ±10 minutes (`app/reconcile.py`). Attempts
near midnight also look at the neighbouring day.

| Outcome | When | What the owner reads |
|---|---|---|
| `courier_at_door` | A Bedrock-labelled courier within ±10 min | Time, gap to the carrier's time, frame |
| `someone_not_a_courier` | Bedrock saw a visit near then and labelled it something else | What Bedrock called them |
| `door_activity_unverified` | A press or motion near then with no frame confirming who | That Shopfront can't say it was a courier |
| `nothing_recorded` | Door events that day, none near then | Supports the owner's account, doesn't prove it |
| `no_door_data` | No door events at all that day | Can't check |

## Named failure states beyond the Ring client

| Where | Failure | What happens |
|---|---|---|
| Matcher | Two expected deliveries fit one courier | Receipt names the other supplier ("could also be …") |
| Matcher | Fewer couriers than expected deliveries | Remaining suppliers are `not_seen`; never a duplicate receipt |
| Attempt check | Time in the future or unparseable | 400 `BAD_TIME` |
| Bedrock | Call fails (throttled, no access, network) | Rules label with "Bedrock error: …" in the reason, and the error is written to `.data/bedrock-calls.jsonl` |
| Live view | Rules classifier active | Frames are `unknown` with the reason; never guessed from the clock |
| Agent | No credentials, no model access, network | 503 `AGENT_UNAVAILABLE` with the cause; no answer is made up |
| Agent | Question about a future date or the hand-over | Declines (checked by the agent eval's two trap questions) |

## How the classifier is measured

`scripts/eval_frames.py`. Live view keeps every frame it sends to Bedrock (`.data/live-frames/`), and every
Bedrock call appends its raw record (RequestId, `latencyMs`, token usage, stop reason, the model's text,
or the error) to `.data/bedrock-calls.jsonl`.

1. `sheet` writes a shuffled, blank labelling sheet. Two people label the same frames without seeing the
   model's output: courier, customer, staff, nobody, other.
2. `run` asks Bedrock about every frame once (resumable), with all-day hours so the clock can't push a
   label to "after hours".
3. `score` reports Cohen's kappa between the two people, drops the frames they disagree on, then gives
   the confusion matrix, per-class precision and recall, accuracy with a Wilson 95% interval, a
   majority-class baseline, a per-session table (frames from one session aren't independent), latency
   p50/p95, cost, and every mistake with Bedrock's reason and RequestId.

The Ring Developer Playground plays three scenes (Package, Vehicle, Motion), so a Playground-only set
measures three clips, however many frames it has. The report's per-session table makes that visible.

## How the agent is measured

`scripts/eval_agent.py` builds ten questions from a day's data (expected deliveries go in a temporary
book), works out each answer from the tools alone, then asks the agent. An answer passes when it's
correct and every time, date and number in it appears in what that run's tool calls returned. Two
questions are traps: whether the parcel was handed over, and a future date.
