# Amazon Bedrock in Shopfront

## 1. The rule we enforce

A visit gets a Bedrock label only when Bedrock looked at that visit's Ring frame. Every other label is marked `rules` and says why.

## 2. Enforcement layers

| Layer | Where | What it blocks |
|---|---|---|
| Classifier choice at start-up | `make_classifier` in [app/classify/\_\_init\_\_.py](../app/classify/__init__.py) | `CLASSIFIER=auto` uses Bedrock only when the AWS credential chain resolves. The footer and `/healthz` show which classifier is active. |
| No frame, no call | `BedrockClassifier.classify` in [app/classify/bedrock.py](../app/classify/bedrock.py) | An event without a JPEG (synthetic data, or a Ring `MEDIA_NOT_FOUND`) never reaches Bedrock. It is labelled `rules` with the reason, e.g. "no Ring frame: MEDIA_NOT_FOUND". |
| Call failure | same | Any exception from the Converse call, or an unparseable reply, gives a `rules` label with "Bedrock error: <type>". |
| Cache by Ring event ID and model | [app/cache.py](../app/cache.py) | Each frame is classified once per model, so reloading the page doesn't repeat calls. Only real Bedrock answers are cached. |

## 3. Flow

1. Ring Event History returns a ding or motion event (see [ring.md](ring.md)).
2. Shopfront downloads the frame at the event's start time.
3. `bedrock-runtime` Converse is called with the JPEG as an image block, plus the event kind, the local time and the posted hours. Temperature is 0, with at most 200 tokens out.
4. The model replies with `{"type", "confidence", "reason"}`. The label, reason and Bedrock `RequestId` are stored with the event and shown in the "Every door event" table and `/api/events`.

## 4. Service and model

| Item | Value |
|---|---|
| Service | Amazon Bedrock Runtime, `Converse` API (boto3) |
| Model | `BEDROCK_MODEL_ID`, default `us.anthropic.claude-haiku-4-5-20251001-v1:0` (a cross-region inference profile) |
| Auth | Standard AWS credential chain. `aws login` credentials need `botocore[crt]`, which is now in `requirements.txt`. |
| Region | `AWS_REGION`, default `us-east-1` |

## 5. Failure handling

| Failure | Resulting label | Visible as |
|---|---|---|
| No AWS credentials with `CLASSIFIER=auto` | Rules for every event | Footer: "Labels: time-of-day rules" |
| No frame for the event | Rules | Reason ends with "(no Ring frame: <RingStatus>)" or "(synthetic event has no frame)" |
| Converse throws (AccessDenied, throttling, model not enabled, …) | Rules | Reason ends with "(Bedrock error: <ExceptionType>)" |
| Reply isn't the JSON we asked for, or has an unknown type | Rules | Same, "(Bedrock error: JSONDecodeError / ValueError)" |

## 6. Docs vs live differences

None recorded yet, because Bedrock has not been called from this project. The earlier scaffold could never call it: it needed image bytes that nothing supplied.

## 7. Config

`CLASSIFIER`, `AWS_REGION`, `BEDROCK_MODEL_ID`. No AWS keys go in `.env`.

## 8. Live proof

| Action | Model | Bedrock RequestId | Date |
|---|---|---|---|
| Classify a real Ring frame | — | **Not captured yet.** Needs a Ring frame and AWS credentials with Bedrock model access. | — |

`scripts/ring_proof.py --bedrock` records the label and RequestId for the latest frame of the day.

## 9. Honest scope

- One Bedrock call per Ring event, with no batching. That's fine for a single shop, but busy days would want a cheaper first pass, such as only classifying dings plus motion outside open hours.
- The prompt has not been evaluated on real footage. The accuracy of the rules on synthetic data is circular, because the generator and the rules share their assumptions, and it is not reported as a result.
- This is the only AWS service used at runtime. There is no AgentCore or Strands pipeline.
