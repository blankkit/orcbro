# Push relay for Live Activities and notifications (Orcbro)

Cloudflare Worker at **https://push.blankk.it**. Self-hosted Orcbro bridges do not have the app's APNs key; they send
Live Activity pushes and notifications here (`BRIDGE_PUSH_RELAY` on the bridge), and the worker forwards them to
APNs. Bridges with their own key for the app do not need the relay.

## Accepted requests

- `POST /v1/bridges`: register a bridge. Returns `{"bridge", "key"}`; the key is an HMAC of the ID using
  `RELAY_SECRET`, so the relay stores nothing for registration. Rate-limited per sender IP.
- `POST /v1/live-activity` with `Authorization: Bearer <bridge>.<key>` and `{"token", "environment", "priority": 5|10,
  "payload", "language"?}`. The payload uses the bridge format (`bridge/live.py`): only `aps`, `event`
  start/update/end, `timestamp`, `content-state`, `stale-date`, for end `dismissal-date`, and for start,
  `attributes-type: PrintActivityAttributes` and `attributes` (`printerName`, `printerId`); at most 4 KB. The relay
  builds the outbound payload from these fields only (others are dropped) and from the app's content state
  (`phase`, `jobName`, `progress`, `layer`, `totalLayers`, `remainingMinutes`, `finishAt`, `stage`, `error`): texts
  are shortened (job 120, printer 60, stage 120, error 200 characters), numbers out of range become `null`,
  `timestamp` must be within a day of the relay's clock, `stale-date` at most a day and `dismissal-date` at most four
  hours after it, `finishAt` within 60 days. The relay adds `alert` itself (the "Print Started" template using
  `jobName` and `printerName`).
- `POST /v1/notification` with the key and `{"token", "environment", "template", "job", "printer", "detail"?,
  "thread"?, "language"?, "id"?}`. Templates: `finished`, `failed`, `cancelled`, `paused`, `error` (`src/templates.ts`);
  values must be short and free of control characters. With an `id` (8–64 of `A-Z a-z 0-9 _ -`), a notification
  reaches each token only once: a repeat within a day returns 200 `{"ok": true, "duplicate": true}` without a push.

Both push endpoints accept an optional `"language": "en"|"de"`, defaulting to `"en"`. Any other value, including
`null`, returns 422. Start alerts and notification templates use that language, matching `bridge/notify.py`.
The bridge sends the language stored for the iPhone's push token; Live Activity content state is supplied by the
bridge in that token's language. Request errors are in English.

A token belongs to the first bridge that uses it (`src/bridges.ts`): KV `TOKENS` holds SHA-256 of the token → bridge
ID for seven days, refreshed on use at most once a day. Other bridges receive 403. The relay checks the binding before
it counts a push against the device's rate limit, so another bridge cannot use up a phone's allowance; rejected
requests count only against the sending bridge. KV has no compare-and-swap: a first claim is read back and only counts
if KV kept it, which settles claims racing at the same Cloudflare location but not across locations. Binding a token
needs no approval from the app, so a bridge that knows a token can claim it once its binding has expired; tokens are
never exposed by the bridge or the relay.

Responses: 200 delivered; 401 unknown bridge (register again); 403 token belongs to another bridge; 410 invalid device
token (the bridge discards it); 422 invalid format or language; 429 rate-limited (20 per device, 120 per bridge per
minute, registrations 5 per IP per minute); 502 other rejection by APNs; 503 not configured. `GET /health` reports
whether the key, team ID and relay secret are set.

## Rotating the relay secret

Bridge keys are HMACs of the bridge ID, so a new `RELAY_SECRET` invalidates every key. To keep bridge IDs, and with
them the token bindings:

1. `npx wrangler secret put RELAY_SECRET_PREVIOUS` with the current secret, then put the new `RELAY_SECRET`.
2. Keys made with the previous secret keep working. Each response to such a key carries the bridge's key under the new
   secret in `x-bridge-key`; Orcbro stores it in `relay.json` and keeps its ID.
3. After at least seven days (`BIND_SECONDS`), `npx wrangler secret delete RELAY_SECRET_PREVIOUS`. A bridge that did not
   push in that time gets 401, registers again with a new ID, and has no live bindings left to conflict with.

If the old secret was exposed and must stop working at once, skip step 1. Every bridge then registers again with a new
ID; pushes to phones bound to its old ID fail with `TokenBoundElsewhere` until that binding expires (seven days after
its last push), and the bridge forgets those tokens. Each phone works again once its app registers the token again
after the binding expired (on its next launch). Older bridges, which ignore `x-bridge-key`, behave the same way once
the previous secret is deleted.

## Privacy

Only the token hash → random bridge ID binding is stored, for at most seven days after the last push, and for
notifications sent with an `id`, a hash of bridge, token and ID for one day. Logs are off
(`observability.enabled: false`). Job names, progress and error texts only pass through the worker on their way to
Apple, as described in the app's privacy policy.

## Operations

```sh
node --test "test/**/*.test.ts"              # Node test runner, no dependencies or network needed
npx wrangler deploy                         # publishes at push.blankk.it (custom domain)
npx wrangler secret put APNS_KEY < AuthKey_JK2Q56X6XH.p8   # APNs .p8 key file (keep out of the repo)
npx wrangler secret put APNS_TEAM_ID        # Apple Developer account team ID
openssl rand -base64 32 | npx wrangler secret put RELAY_SECRET   # rotate as described above
curl https://push.blankk.it/health
```

Sign in to Cloudflare with `npx wrangler login`. The APNs token (ES256 JWT) is generated in 40-minute windows;
Apple allows new tokens at most every 20 minutes.
