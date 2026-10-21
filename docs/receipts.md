# Delivery receipts: what they prove, how they fail, how they're measured

A receipt is one sentence: **a courier was at the shop door at this time, and here is the Ring frame.**
Everything below is about keeping that sentence true.

## What a receipt does and doesn't prove

| It proves | It doesn't prove |
|---|---|
| The doorbell recorded an event at that time (Ring X-Request-ID on the call that returned it) | That the parcel was handed over. Ring has no "door answered" signal |
| Bedrock, looking at the Ring frame, called the person a courier (Bedrock RequestId), or Ring's own `package_delivery` alert did | Whose courier it was. A frame shows a uniform or a parcel, not a supplier, so a receipt lists the other expected suppliers the visit also fits |
| Nothing was recorded near a carrier's "attempted" time | That nobody came. Motion detection can miss people, so this outcome is worded as support, not proof |

Labels made by the time-of-day rules (no frame looked at) are guesses. A rules "delivery" (the rules call
every doorbell press one) is never a receipt, never counts as "seen", never feeds the money line and is
never called a courier: an order with one in its window is **possible** ("Possible courier, not confirmed
by a camera frame"), and every such press is listed under `unconfirmed`. An order whose window hasn't
ended yet is **expected** ("Not due yet"), not "not seen".

Staff at the door turn "while closed" off: a courier before opening, after a staff visit that morning,
or between two staff visits, wasn't missed (the bakery whose staff are in from 07:45 and whose flour
comes at 07:47).

## Matching expected deliveries to couriers

All of a day's expected deliveries are assigned at once to the couriers Ring or Bedrock saw (Hungarian
method, `app/deliveries.py`), in this
order of priority: match as many suppliers as there are couriers; then as many of those inside their
window as possible; then the closest times; then the earliest courier. The earlier one-supplier-at-a-time
matcher failed two cases that property tests found (`tests/test_matcher_properties.py`):

- a wide window took the only courier that fitted a narrower one, pushing that supplier outside its window;
- a windowed supplier took a courier outside its window while a supplier with no window went unmatched.

Properties checked on random days (with rules-labelled visits mixed in): one door visit never backs two
receipts; only Ring- or Bedrock-confirmed couriers become receipts, and a rules guess makes an unmatched
order "possible", never "seen"; every courier is either a receipt or listed as unexpected; nobody is "not seen" while a
courier is unmatched; the in-window count equals the best possible (brute force); the result doesn't
depend on the order events arrive in.

## Checking a carrier's attempt time

`GET /api/attempt?at=YYYY-MM-DDTHH:MM` (shop local time), and the agent's `check_attempt` tool, compare a
carrier's "delivery attempted" time with door events within ±10 minutes (`app/reconcile.py`). Attempts
near midnight also look at the neighbouring day. Outcomes, first match wins:

| Outcome | When | What the owner reads |
|---|---|---|
| `not_happened_yet` | The time is later than now at the shop | "That time hasn't happened yet"; nothing is checked, no pack |
| `no_such_local_time` | The clocks skipped that time (02:30 on a spring-forward night) | That the time didn't exist; no pack |
| `no_door_data` | No door events at all that day | Can't check |
| `courier_at_door` | A Ring- or Bedrock-labelled courier within ±10 min | Time, gap to the carrier's time, frame |
| `courier_came_later` | No such courier near then, but one later the same day | It may have arrived late; Shopfront can't tell whose it was |
| `door_activity_unverified` | Any press or motion near then that nothing looked at | That Shopfront can't say it was a courier, or that it wasn't |
| `someone_not_a_courier` | Everything near then was looked at, none a courier | What Bedrock called the nearest one |
| `nothing_recorded` | Door events that day, none near then | Supports the owner's account, doesn't prove it |

Gaps are measured between instants (UTC), never wall-clock readings: on a fall-back night 01:58 PDT and
01:03 PST are five minutes apart, not 55. A time that happened twice is read as the first, and the text
says so.

## Named failure states beyond the Ring client

| Where | Failure | What happens |
|---|---|---|
| Matcher | Two expected deliveries fit one courier | Receipt names the other supplier ("could also be …") |
| Matcher | Fewer couriers than expected deliveries | Remaining suppliers are `not_seen`; never a duplicate receipt |
| Attempt check | Unparseable, or a future date (`/api/attempt`) | 400 `BAD_TIME` |
| Attempt check | A time later today, or one the clocks skipped | Outcome `not_happened_yet` / `no_such_local_time`; a pack for it is a 400 |
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
