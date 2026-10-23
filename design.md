# Shopfront design

**Feel:** a receiving desk, calm and factual. It answers "did my delivery come, and is the carrier's scan true?" It is not a security console.
**Direction (round 11):** one column of rounded cards on a warm blush page, in a geometric sans. A pink-to-lavender hero card carries the answer, near-black cards hold the record and the folds, white cards hold the forms; pills for what you press, amber for the one thing to act on, soft glows. The first thing on the page is what the owner is waiting for: today's orders and any carrier scan that needs checking. The briefing of yesterday at the door comes after.

## Tokens

The values live in [app/templates/_style.html](app/templates/_style.html) (`:root`, with dark mode under `prefers-color-scheme`). Never copy them elsewhere.

| Token | Light | Dark | Use |
|---|---|---|---|
| `--bg` | #f7f1f1 | #0d0c10 | Page (warm blush, with soft blurred bubbles from `--bubble-a/b/c`) |
| `--ink` / `--muted` | #17161b / #68646f | #f1eef3 / #a29eab | Text / labels, reasons, footer |
| `--line` / `--edge` | #e9e1e3 / #8a8490 | #2a2830 / #76717f | Rules between rows / borders of inputs and small square buttons (3:1 or more) |
| `--surface` | #ffffff | #211f27 | White cards: the header bar, the forms (scan check, Ask, set-up steps, the pack's facts) |
| `--card` / `--card-2` | #17161b / #222128 | #121116 / #201f26 | Near-black cards (carrier record, folds, door logs) / the figure tiles inside them |
| `--card-ink` / `--card-muted` / `--card-line` / `--card-edge` | #f5f2f6 / #a19daa / #2f2d36 / #77727f | same | Text, labels, rules and borders inside a dark card |
| `--pill` / `--pill-ink` | #f5d0d3 → #ead2e3 → #cbc5f2 / #17161b | same | The hero card, secondary buttons, the current tab |
| `--amber` / `--amber-2` / `--amber-ink` | #f7a928 / #f59e0b / #1d1405 | same | The one act-on button (Build the dispute pack, Download the pack) and the act-on dot |
| `--today` | #b49cf0 | #c2adff | Today's chart bars only (lavender, fading to pink) |
| `--lw` / `--card-lw` | #cfc8cd / #67636f | #46434d / #67636f | Last week's bars (graphite in a dark card) |
| `--warn` / `--warn-bg` | #a15c00 / #fff2d9 | #ffbe55 / #33240b | Things the owner should act on, the data-source banner and the pack watermark. In a dark card `--card-warn` #ffbe55; on the hero #7a4300 |
| `--glow` / `--glow-pill` / `--glow-amber` | pink shadows | dimmer | Under cards, pills and the amber button |
| `--r-xl/lg/md/sm` | 32 / 24 / 16 / 12px | | Cards / tiles and the header / banners, buttons / thumbnails, square buttons |

**Colour discipline:** mostly blush, white and near-black; the pink-lavender gradient for the hero and what you press; amber only for the act-on.
- Lavender in the chart means "today" and is never a link colour. Links are ink, underlined (the underline in `--muted`, thicker on hover).
- Contrast: text and links 4.5:1 or more, warn text on `--warn-bg` included; input borders 3:1. `color-scheme: light dark` so date and time pickers follow the theme. Inputs are 16px so phones don't zoom on focus.
- Amber/orange means "act on this" or "this isn't real Ring data". It never decorates.
- Every surface redefines `--ink`/`--muted`/`--line`/`--edge`/`--warn` for itself (dark cards light, the hero dark in both themes), so text keeps 4.5:1 wherever it sits. Print drops the cards, gradients and glows: black on white, thin rules.
- Never put two accents in one component.

**Type:** Poppins (Google Fonts, system sans fallback). h1 30/1.2/600 · verdict 24/1.3/600 · section and fold headings 17/600, sentence case · body 15/1.55 · eyebrows 12 uppercase, tracking .06em · figures 30/600. Monospace is only for env var names.
**Spacing:** 4px base. Cards are 16px apart with 20-24px padding (16px on a phone). The width cap is 880px, with a 16px side gutter.

## Components

