import assert from "node:assert/strict";
import { test } from "node:test";

import { authenticate, BIND_SECONDS, claim, issue } from "../src/bridges.ts";
import worker from "../src/index.ts";
import { forgetProviderToken, providerToken, WINDOW_SECONDS } from "../src/jwt.ts";
import { alert, type Language, type Template } from "../src/templates.ts";
import { readLimited, validate, validateNotification } from "../src/validate.ts";

const TOKEN = "ab".repeat(40);
const update = { aps: { timestamp: 1, event: "update", "content-state": { phase: "RUNNING", jobName: "Lid", progress: 3 } } };

test("accepts only Live Activity pushes in the bridge format", () => {
  const ok = validate({ token: TOKEN.toUpperCase(), environment: "production", priority: 10, payload: update });
  assert.ok(!("error" in ok));
  assert.equal(ok.token, TOKEN);
  const start = { aps: { ...update.aps, event: "start", "attributes-type": "PrintActivityAttributes",
                         attributes: { printerName: "P1S" } } };
  const started = validate({ token: TOKEN, environment: "development", payload: structuredClone(start) });
  assert.ok(!("error" in started));
  // The relay builds the start alert from a template; custom texts are rejected.
  assert.deepEqual(JSON.parse(started.body).aps.alert, { title: "Print Started", body: "Lid on P1S" });
  assert.ok("error" in validate({ token: TOKEN, environment: "production",
                                  payload: { aps: { ...start.aps, alert: { title: "Giveaway", body: "Click here" } } } }));
  assert.ok("error" in validate({ token: TOKEN, environment: "production", payload: { aps: { ...update.aps, alert: "x" } } }));
  const refused = [
    { token: "xyz", environment: "production", payload: update },
    { token: TOKEN, environment: "staging", payload: update },
    { token: TOKEN, environment: "production", priority: 1, payload: update },
    { token: TOKEN, environment: "production", payload: { ...update, extra: 1 } },
    { token: TOKEN, environment: "production", payload: { aps: { ...update.aps, event: "alert" } } },
    { token: TOKEN, environment: "production", payload: { aps: { ...start.aps, "attributes-type": "Other" } } },
    { token: TOKEN, environment: "production", payload: { aps: { ...update.aps, "content-state": { name: "x".repeat(5000) } } } },
  ];
  for (const input of refused) assert.ok("error" in validate(input), JSON.stringify(input).slice(0, 80));
});

test("Live Activity start alerts use English by default and accept English or German", () => {
  const start = { aps: { ...update.aps, event: "start", "attributes-type": "PrintActivityAttributes",
                         attributes: { printerName: "P1S" } } };
  for (const language of [undefined, "en", "de"] as const) {
    const checked = validate({ token: TOKEN, environment: "production", language, payload: structuredClone(start) });
    assert.ok(!("error" in checked));
    assert.deepEqual(JSON.parse(checked.body).aps.alert, language === "de"
      ? { title: "Druck gestartet", body: "Lid auf P1S" }
      : { title: "Print Started", body: "Lid on P1S" });
  }
});

async function testKey() {
  const pair = await crypto.subtle.generateKey({ name: "ECDSA", namedCurve: "P-256" }, true, ["sign", "verify"]);
  const der = new Uint8Array(await crypto.subtle.exportKey("pkcs8", pair.privateKey));
  const pem = `-----BEGIN PRIVATE KEY-----\n${btoa(String.fromCharCode(...der))}\n-----END PRIVATE KEY-----\n`;
  return { pem, publicKey: pair.publicKey };
}

const decode = (part: string) => Uint8Array.from(atob(part.replace(/-/g, "+").replace(/_/g, "/")), (c) => c.charCodeAt(0));

test("ES256 JWT with rounded iat, a valid signature and caching", async () => {
  forgetProviderToken();
  const { pem, publicKey } = await testKey();
  const now = 10 * WINDOW_SECONDS + 123;
  const token = await providerToken(pem, "KEYID12345", "TEAMID1234", now);
  const [header, claims, signature] = token.split(".");
  assert.deepEqual(JSON.parse(new TextDecoder().decode(decode(header))), { alg: "ES256", kid: "KEYID12345" });
  assert.deepEqual(JSON.parse(new TextDecoder().decode(decode(claims))), { iss: "TEAMID1234", iat: 10 * WINDOW_SECONDS });
  const valid = await crypto.subtle.verify({ name: "ECDSA", hash: "SHA-256" }, publicKey, decode(signature),
                                           new TextEncoder().encode(`${header}.${claims}`));
  assert.ok(valid);
  assert.equal(await providerToken(pem, "KEYID12345", "TEAMID1234", now + 60), token);
  assert.notEqual(await providerToken(pem, "KEYID12345", "TEAMID1234", now + WINDOW_SECONDS), token);
});

