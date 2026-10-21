# Shopfront

**The carrier scanned "business closed". Your doorbell says you were open.**

Shopfront is a receiving desk for a one-door shop (a bakery, café or boutique) with a Ring doorbell. It
checks a carrier's "business closed" or "delivery attempted" scan against the shop's posted hours and what
the doorbell recorded, and builds a dispute pack the owner reviews and sends themselves.

## What only Shopfront does

A doorbell clip can show who came to the door. On its own it's evidence of a visit, not an answer to the
carrier's scan. Shopfront takes four steps past "the doorbell as evidence":

1. **It ties the scan to the order.** The carrier's scan is linked to the purchase order or invoice it was
   for, from the shop's own CSV (by the tracking number, or from the order's row on the desk), so the pack
   names the order, its supplier, its items and its delivery window, not just a time.
2. **It checks "business closed" against the shop's hours and its door.** The scan time is compared with the
   posted hours and with the door activity 30 minutes either side: "you were open, and the door was in use".
   When the door recorded nothing it says so, and that only the hours back you, or that it can't tell.
3. **It keeps a record per carrier, month by month.** Each checked scan can be kept. The desk shows, per
   carrier, how many "business closed" scans it made this month and how many fell in posted hours, and
   `/report` prints that record as one page for the carrier's account manager or the supplier.
4. **It makes a signed pack anyone can check offline, addressed to the supplier.** The supplier is the
   carrier's customer and can get a scan corrected, so the draft goes to them. The zip's manifest is signed
   (Ed25519, or an AWS KMS key), and `scripts/verify_pack.py` checks it against the shop's key ID with no
   account and no network.

## How many shops this could help: an estimate (US)

