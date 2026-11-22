// Relay pushes from Orcbro bridges to Bambro: Live Activities and notifications (print finished, paused, etc.).
//
// Self-hosted bridges do not have the app's APNs key. They send pushes here; the worker validates them
// (only app Live Activities and notifications, with fixed text templates), checks the bridge's permission to send
// (bridges.ts), rate-limits per device and bridge, then forwards to APNs. Only the token hash → bridge ID binding
// is stored (seven days); content is neither stored nor logged.

import { authenticate, claim, issue, type TokenStore } from "./bridges.ts";
import { forgetProviderToken, providerToken } from "./jwt.ts";
import { readLimited, validate, validateNotification } from "./validate.ts";

interface RateLimit {
  limit(options: { key: string }): Promise<{ success: boolean }>;
}

export interface Env {
  APNS_KEY?: string; // Secret: contents of the .p8 file
  APNS_KEY_ID: string;
  APNS_TEAM_ID?: string; // Secret (not confidential, but kept out of the repository)
  RELAY_SECRET?: string; // Secret: used to derive bridge keys
  BUNDLE_ID: string;
  TOKENS: TokenStore;
  DEVICE_LIMIT: RateLimit;
  SENDER_LIMIT: RateLimit;
  REGISTER_LIMIT: RateLimit;
}

const HOSTS = { production: "https://api.push.apple.com", development: "https://api.sandbox.push.apple.com" };
// Responses that invalidate the device token; the bridge then discards it.
const GONE = new Set(["BadDeviceToken", "Unregistered", "DeviceTokenNotForTopic", "ExpiredToken"]);

const json = (body: unknown, status = 200, headers: Record<string, string> = {}): Response =>
  new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json", "cache-control": "no-store", ...headers } });

const tooMany = (error: string) => json({ error }, 429, { "retry-after": "60" });

export default {
  async fetch(request: Request, env: Env): Promise<Response> {
    const url = new URL(request.url);
    const ready = Boolean(env.APNS_KEY && env.APNS_TEAM_ID && env.RELAY_SECRET);
    if (url.pathname === "/health") return json({ ok: true, configured: ready });
    const routes = ["/v1/bridges", "/v1/live-activity", "/v1/notification"];
    if (!routes.includes(url.pathname)) return json({ error: "Not found" }, 404);
    if (request.method !== "POST") return json({ error: "POST only" }, 405, { allow: "POST" });
    if (!ready) return json({ error: "Relay is not configured" }, 503);

    if (url.pathname === "/v1/bridges") {
      const sender = request.headers.get("cf-connecting-ip") ?? "unbekannt";
      if (!(await env.REGISTER_LIMIT.limit({ key: sender })).success) return tooMany("Too many registrations");
      return json(await issue(env.RELAY_SECRET!), 201);
    }

    const bridge = await authenticate(request.headers.get("authorization"), env.RELAY_SECRET!);
    if (!bridge) return json({ error: "Bridge is not registered", reason: "UnknownBridge" }, 401);
    if (!(await env.SENDER_LIMIT.limit({ key: bridge })).success) return tooMany("Too many requests");
    const text = await readLimited(request);
    if (text === null) return json({ error: "Request is too large" }, 413);
    let input: unknown;
    try {
      input = JSON.parse(text);
    } catch {
      return json({ error: "Invalid JSON" }, 400);
    }

    const live = url.pathname === "/v1/live-activity";
    const checked = live ? validate(input) : validateNotification(input);
    if ("error" in checked) return json({ error: checked.error }, 422);
    if (!(await env.DEVICE_LIMIT.limit({ key: checked.token })).success) return tooMany("Too many pushes for this device");
    if (!(await claim(env.TOKENS, checked.token, bridge))) {
      return json({ error: "The token belongs to another bridge", reason: "TokenBoundElsewhere" }, 403);
    }

    const authorization = await providerToken(env.APNS_KEY!, env.APNS_KEY_ID, env.APNS_TEAM_ID!);
    const response = await fetch(`${HOSTS[checked.environment]}/3/device/${checked.token}`, {
      method: "POST",
      headers: {
        authorization: `bearer ${authorization}`,
        "apns-push-type": live ? "liveactivity" : "alert",
        "apns-topic": live ? `${env.BUNDLE_ID}.push-type.liveactivity` : env.BUNDLE_ID,
        "apns-priority": String("priority" in checked ? checked.priority : 10),
        "content-type": "application/json",
      },
      body: checked.body,
    });
    if (response.status === 200) return json({ ok: true });
    let reason = "unknown";
    try {
      reason = ((await response.json()) as { reason?: string }).reason ?? reason;
    } catch {
      // APNs returns JSON for errors; otherwise the reason remains unknown.
    }
    if (reason === "ExpiredProviderToken" || reason === "InvalidProviderToken") forgetProviderToken();
    // Forward device errors from APNs so the bridge discards stale tokens.
    const status = response.status === 410 || GONE.has(reason) ? 410 : response.status === 429 ? 429 : 502;
    return json({ error: "APNs rejected the push", apns_status: response.status, reason }, status);
  },
};
