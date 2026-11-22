# Push relay for Live Activities and notifications (Orcbro)

Cloudflare Worker at **https://push.blankk.it**. Self-hosted Orcbro bridges do not have the app's APNs key; they send
Live Activity pushes and notifications here (`BRIDGE_PUSH_RELAY` on the bridge), and the worker forwards them to
APNs. Bridges with their own key for the app do not need the relay.

## Accepted requests

- `POST /v1/bridges`: register a bridge. Returns `{"bridge", "key"}`; the key is an HMAC of the ID using
  `RELAY_SECRET`, so the relay stores nothing for registration. Rate-limited per sender IP.
- `POST /v1/live-activity` with `Authorization: Bearer <bridge>.<key>` and `{"token", "environment", "priority": 5|10,
  "payload", "language"?}`. The payload uses exactly the bridge format (`bridge/live.py`): only `aps`, `event`
  start/update/end, `timestamp`, `content-state`, and for start, `attributes-type: PrintActivityAttributes` and
  `attributes`; at most 4 KB. The relay adds `alert` itself (the "Print Started" template using `jobName` and
  `printerName`).
- `POST /v1/notification` with the key and `{"token", "environment", "template", "job", "printer", "detail"?,
  "thread"?, "language"?}`. Templates: `finished`, `failed`, `cancelled`, `paused`, `error` (`src/templates.ts`);
  values must be short and free of control characters.

Both push endpoints accept an optional `"language": "en"|"de"`, defaulting to `"en"`. Any other value, including
`null`, returns 422. Start alerts and notification templates use that language, matching `bridge/notify.py`.
The bridge sends the language stored for the iPhone's push token; Live Activity content state is supplied by the
bridge in that token's language. Request errors are in English.

A token belongs to the first bridge that uses it (`src/bridges.ts`): KV `TOKENS` holds SHA-256 of the token → bridge
ID for seven days, refreshed on use at most once a day. Other bridges receive 403.

Responses: 200 delivered; 401 unknown bridge (register again); 403 token belongs to another bridge; 410 invalid device
token (the bridge discards it); 422 invalid format or language; 429 rate-limited (20 per device, 120 per bridge per
minute, registrations 5 per IP per minute); 502 other rejection by APNs; 503 not configured. `GET /health` reports
whether the key, team ID and relay secret are set.

## Privacy

Only the token hash → random bridge ID binding is stored, for at most seven days after the last push. Logs are off
(`observability.enabled: false`). Job names, progress and error texts only pass through the worker on their way to
Apple, as described in the app's privacy policy.

## Operations

```sh
node --test "test/**/*.test.ts"              # Node test runner, no dependencies or network needed
npx wrangler deploy                         # publishes at push.blankk.it (custom domain)
npx wrangler secret put APNS_KEY < AuthKey_JK2Q56X6XH.p8   # APNs .p8 key file (keep out of the repo)
npx wrangler secret put APNS_TEAM_ID        # Apple Developer account team ID
openssl rand -base64 32 | npx wrangler secret put RELAY_SECRET   # replacing it makes all bridges register again
curl https://push.blankk.it/health
```

Sign in to Cloudflare with `npx wrangler login`. The APNs token (ES256 JWT) is generated in 40-minute windows;
Apple allows new tokens at most every 20 minutes.