const SECRET = "test-relay-secret";

class MemoryStore {
  entries = new Map<string, { value: string; metadata: unknown }>();
  writes = 0;
  async getWithMetadata<M>(key: string) {
    const entry = this.entries.get(key);
    return { value: entry?.value ?? null, metadata: (entry?.metadata ?? null) as M | null };
  }
  async put(key: string, value: string, options: { metadata: unknown }) {
    this.writes += 1;
    this.entries.set(key, { value, metadata: options.metadata });
  }
}

function env(pem: string | undefined, allow = true, store = new MemoryStore()) {
  const limiter = { limit: async () => ({ success: allow }) };
  return { APNS_KEY: pem, APNS_KEY_ID: "KEYID12345", APNS_TEAM_ID: "TEAMID1234", BUNDLE_ID: "it.example.app",
           RELAY_SECRET: pem ? SECRET : undefined, TOKENS: store,
           DEVICE_LIMIT: limiter, SENDER_LIMIT: limiter, REGISTER_LIMIT: limiter };
}

let credentials = "";
const post = (body: unknown, path = "/v1/live-activity", auth: string | null = credentials) => new Request(`https://relay.example${path}`,
  { method: "POST", body: JSON.stringify(body),
    headers: { "cf-connecting-ip": "192.0.2.1", ...(auth ? { authorization: `Bearer ${auth}` } : {}) } });

test("bridge keys are issued and verified without stored state", async () => {
  const issued = await issue(SECRET);
  assert.match(issued.bridge, /^[0-9a-f]{32}$/);
  assert.equal(await authenticate(`Bearer ${issued.bridge}.${issued.key}`, SECRET), issued.bridge);
  assert.equal(await authenticate(`Bearer ${issued.bridge}.${issued.key}`, "other-secret"), null);
  const other = await issue(SECRET);
  assert.equal(await authenticate(`Bearer ${other.bridge}.${issued.key}`, SECRET), null);
  assert.equal(await authenticate(null, SECRET), null);
  assert.equal(await authenticate("Bearer broken", SECRET), null);
});

test("token belongs to the first bridge; binding refreshes infrequently and expires", async () => {
  const store = new MemoryStore();
  assert.ok(await claim(store, TOKEN, "a".repeat(32), 1000));
  assert.ok(await claim(store, TOKEN.toUpperCase(), "a".repeat(32), 1100));
  assert.equal(store.writes, 1, "no additional write within a day");
  assert.ok(!(await claim(store, TOKEN, "b".repeat(32), 1200)));
  assert.ok(await claim(store, TOKEN, "a".repeat(32), 1000 + 2 * 86400));
  assert.equal(store.writes, 2);
  assert.equal(BIND_SECONDS, 7 * 86400);
  assert.ok(![...store.entries.keys()].some((key) => key.includes(TOKEN)), "only a hash of the token");
});

test("notifications use only templates", () => {
  const ok = validateNotification({ token: TOKEN, environment: "production", template: "finished", job: "Lid",
                                    printer: "P1S", thread: "p0123456789abcdef" });
  assert.ok(!("error" in ok));
  assert.deepEqual(JSON.parse(ok.body), { aps: { alert: { title: "Print Finished", body: "Lid on P1S is done." },
                                                 sound: "default", "thread-id": "p0123456789abcdef" } });
  const paused = validateNotification({ token: TOKEN, environment: "production", template: "paused", job: "Lid",
                                        printer: "P1S", detail: "Out of\nfilament." });
  assert.equal(JSON.parse((paused as { body: string }).body).aps.alert.body, "Lid on P1S: Out of filament.");
  const refused = [
    { token: TOKEN, environment: "production", template: "custom", job: "x", printer: "y" },
    { token: TOKEN, environment: "production", template: "finished", job: "x", printer: "y", title: "Custom title" },
    { token: TOKEN, environment: "production", template: "finished", job: "x".repeat(121), printer: "y" },
    { token: TOKEN, environment: "production", template: "error", job: "x", printer: "y" },
    { token: TOKEN, environment: "production", template: "finished", job: "x", printer: "y", thread: "01P00A" },
  ];
  for (const input of refused) assert.ok("error" in validateNotification(input), JSON.stringify(input).slice(0, 90));
});