- **Header.** The same on every page: a white rounded bar with a small near-black square icon, "Shopfront · shop name" and the four tabs (receiving desk, live door, set up, Ring) as pills with line icons; the current tab is the gradient pill with its glow. On a phone the tabs are a dark bar fixed at the bottom of the screen, the current one a glowing pink circle.
- **Banner** (synthetic mode, replayed webhooks, Ring sign-in expired). On every page, error pages and the printed pack included, except where the page carries its own watermark saying the same (the dispute page's pack watermark and the monthly report's, each linking to Connect Ring in demo mode). One page-level notice per page: never a banner and a watermark, and never a second warning inside the draft. Shows the source of truth. Full width, rounded, warn colours with an amber dot, one sentence.
- **Details.** Request IDs, hashes, sub_types, event IDs and the funnel sit in a closed "Details" toggle on owner pages. Printing a pack opens them.
- **Receiving desk.** The day's orders, each with how its courier was seen (Ring alert, Bedrock frame, or "possible, unconfirmed" from the rules) or "not seen". Leads the dashboard.
- **Check the door.** A white card: the carrier's scan, its time, carrier and tracking, one button. The answer is the hero card: one sentence, in warn colour (and an amber dot) only when the owner should act, with the amber "Build the dispute pack" button.
- **Dispute pack.** The verdict in one line, then the evidence (door log, posted hours, order), the "what this does not show" list, and the draft. Download and copy are the actions; Shopfront never sends.
- **Lead.** `headline[0]` in large type, the rest as a short list. Order: orders not seen and couriers seen → doorbell presses while closed → staff at the door → traffic vs last week → busiest hour → after-hours.
- **Figures.** Exactly three numbers in one row, as three tiles (`--card-2`) inside the dark Deliveries card, the act-on number amber. They stay in one row on phones.
- **Chart.** Paired rounded bars per hour, today (lavender to pink) against last week (graphite), with a legend inline in the heading.
- **Stills.** Only frames Shopfront holds: a Ring frame (or one captured from live view, named as such) beside a delivery row, and on the pack the cited frame large plus up to two more around the scan, each captioned with its provenance. No drawings anywhere: a drawn "customer (guess)" adds doubt, not evidence. A pack with no frame says "No camera frame for this scan." in one line.
- **Needs a look.** A table of time, what happened, and why. Only rendered when there is something to show.
- **Every door event.** A collapsed `<details>` with the label, the classifier and the reason for each event. This is the proof surface.
- **Folds.** Each named `<details>` below the verdict is its own card (near-black; the scan check and Ask white), opened by a small square button with a thin border on the right.
- **Buttons.** Plain: near-black (light on a dark card or in dark mode). Secondary: the gradient pill with a pink glow (white on the hero). Small taps (All there, Short, Damaged): thin-bordered pills. Badges: small outlined pills, dashed for demo and replay.
- **Footer.** The data source and what actually labelled the day (counts by Ring, Bedrock and rules), with the Ring X-Request-ID, funnel and JSON links under Details.

## States

| State | What shows |
|---|---|
| Synthetic | Banner on every page (or the page's own watermark on the dispute page and /report) |
| Live | Footer with the Ring request ID |
| Ring error | `ring_error.html` with a plain sentence, "Reconnect Ring", and the named `RingStatus` under Details |
| Ring sign-in expired (dashboard) | The receiving desk from orders and Ring's webhooks, with a notice; unconfirmed orders read "not confirmed", never "no courier seen", and the scan check waits for Ring |
| Bad link or input | `error.html`: a title, one sentence and a way back; the HTTP status and detail under Details |
| Demo or replayed data in a pack | The watermark ("Demo data: don't send this pack as evidence") above the verdict, on screen and in print. What leaves the page stays marked: the draft's subject starts "[DEMO]" (the copy button copies it with the body), and pack.txt and the zip README start "DEMO DATA, not from a camera" |
| Empty day | "No door activity recorded for <date>." |
| Closed day | "The shop was closed." Doorbell presses while closed are still listed. |
| No orders yet | One sentence on what Shopfront does, with links to import orders, check a scan and the dispute pack. |
| First week after linking | "No history for the same day last week yet." |

## Don't

- No grid of 6+ stat tiles; the only cards inside cards are the three figure tiles and the three steps.
- No security-camera language (intruders, threats). This is a business tool. "Smart Alert" only as Ring's product name.
- No live video on the dashboard; it is about orders and yesterday. `/live` is a separate page because the Ring Developer Playground plays its simulated scenes only over the live-view stream, so that is where the simulator is shown and where frames reach Bedrock.
