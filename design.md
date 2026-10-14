# Shopfront design

**Feel:** morning paper, calm, factual. A briefing to read over coffee, not a security console.
**Direction:** one column, editorial type, rules instead of boxes. The first sentence is the most important thing that happened at the door.

## Tokens

The values live in [app/templates/_style.html](app/templates/_style.html) (`:root`, with dark mode under `prefers-color-scheme`). Never copy them elsewhere.

| Token | Light | Dark | Use |
|---|---|---|---|
| `--bg` | #f7f6f3 | #151517 | Page |
| `--ink` | #1d1d1f | #ececee | Text |
| `--muted` | #6b6b70 | #9a9aa1 | Labels, reasons, footer |
| `--line` | #e4e2dd | #2e2e33 | Rules between rows and figures |
| `--today` | #2f6fdb | #6b9cf0 | Today's data only |
| `--lw` | #c9c7c1 | #4a4a50 | Last week's data only |
| `--warn` / `--warn-bg` | #b4541a / #fbefe4 | #e48a52 / #3a2516 | Things the owner should act on, and the synthetic-data banner |

**Colour discipline:** about 85% neutral, 10% blue, 5% orange.
- Blue means "today" and is never a button or link colour.
- Orange means "act on this" or "this isn't real Ring data". It never decorates.
- Never put two accents in one component.

**Type:** system sans. h1 28/1.2/600 · lead sentence 22/1.35/600 · body 15/1.5 · labels 12 uppercase, tracking .05em. Monospace is only for env var names.
**Spacing:** 4px base. Sections are 28px apart. The width cap is 820px, with a 16px side gutter.

## Components

- **Banner** (synthetic mode, error page). Shows the source of truth. Full width, warn colours, one sentence.
- **Lead.** `headline[0]` in large type, the rest as a short list. Order: deliveries while closed → staff at the door → traffic vs last week → busiest hour → after-hours.
- **Figures.** Exactly three numbers in one row, divided by rules, with no cards. They stay in one row on phones.
- **Chart.** Paired bars per hour, today against last week, with a legend inline in the heading.
- **Needs a look.** A table of time, what happened, and why. Only rendered when there is something to show.
- **Every door event.** A collapsed `<details>` with the label, the classifier and the reason for each event. This is the proof surface.
- **Footer.** The data source, the last Ring X-Request-ID, the classifier, and links to the JSON.

## States

| State | What shows |
|---|---|
| Synthetic | Banner on every page |
| Live | Footer with the Ring request ID |
| Ring error | `ring_error.html` with the named `RingStatus` and one action |
| Empty day | "No door activity recorded for <date>." |
| Closed day | "The shop was closed." Deliveries while closed are still flagged. |
| First week after linking | "No history for the same day last week yet." |

## Don't

- No cards inside cards, no grid of 6+ stat tiles.
- No security-camera language (alerts, intruders, threats). This is a business briefing.
- No live video embed. The briefing is about yesterday.