test("all notification templates use the selected language, defaulting to English", () => {
  const cases: { template: Template; detail: string | null; en: [string, string]; de: [string, string] }[] = [
    { template: "finished", detail: null,
      en: ["Print Finished", "Lid on P1S is done."], de: ["Druck fertig", "Lid auf P1S ist fertig."] },
    { template: "failed", detail: null,
      en: ["Print Failed", "Lid on P1S did not finish."], de: ["Druck fehlgeschlagen", "Lid auf P1S wurde nicht fertig."] },
    { template: "failed", detail: "Detail",
      en: ["Print Failed", "Lid on P1S: Detail"], de: ["Druck fehlgeschlagen", "Lid auf P1S: Detail"] },
    { template: "cancelled", detail: null,
      en: ["Print Canceled", "Lid on P1S was canceled."], de: ["Druck abgebrochen", "Lid auf P1S wurde abgebrochen."] },
    { template: "paused", detail: null,
      en: ["Print Paused", "Lid on P1S is paused."], de: ["Druck pausiert", "Lid auf P1S ist pausiert."] },
    { template: "paused", detail: "Detail",
      en: ["Print Paused", "Lid on P1S: Detail"], de: ["Druck pausiert", "Lid auf P1S: Detail"] },
    { template: "error", detail: "Detail",
      en: ["Message from P1S", "Detail"], de: ["Meldung von P1S", "Detail"] },
  ];
  for (const language of [undefined, "en", "de"] as const) {
    for (const entry of cases) {
      const checked = validateNotification({ token: TOKEN, environment: "production", language,
        template: entry.template, job: "Lid", printer: "P1S", detail: entry.detail });
      assert.ok(!("error" in checked));
      const [title, body] = entry[language ?? "en"];
      assert.deepEqual(JSON.parse(checked.body).aps.alert, { title, body });
    }
  }
  for (const language of ["en", "de"] as Language[]) {
    assert.deepEqual(alert("error", "Lid", "P1S", null, language), language === "de"
      ? { title: "Meldung von P1S", body: "Der Drucker meldet einen Fehler." }
      : { title: "Message from P1S", body: "The printer reports an error." });
  }
});

test("forwards to APNs and propagates device errors", async () => {
  const { pem } = await testKey();
  const calls: { url: string; init: RequestInit }[] = [];
  let reply = new Response(null, { status: 200 });
  const original = globalThis.fetch;
  globalThis.fetch = (async (url: string, init: RequestInit) => {
    calls.push({ url, init });
    return reply;
  }) as typeof fetch;
  try {
    const store = new MemoryStore();
    const registered = await worker.fetch(post({}, "/v1/bridges", null), env(pem, true, store));
    assert.equal(registered.status, 201);
    const issued = (await registered.json()) as { bridge: string; key: string };
    credentials = `${issued.bridge}.${issued.key}`;
    assert.equal((await worker.fetch(post({ token: TOKEN, environment: "development", payload: update }, "/v1/live-activity", null),
                                     env(pem, true, store))).status, 401);
    const ok = await worker.fetch(post({ token: TOKEN, environment: "development", priority: 10, payload: update }), env(pem, true, store));
    assert.equal(ok.status, 200);
    assert.equal(calls[0].url, `https://api.sandbox.push.apple.com/3/device/${TOKEN}`);
    const headers = calls[0].init.headers as Record<string, string>;
    assert.equal(headers["apns-topic"], "it.example.app.push-type.liveactivity");
    assert.equal(headers["apns-push-type"], "liveactivity");
    assert.equal(headers["apns-priority"], "10");
    assert.deepEqual(JSON.parse(calls[0].init.body as string), update);

    const note = await worker.fetch(post({ token: TOKEN, environment: "production", template: "finished", job: "Lid",
                                           printer: "P1S" }, "/v1/notification"), env(pem, true, store));
    assert.equal(note.status, 200);
    const alertHeaders = calls[1].init.headers as Record<string, string>;
    assert.equal(alertHeaders["apns-push-type"], "alert");
    assert.equal(alertHeaders["apns-topic"], "it.example.app");
    assert.equal(calls[1].url, `https://api.push.apple.com/3/device/${TOKEN}`);
    assert.deepEqual(JSON.parse(calls[1].init.body as string).aps.alert,
                     { title: "Print Finished", body: "Lid on P1S is done." });

    // Another bridge cannot send to this token.
    const intruder = await issue(SECRET);
    const refused = await worker.fetch(post({ token: TOKEN, environment: "production", payload: update }, "/v1/live-activity",
                                            `${intruder.bridge}.${intruder.key}`), env(pem, true, store));
    assert.equal(refused.status, 403);
    assert.equal(calls.length, 2);

    reply = Response.json({ reason: "BadDeviceToken" }, { status: 400 });
    const gone = await worker.fetch(post({ token: TOKEN, environment: "production", payload: update }), env(pem));
    assert.equal(gone.status, 410);
    assert.equal(((await gone.json()) as { reason: string }).reason, "BadDeviceToken");

    assert.equal((await worker.fetch(post({ token: "x" }), env(pem))).status, 422);
    assert.equal((await worker.fetch(post({ token: TOKEN, environment: "production", payload: update }), env(pem, false))).status, 429);
    assert.equal((await worker.fetch(post({}), env(undefined))).status, 503);
    assert.equal((await worker.fetch(new Request("https://relay.example/health"), env(undefined))).status, 200);
    assert.equal((await worker.fetch(new Request("https://relay.example/v1/live-activity"), env(pem))).status, 405);
  } finally {
    globalThis.fetch = original;
  }
});

