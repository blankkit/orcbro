// Validate bridge relay requests: only Live Activity pushes and app notifications in the format produced by
// the bridge (bridge/live.py, bridge/notify.py). The relay builds visible texts from templates (templates.ts)
// and rejects other uses.

import { alert, clean, LIMITS, startAlert, TEMPLATES, type Template } from "./templates.ts";

export const ATTRIBUTES_TYPE = "PrintActivityAttributes";
export const MAX_PAYLOAD_BYTES = 4096; // APNs limit for Live Activity pushes
const EVENTS = new Set(["start", "update", "end"]);
const ENVIRONMENTS = new Set(["development", "production"]);

export interface Checked {
  token: string;
  environment: "development" | "production";
  priority: 5 | 10;
  payload: { aps: Record<string, unknown> };
  body: string;
}

const isObject = (value: unknown): value is Record<string, unknown> =>
  typeof value === "object" && value !== null && !Array.isArray(value);

export function validate(input: unknown): Checked | { error: string } {
  if (!isObject(input)) return { error: "Expected a JSON object" };
  const { token, environment, priority = 5, payload, language = "en" } = input;
  if (typeof token !== "string" || !/^[0-9a-fA-F]{32,400}$/.test(token)) return { error: "Invalid token" };
  if (typeof environment !== "string" || !ENVIRONMENTS.has(environment)) return { error: "Invalid environment" };
  if (priority !== 5 && priority !== 10) return { error: "priority must be 5 or 10" };
  if (language !== "en" && language !== "de") return { error: "language must be en or de" };
  if (!isObject(payload) || Object.keys(payload).some((key) => key !== "aps") || !isObject(payload.aps)) {
    return { error: "payload must contain only aps" };
  }
  const aps = payload.aps;
  if (typeof aps.event !== "string" || !EVENTS.has(aps.event)) return { error: "Invalid aps.event" };
  if (typeof aps.timestamp !== "number") return { error: "Missing aps.timestamp" };
  if (!isObject(aps["content-state"])) return { error: "Missing aps.content-state" };
  // Custom alert texts are not allowed; the relay adds the start alert required for push to start.
  if ("alert" in aps || "sound" in aps) return { error: "aps.alert is set by the relay" };
  if (aps.event === "start") {
    if (aps["attributes-type"] !== ATTRIBUTES_TYPE) return { error: "Invalid attributes-type" };
    if (!isObject(aps.attributes)) return { error: "start requires attributes" };
    const job = clean(aps["content-state"].jobName, LIMITS.job);
    const printer = clean(aps.attributes.printerName, LIMITS.printer);
    if (!job || !printer) return { error: "Invalid jobName or printerName" };
    aps.alert = startAlert(job, printer, language);
  }
  const body = JSON.stringify(payload);
  if (new TextEncoder().encode(body).length > MAX_PAYLOAD_BYTES) return { error: "payload is too large" };
  return { token: token.toLowerCase(), environment: environment as Checked["environment"], priority, payload: payload as Checked["payload"], body };
}

export interface CheckedNotification {
  token: string;
  environment: "development" | "production";
  body: string;
}

/** Notification (print finished, paused, etc.): `{token, environment, template, job, printer, detail?, thread?, language?}`. */
export function validateNotification(input: unknown): CheckedNotification | { error: string } {
  if (!isObject(input)) return { error: "Expected a JSON object" };
  const { token, environment, template, thread, language = "en" } = input;
  if (typeof token !== "string" || !/^[0-9a-fA-F]{32,400}$/.test(token)) return { error: "Invalid token" };
  if (typeof environment !== "string" || !ENVIRONMENTS.has(environment)) return { error: "Invalid environment" };
  if (typeof template !== "string" || !TEMPLATES.has(template as Template)) return { error: "Invalid template" };
  if (language !== "en" && language !== "de") return { error: "language must be en or de" };
  const known = new Set(["token", "environment", "template", "job", "printer", "detail", "thread", "language"]);
  if (Object.keys(input).some((key) => !known.has(key))) return { error: "Unknown fields" };
  const job = clean(input.job, LIMITS.job);
  const printer = clean(input.printer, LIMITS.printer);
  const detail = input.detail === undefined || input.detail === null ? null : clean(input.detail, LIMITS.detail);
  if (!job || !printer) return { error: "Invalid job or printer" };
  if (detail === null && input.detail !== undefined && input.detail !== null) return { error: "Invalid detail" };
  if (template === "error" && !detail) return { error: "error requires detail" };
  if (thread !== undefined && (typeof thread !== "string" || !/^p[0-9a-f]{16}$/.test(thread))) return { error: "Invalid thread" };
  const aps: Record<string, unknown> = { alert: alert(template as Template, job, printer, detail, language), sound: "default" };
  if (thread) aps["thread-id"] = thread;
  return { token: token.toLowerCase(), environment: environment as CheckedNotification["environment"], body: JSON.stringify({ aps }) };
}

/** Largest request accepted by the relay (Live Activity payloads are at most 4 KB). */
export const MAX_REQUEST_BYTES = 8192;

/** Read the body, stopping as soon as it exceeds `limit` bytes instead of buffering it all. `null`: too large. */
export async function readLimited(request: Request, limit = MAX_REQUEST_BYTES): Promise<string | null> {
  const declared = Number(request.headers.get("content-length"));
  if (Number.isFinite(declared) && declared > limit) return null;
  if (!request.body) return "";
  const reader = request.body.getReader();
  const chunks: Uint8Array[] = [];
  let size = 0;
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    size += value.byteLength;
    if (size > limit) {
      await reader.cancel();
      return null;
    }
    chunks.push(value);
  }
  const all = new Uint8Array(size);
  let offset = 0;
  for (const chunk of chunks) {
    all.set(chunk, offset);
    offset += chunk.byteLength;
  }
  return new TextDecoder().decode(all);
}
