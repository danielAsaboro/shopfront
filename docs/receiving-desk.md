# The receiving desk: orders in, proof out

What Shopfront does with the shop's orders and the carrier's scans, what each output claims, and
where each claim stops. Companion to [receipts.md](receipts.md), which covers the matcher.

## 1. Orders and invoices in (`app/orders.py`, `/setup#orders`)

A CSV export becomes the list of expected deliveries. Only `day` and `supplier` are required.
Shopfront's own header is

```
day,supplier,window_start,window_end,order_ref,carrier,tracking,items,amount
```

Other exports are read through column aliases (Order ID, PO number, Vendor, Seller Name, Expected
delivery date, Tracking number, …). Rules:

- Every row is validated like one typed on `/setup`. A bad row is reported by line number; the rest import.
- Re-importing updates rows with the same `order_ref` and day instead of duplicating them, and keeps any staff tap.
  A row with no order number is the same row when its day, supplier, tracking, window and items match.
- Several rows for one order and day (one per item) fold into one delivery. Their items are joined; if
  more than one carries an amount, the amount is left blank rather than presenting one line as the total.
- `amount` is kept as written. Shopfront never sums or converts it.
- Dates are read as YYYY-MM-DD or month/day (2- or 4-digit years) unless the owner ticks "Dates are day/month".
  The day comes from the most specific date column: a delivery/expected/due date beats a plain `Date`, and
  `Order Date` is used only when there is no other, which the import message says.
- Comma, semicolon, tab and pipe delimiters are detected. Window times may be `9`, `09:00` or `9:00 AM`.

**Amazon Business:** the aliases cover the column names we expect in its order-history report
(`Order ID`, `Seller Name`, `Title`, `Item Subtotal`, `Carrier Name & Tracking Number` as
`UPS(1Z…)`). **Unverified:** these names come from public examples, not a live Amazon Business
account, and the Ordering/Reconciliation APIs are not connected (developer access unconfirmed).

## 2. Stage 0: Ring's `package_delivery` sub_type (`app/ring/webhooks.py`, `app/ring/api.py`)