test("both push endpoints forward localized alerts and return 422 for invalid languages", async () => {
  const { pem } = await testKey();
  const issued = await issue(SECRET);
  const auth = `${issued.bridge}.${issued.key}`;
  const environment = env(pem);
  const calls: RequestInit[] = [];
  const original = globalThis.fetch;
  globalThis.fetch = (async (_url: string, init: RequestInit) => {
    calls.push(init);
    return new Response(null, { status: 200 });
  }) as typeof fetch;
  try {
    for (const path of ["/v1/live-activity", "/v1/notification"]) {
      const input = path === "/v1/live-activity"
        ? { token: TOKEN, environment: "production", payload: { aps: { ...update.aps, event: "start",
            "attributes-type": "PrintActivityAttributes", attributes: { printerName: "P1S" } } } }
        : { token: TOKEN, environment: "production", template: "finished", job: "Lid", printer: "P1S" };
      for (const language of [undefined, "en", "de"] as const) {
        const response = await worker.fetch(post({ ...input, language }, path, auth), environment);
        assert.equal(response.status, 200);
        const isStart = path === "/v1/live-activity";
        assert.deepEqual(JSON.parse(calls.at(-1)!.body as string).aps.alert, language === "de"
          ? { title: isStart ? "Druck gestartet" : "Druck fertig", body: isStart ? "Lid auf P1S" : "Lid auf P1S ist fertig." }
          : { title: isStart ? "Print Started" : "Print Finished", body: isStart ? "Lid on P1S" : "Lid on P1S is done." });
      }
      const sent = calls.length;
      for (const language of ["fr", "EN", "de-DE", "", null, 0, false, [], {}]) {
        const response = await worker.fetch(post({ ...input, language }, path, auth), environment);
        assert.equal(response.status, 422, `${path}: ${JSON.stringify(language)}`);
        assert.deepEqual(await response.json(), { error: "language must be en or de" });
      }
      assert.equal(calls.length, sent);
    }
  } finally {
    globalThis.fetch = original;
  }
});

test("stops oversized requests while reading", async () => {
  const small = new Request("https://relay.example/v1/live-activity", { method: "POST", body: "{}" });
  assert.equal(await readLimited(small), "{}");
  const declared = new Request("https://relay.example/v1/live-activity", {
    method: "POST", body: "x", headers: { "content-length": "9000" } });
  assert.equal(await readLimited(declared), null);
  let pulled = 0;
  const endless = new ReadableStream({
    pull(controller) { pulled += 1; controller.enqueue(new Uint8Array(1024)); },
  });
  const streamed = new Request("https://relay.example/v1/live-activity", { method: "POST", body: endless, duplex: "half" } as RequestInit);
  assert.equal(await readLimited(streamed), null);
  assert.ok(pulled <= 10, `read only as much as needed (${pulled} KB)`);
});
