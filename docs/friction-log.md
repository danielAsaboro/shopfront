# Shopfront friction log (Ring Partner API, Developer Playground, AWS)

| # | Task | Steps | Expected | Actual | Severity | Workaround | Suggestion |
|---|---|---|---|---|---|---|---|
| 1 | Get a server-side token | Read Authentication; tried `client_credentials` | A machine grant for a developer's own test devices | Only `authorization_code` and `refresh_token` exist; partner-initiated PKCE is invite-only | High | Playground token or a pasted refresh token | Document the Playground as the hackathon path at the top of Getting Started |
| 2 | Demo without a device | Searched docs, FAQ and site for "simulator" | A virtual device with events and frames | None found; Playground live view only | High | Playground live view | Publish a simulator, or remove "simulators" from the rules text |
| 3 | Compute "missed delivery" | Read the Event History fields | A signal that someone answered | Only `event_type`, start and end | Medium | Reframed as "doorbell while closed" | Add `answered`, or link the ding to its on-demand session |
| 4 | Verify webhooks | Compared the sample repo with the API reference | One scheme | Sample: Bearer `RING_WEBHOOK_SECRET`. Reference: HMAC `X-Signature` | Medium | Followed the reference | Align `ring-api-helloworld` |
| 5 | Request the right scope | Read the PKCE section | One scope string | Table `ava.v1:read`, example and response `ava` | Low | Not needed on the Playground path | Make them consistent |
| 6 | Compare with last week | Read Event History | Past weeks of history | Gated to after consent, so the first week has no baseline | Low | Show "no history yet" | Say so on the app listing |
| 7 | Download a frame | `POST /v1/devices/{id}/media/image/download` with `latest_in_range` over 24 h while Event History was empty | JPEG via a 303 | 403 FORBIDDEN (X-Request-ID `843c63da`) | Medium | Frame receipts only where a download works | Document whether Playground tokens may download frames |