| Factor | Value | Source |
|---|---|---|
| Retail shops with fewer than 20 employees | 877,627 | US Census Bureau, [County Business Patterns 2023](https://www2.census.gov/programs-surveys/cbp/datasets/2023/cbp23us.zip), retail trade (NAICS 44-45), size classes under 5, 5-9 and 10-19 employees: 457,825 + 248,198 + 171,604 |
| Parcel deliveries to a shop in a week | 5 | **Assumption.** We found no survey of how many parcels small shops receive |
| First deliveries that fail | 8% | Loqate, [*Fixing Failed Deliveries* 2021](https://info.loqate.com/hubfs/Loqate%202021/Fixing%20Failed%20Deliveries/Fixing%20Failed%20Deliveries%20-%20Final.pdf), US: "8% of first-time deliveries fail" (retailers' parcels to their customers, used here as a stand-in for deliveries to shops) |
| What a failed delivery costs the sender | $17.20 | The same Loqate report: "at an average cost of $17.20" |

877,627 shops × 5 deliveries × 52 weeks = 228.2 million deliveries a year. 8% of them is 18.3 million failed
first attempts, and at $17.20 each that's about **$314 million a year** in senders' costs. For one shop it's
5 × 52 × 8% ≈ 21 failed deliveries a year, about $358 at the survey average, before any lost trade.

What the estimate leaves out, because we found no source: how many failed attempts are "business closed"
scans while the shop was open, and how many of these shops have a Ring doorbell. So $314 million is the pool
of failed deliveries the check works on, not the size of Shopfront's market. Cafés and restaurants (NAICS 722:
another 495,905 places with fewer than 20 employees in the same Census file) aren't counted.

## At home too

The "delivery attempted" check doesn't use opening hours. It compares the carrier's time with what the
doorbell recorded within 10 minutes, so it answers the same question at a house: did a courier come to the
door when the tracking says they tried? With no order linked, the pack's draft goes to the carrier. There's
no separate home mode: the pages and the pack still say "shop". How to use it:
[docs/receiving-desk.md](docs/receiving-desk.md#at-home-the-delivery-attempted-check).

**What's live and what isn't:** Shopfront calls the Ring Partner API live against the Ring Developer
Playground. A Ring frame, a webhook sent by Ring itself and a Bedrock label haven't been captured yet, so the
demo runs on synthetic door events and a signed webhook replay, and every page says so.
[Status and proof](docs/status.md).

## How Ring and AWS do the work

1. **Orders in.** Import a CSV of orders or invoices (any POS, inventory or accounting export), so
   Shopfront knows what should arrive, from whom and when.
2. **Ring sees the door.** The Ring Partner API's Event History gives every ding and motion at the
   door. A signed Ring webhook whose motion `sub_type` is `package_delivery` (Ring's own Smart Alert)
   marks a courier straight away, with no model call. Other visits go to **Amazon Bedrock**, which
   labels the Ring frame (courier, customer, staff). Without a frame, time-of-day rules make a guess,
   and the page says it's a guess.
3. **Check the scan.** Type the carrier's scan and its time. Shopfront compares it with the posted
   hours and every door event within 30 minutes.
4. **Dispute pack.** You get the finding, the door log, the posted hours, the order, and the request
   IDs of the Ring and Bedrock calls it rests on. It also lists what it can't show and drafts a
   message. Shopfront never sends anything.

## Run the demo

Needs Python 3.11+. One command after install:

```bash
git clone <this repo> shopfront && cd shopfront
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env
.venv/bin/python -m scripts.demo
```

What the three links show, the hosted demo, tests, using Shopfront from a phone and connecting a real
Ring doorbell: [docs/run.md](docs/run.md).

## Why

- **It happens, and it costs the sale.** In November 2024 a family boutique in Clear Lake, Texas, was
  waiting for six boxes of denim for its Black Friday jeans sale. UPS tracking read "Business closed.
  Will deliver the next business day," while the store was open
  ([KPRC Click2Houston, 28 Nov 2024](https://www.click2houston.com/news/local/2024/11/29/small-business-scrambles-after-ups-mix-up-threatens-black-friday-plans/)).
- **The scan code itself has been disputed.** In 2015 UPS agreed to pay $25 million to settle a
  False Claims Act case. The US alleged that UPS applied inapplicable exception codes such as
  "business closed" to excuse late deliveries to federal customers. These were allegations only:
  UPS made no admission, and there was no finding of liability
  ([US Department of Justice, 19 May 2015](https://www.justice.gov/opa/pr/united-parcel-service-agrees-settle-alleged-civil-false-claims-act-violations)).
- **Ring's Package Alerts answer a different question.** They say a box landed in your Package
  Zone. Ring says they "work best with medium-to-large shipping boxes" and that "envelopes, tubes,
  small boxes, and food deliveries might not be detected". They need a compatible subscription
  ([Ring support](https://ring.com/support/articles/rk2nf/How-to-Get-Package-Alerts)). None of that
  helps when nothing was delivered. Shopfront checks the carrier's claim instead, against the
  posted hours and a door that was busy with customers. It needs no package in view, and it ties the
  scan to the order it was for.

## Status: what's live and what isn't

The hackathon requirements, the proof table (every live claim with its Ring request ID) and the code map
are in [docs/status.md](docs/status.md).

## Also in the app

- **Ask Shopfront** (`POST /api/ask`, the box on the dashboard): a [Strands](https://strandsagents.com) agent on Amazon Bedrock with five tools over the same data: door visits, deliveries, week, check_attempt and dispute_pack. It answers from what the tools return and lists every tool call and Bedrock RequestId with the answer. It needs AWS credentials with Bedrock access.
- **Morning briefing:** yesterday at the door, under the receiving desk. It covers doorbell presses while the shop was closed, when staff were first and last at the door, and foot traffic against the same weekday last week.
- **Staff tap:** once Ring or Bedrock has seen the courier for an order, staff can mark it All there, Short or Damaged. A short or damaged order gets its own dispute pack. An order with only a possible courier (a doorbell press nothing identified) or none seen has one quiet "It arrived" link instead, for when the delivery came anyway.

## Limits

- Ring has no "door answered" signal. Shopfront can say a courier was at the door. It can't say a
  parcel was handed over.
- A pack shows the shop was open and the door was busy. It supports the owner's account but can't
  prove that nobody came, because motion detection can miss people. Every pack says so.
- Labels from the time-of-day rules are guesses. The rules can't tell a customer's ring from a
  courier's, so only Ring's `package_delivery` alert or a Bedrock-labelled frame counts as a courier.
- Ring only returns events from after the owner connected the app, so "vs last week" is empty for
  the first week.
- One shop and one doorbell. One-way Ring account linking is built to the API reference (docs/ring.md, section 10) but hasn't been run against a real Ring AppStore install, and Shopfront hasn't been through certification.

## License

MIT