Following Ring's [Computer Vision Guidelines](https://developer.amazon.com/docs/ring/computer-vision-guidelines.html),
the motion `sub_type` (added 2026-10-05, [release notes](https://developer.amazon.com/docs/ring/release-notes.html))
is a free first filter on signed webhooks:

| sub_type | What happens |
|---|---|
| `package_delivery` | Courier visit labelled **by ring**, no Bedrock call. In live mode the Ring frame at that time is kept as the receipt when the download works |
| `human`, none (Smart Alerts off), doorbell press | On to the classifier (Bedrock with a frame, else rules) |
| `vehicle`, `other_motion` | Logged and counted, never classified |

**Event History gets the same stage 0** (`stage0_route`, used by `app/briefing.py`). A history item has
no sub_type field, but `event_types` takes composite values that filter "by event type AND detection
subtype" ([API reference](https://developer.amazon.com/docs/ring/api-documentation.html), Event History >
Event type filtering). So `RingApiClient` asks for the day twice: `ding,motion`, then
`motion.package_delivery`; motion events in the second answer (by Ring's event ID) get
`ring_subtype = "package_delivery"` and become couriers **by ring** with no model call. The Event History
table lists `motion.human`, `.vehicle`, `.animal` and `.other_motion`; `package_delivery` is a detection
subtype from the Motion Detection webhook section, so whether history honours `motion.package_delivery`
is **unverified live**. If Ring answers 400, the filter isn't asked again and those events go to the
classifier untagged, as with Smart Alerts off. When `RING_EVENT_TYPES` has a single composite motion
value (`ding,motion.human`), every motion event carries that subtype with no extra call.

A webhook and its Event History record of the same press or motion (same kind, within 5 s) count
once; the webhook copy wins. The dashboard footer shows the funnel:
events, skipped at stage 0, labelled by Ring, reached Bedrock, labelled by rules.

**Replays.** The Developer Playground sends no webhooks. `scripts/feed_webhook.py` posts signed
payloads in Ring's documented shape with `X-Shopfront-Replay: 1`; they are logged `replayed: true`,
their events say `ring-webhook-replay`, and every dispute pack built on them says so in its limits, in
the first line of the draft and its subject ("REPLAYED RING EVENTS"), and in the zip's README.

## 3. Carrier scans (`app/reconcile.py`, dashboard "The carrier's tracking says")

**Delivery attempted:** the outcomes in [receipts.md](receipts.md#checking-a-carriers-attempt-time). A
courier labelled by Ring's `package_delivery` alert counts as seen, like one Bedrock saw in a frame.

**Business closed** (the scan behind the Texas boutique's missed Black Friday stock):

First match wins (after `not_happened_yet` / `no_such_local_time`, which refuse the scan):

| Outcome | When | Claim it makes |
|---|---|---|
| `posted_closed` | The shop's own hours say closed | The scan agrees with the hours; no draft |
| `no_door_data` | No door events at all that day | Can't check |
| `open_courier_came` | Posted open and a Ring/Bedrock courier within 10 min | A courier came to an open shop |
| `open_door_busy` | Posted open, something nothing looked at within 10 min of the scan, and other door events within 30 min | "You were open": the posted hours and a door in use say so. The event near the scan is named as unidentified ("an unidentified doorbell press at 14:36, no camera frame confirms who"), never offered as the courier; the draft states the hours and the activity only, and never says nobody came |
| `open_no_courier` | Posted open, door events within 30 min, and everything within 10 min was looked at and wasn't a courier (or there was nothing that close) | Open and operating; supports, doesn't prove, that nobody came |
| `courier_came_later` | Posted open, nothing at the door within 30 min of the scan, a Ring/Bedrock courier later the same day | It may have arrived late after a scan that doesn't match the hours; whose it was is unknown; no draft until the order is named |
| `door_activity_unverified` | Posted open, and the only door events within 30 min are within 10 min of the scan and nothing looked at them | Can't say whether a courier came, or show anyone else using the door; no draft |
| `camera_offline` | Posted open, no courier near the scan, and Ring's `device_offline` notice covers it | The camera may not have been watching; no draft |
| `open_by_hours_only` | Posted open, nothing at the door within 30 min, Ring's last status notice said online | Only the posted hours back the owner; no draft |
| `door_silent` | Posted open, nothing at the door within 30 min, no Ring status notice saying online | "Can't tell: the door recorded nothing around the scan"; no draft |

Both checks ask whether the camera was watching (`app/coverage.py`, see
[receipts.md](receipts.md#checking-a-carriers-attempt-time)): Ring's `device_online` / `device_offline`
webhooks, then the door's own events within 30 minutes.

A label from the time-of-day rules is never a sighting and never "not a courier" either. For an
attempted-delivery scan, one near the scan makes the answer unverified. A "business closed" scan claims
the shop was closed, not that nobody came, so posted hours plus other people using the door answer it
even with an unidentified event near the scan; only when that event is all the door recorded is the
answer unverified. When the order's own courier came later, either way, the pack is a late-delivery note.

**Two drafts.** A scan pack for one of the shop's orders offers a draft to the supplier (a receiving
issue: the supplier is the carrier's customer and can make it act) and one to the carrier about its own
scan; the owner picks one on the pack page and both are in the zip. Both pass the wording guard
(`app/wording.py`). With no order there is only the carrier's. The activity counts give Ring's event kinds ("11 motion events") and
only camera-confirmed customers; rule-guessed customers are kept apart. A hand-worked day in
`tests/test_scan_checks.py` pins every outcome, and a property test checks them against the evidence.

**From a screenshot** (`app/scan_extract.py`, `POST /scan/extract`, `_scan_upload.html`). The owner can
upload a photo or screenshot of the carrier's tracking page (JPEG or PNG by its own bytes, up to
3.75 MB, Bedrock's Converse image limit) instead of typing. Bedrock Converse reads it through one forced
tool, `read_tracking_page` (carrier, tracking number, status text and scan time verbatim, scan date),
with the same injection guard as the frame classifier; free text is never parsed. The page shows each
value as read next to what goes into the form; "Put these in the form" fills the typed form and
nothing is checked until the owner presses "Check the door". The picture isn't kept. Without AWS
credentials the section says, in plain words, that reading a screenshot needs an AWS sign-in, and the
typed form works as before. Owner-only and same-site, like every other write (`app/owner.py`).
Tested against botocore's Stubber in `tests/test_scan_extract.py`; **not run against live Bedrock yet**.

## 4. Staff tap (`/deliveries/{id}/received`)

Optional. Once the door has seen a courier for an order, staff can tap **All there**, **Short** or
**Damaged** with a note. Nothing is inferred: Shopfront only records what staff tapped and when.

## 5. Dispute pack (`app/dispute.py`, `/dispute`, `/dispute.zip`, `/api/dispute`)

One pack per question: a carrier scan, an order not seen, or an order marked short/damaged. It holds
the order (auto-linked when the carrier's tracking number matches an imported order), the carrier's
claim and the finding, the door log 30 minutes either side, the Ring frame with its SHA-256 and
Bedrock RequestId when one was cited, the posted hours, and a fixed "what this does not show" list.

The draft is a template in the shop's voice, filled only from those fields, and it states only what
something looked at: Ring's event kinds, Ring or Bedrock courier labels, Bedrock "not a courier"
labels. "Our front-door camera shows otherwise" appears only when such a label backs it.

Who it goes to:

- **An order of the shop's** (linked by order or tracking number): the supplier. The shipper is the
  carrier's customer, so the supplier is who can get a scan corrected. When the order's own courier
  came after the scan, the finding is `delivered_after_scan` ("scanned business closed at 12:40, then
  the courier was at the door at 13:02") and the draft is a late-delivery note asking the supplier to
  have the scan corrected, not a redelivery request.
- **No order:** the carrier, by the name the owner typed, or a plain "Hello," when none was typed.

There is **no draft** when the door agrees with the carrier, when the shop's hours say closed, when
only the posted hours back the owner, when something nothing looked at sat near the scan, when a
courier came later but no order is linked, or for an order that is only "possible" or not due yet. A
"not seen" order gets a draft only when every door event in its window was looked at.

**Demo and replayed data.** The pack records every event's source (`event_sources`, `cited_sources`).
If any is `synthetic`, the pack, `pack.txt`, the zip README and the draft's subject and first line say
"DEMO DATA, not from a camera"; replayed webhooks add "REPLAYED RING EVENTS". Frames say where they
came from: a Ring image download, or a frame captured in the owner's browser from a Ring live-view
session. Times are the shop's (`made_at`, the staff tap).

Shopfront never sends anything; the page says so and offers copy, print and a zip (`README.txt`,
`pack.txt`, `pack.json`, the frame). The zip and frames are served to this machine only.

The Ask agent has a `dispute_pack` tool and is told to point to the pack, never to say it was sent.

## Tests

`tests/test_receiving_desk.py`: imports, folding and re-imports, stage-0 routing and dedupe,
replay marking, every business-closed outcome plus a property test, packs and drafts, the zip, and
the pages end to end on synthetic data. Nothing here has run against a live Ring webhook or Amazon
Business account.
