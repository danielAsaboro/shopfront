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
- Several rows for one order and day (one per item) fold into one delivery. Their items are joined; if
  they carry different amounts, the amount is left blank rather than presenting one line as the total.
- `amount` is kept as written. Shopfront never sums or converts it.
- Dates are read as YYYY-MM-DD or month/day unless the owner ticks "Dates are day/month".

**Amazon Business:** the aliases cover the column names we expect in its order-history report
(`Order ID`, `Seller Name`, `Title`, `Item Subtotal`, `Carrier Name & Tracking Number` as
`UPS(1Z…)`). **Unverified:** these names come from public examples, not a live Amazon Business
account, and the Ordering/Reconciliation APIs are not connected (developer access unconfirmed).

## 2. Stage 0: Ring's `package_delivery` sub_type (`app/ring/webhooks.py`)

Following Ring's [Computer Vision Guidelines](https://developer.amazon.com/docs/ring/computer-vision-guidelines.html),
the motion `sub_type` (added 2026-10-05, [release notes](https://developer.amazon.com/docs/ring/release-notes.html))
is a free first filter on signed webhooks:

| sub_type | What happens |
|---|---|
| `package_delivery` | Courier visit labelled **by ring**, no Bedrock call. In live mode the Ring frame at that time is kept as the receipt when the download works |
| `human`, none (Smart Alerts off), doorbell press | On to the classifier (Bedrock with a frame, else rules) |
| `vehicle`, `other_motion` | Logged and counted, never classified |

A webhook and its Event History record of the same press or motion (same kind, within 5 s) count
once; the webhook copy wins because it carries the sub_type. The dashboard footer shows the funnel:
events, skipped at stage 0, labelled by Ring, reached Bedrock, labelled by rules.

**Replays.** The Developer Playground sends no webhooks. `scripts/feed_webhook.py` posts signed
payloads in Ring's documented shape with `X-Shopfront-Replay: 1`; they are logged `replayed: true`,
their events say `ring-webhook-replay`, and every dispute pack built on them says so.

## 3. Carrier scans (`app/reconcile.py`, dashboard "The carrier's tracking says")

**Delivery attempted:** five outcomes, unchanged except that a courier labelled by Ring's
`package_delivery` alert now counts as seen, like one Bedrock saw in a frame.

**Business closed** (the scan behind the Texas boutique's missed Black Friday stock):

| Outcome | When | Claim it makes |
|---|---|---|
| `open_no_courier` | Posted open, door events within 30 min of the scan, no Ring/Bedrock courier within 10 min | Open and operating; supports, doesn't prove, that nobody came |
| `open_courier_came` | Posted open and a seen courier within 10 min | A courier came to an open shop |
| `open_by_hours_only` | Posted open, nothing at the door within 30 min | Only the posted hours back the owner; no draft |
| `posted_closed` | The shop's own hours say closed | The scan agrees with the hours; no draft |
| `no_door_data` | No door events at all that day | Can't check |

A courier labelled only by the time-of-day rules never counts as a sighting. A property test checks
every outcome against the evidence it cites.

## 4. Staff tap (`/deliveries/{id}/received`)

Optional. Once the door has seen a courier for an order, staff can tap **All there**, **Short** or
**Damaged** with a note. Nothing is inferred: Shopfront only records what staff tapped and when.

## 5. Dispute pack (`app/dispute.py`, `/dispute`, `/dispute.zip`, `/api/dispute`)

One pack per question: a carrier scan, an order not seen, or an order marked short/damaged. It holds
the order (auto-linked when the carrier's tracking number matches an imported order), the carrier's
claim and the finding, the door log 30 minutes either side, the Ring frame with its SHA-256 and
Bedrock RequestId when one was cited, the posted hours, and a fixed "what this does not show" list.

The draft is a template in the shop's voice, filled only from those fields. There is **no draft**
when the door agrees with the carrier, when the shop's hours say closed, or when only the posted
hours back the owner. Shopfront never sends anything; the page says so and offers copy, print and a
zip (`pack.txt`, `pack.json`, the frame). The zip and frames are served to this machine only.

The Ask agent has a `dispute_pack` tool and is told to point to the pack, never to say it was sent.

## Tests

`tests/test_receiving_desk.py`: imports, folding and re-imports, stage-0 routing and dedupe,
replay marking, every business-closed outcome plus a property test, packs and drafts, the zip, and
the pages end to end on synthetic data. Nothing here has run against a live Ring webhook or Amazon
Business account.
