// Validate bridge relay requests: only Live Activity pushes and app notifications in the format produced by
// the bridge (bridge/live.py, bridge/notify.py). The relay builds visible texts from templates (templates.ts)
// and rejects other uses. Live Activity payloads are rebuilt from the known fields (others are dropped), with
// texts shortened and dates kept close to the push's timestamp.

import { alert, clean, LIMITS, shorten, startAlert, TEMPLATES, type Template } from "./templates.ts";

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

const isNumber = (value: unknown): value is number => typeof value === "number" && Number.isFinite(value);

// Accept bridge clocks up to a day off; dates in the payload must stay close to its timestamp.
export const CLOCK_SKEW_SECONDS = 24 * 3600;
const MAX_STALE_SECONDS = 24 * 3600;
const MAX_DISMISSAL_SECONDS = 4 * 3600; // iOS removes an ended activity after four hours at most.
const MAX_FINISH_SECONDS = 60 * 24 * 3600;
const STATE_LIMITS = { stage: 120, error: 200 };
const PHASE = /^[A-Z_]{2,16}$/;
const PRINTER_ID = /^(p[0-9a-f]{16})?$/;

/** A whole number within [min, max], or null (missing, null or out of range); `undefined` for another type. */
function bounded(value: unknown, min: number, max: number): number | null | undefined {
  if (value === null || value === undefined) return null;
  if (!isNumber(value)) return undefined;
  const whole = Math.round(value);
  return whole >= min && whole <= max ? whole : null;
}

/** `PrintActivityAttributes.ContentState` (the app's Codable contract) from the bridge's content state. Keys the
 * bridge left out stay out; unknown keys are dropped. */
function contentState(input: Record<string, unknown>, timestamp: number): Record<string, unknown> | string {
  const { phase, progress } = input;
  if (typeof phase !== "string" || !PHASE.test(phase)) return "Invalid phase";
  const jobName = shorten(input.jobName, LIMITS.job);
  if (!jobName) return "Invalid jobName";
  if (!isNumber(progress)) return "Invalid progress";
  const state: Record<string, unknown> = { phase, jobName, progress: Math.min(100, Math.max(0, Math.round(progress))) };
  const numbers: [string, number, number][] = [
    ["layer", 0, 1_000_000], ["totalLayers", 0, 1_000_000], ["remainingMinutes", 0, MAX_FINISH_SECONDS / 60],
    ["finishAt", timestamp - MAX_FINISH_SECONDS, timestamp + MAX_FINISH_SECONDS],
  ];
  for (const [key, min, max] of numbers) {
    if (!(key in input)) continue;
    const value = bounded(input[key], min, max);
    if (value === undefined) return `Invalid ${key}`;
    state[key] = value;
  }
  for (const key of ["stage", "error"] as const) {
    if (!(key in input)) continue;
    const value = input[key];
    if (value !== null && typeof value !== "string") return `Invalid ${key}`;
    state[key] = shorten(value, STATE_LIMITS[key]);
  }
  return state;
}

export function validate(input: unknown, now = Date.now() / 1000): Checked | { error: string } {
  if (!isObject(input)) return { error: "Expected a JSON object" };
  const { token, environment, priority = 5, payload, language = "en" } = input;
  if (typeof token !== "string" || !/^[0-9a-fA-F]{32,400}$/.test(token)) return { error: "Invalid token" };
  if (typeof environment !== "string" || !ENVIRONMENTS.has(environment)) return { error: "Invalid environment" };
  if (priority !== 5 && priority !== 10) return { error: "priority must be 5 or 10" };
  if (language !== "en" && language !== "de") return { error: "language must be en or de" };
  if (!isObject(payload) || Object.keys(payload).some((key) => key !== "aps") || !isObject(payload.aps)) {
    return { error: "payload must contain only aps" };
  }
  const source = payload.aps;
  const event = source.event;
  if (typeof event !== "string" || !EVENTS.has(event)) return { error: "Invalid aps.event" };
  if (!isNumber(source.timestamp)) return { error: "Missing aps.timestamp" };
  const timestamp = Math.floor(source.timestamp);
  if (Math.abs(timestamp - now) > CLOCK_SKEW_SECONDS) return { error: "aps.timestamp is out of range" };
  if (!isObject(source["content-state"])) return { error: "Missing aps.content-state" };
  // Custom alert texts are not allowed; the relay adds the start alert required for push to start.
  if ("alert" in source || "sound" in source) return { error: "aps.alert is set by the relay" };
  const state = contentState(source["content-state"], timestamp);
  if (typeof state === "string") return { error: state };
  const aps: Record<string, unknown> = { timestamp, event, "content-state": state };
  // Dates after the timestamp, at most a day (stale) or four hours (dismissal, end only) later.
  const dates: [string, boolean, number][] = [["stale-date", true, MAX_STALE_SECONDS],
                                              ["dismissal-date", event === "end", MAX_DISMISSAL_SECONDS]];
  for (const [key, allowed, limit] of dates) {
    const value = source[key];
    if (!allowed || value === undefined) continue;
    if (!isNumber(value)) return { error: `Invalid aps.${key}` };
    aps[key] = Math.min(timestamp + limit, Math.max(timestamp, Math.floor(value)));
  }
  if (event === "start") {
    if (source["attributes-type"] !== ATTRIBUTES_TYPE) return { error: "Invalid attributes-type" };
    const attributes = source.attributes;
    if (!isObject(attributes)) return { error: "start requires attributes" };
    const printerName = shorten(attributes.printerName, LIMITS.printer);
    if (!printerName) return { error: "Invalid jobName or printerName" };
    const printerId = attributes.printerId ?? "";
    if (typeof printerId !== "string" || !PRINTER_ID.test(printerId)) return { error: "Invalid printerId" };
    aps["attributes-type"] = ATTRIBUTES_TYPE;
    aps.attributes = "printerId" in attributes ? { printerName, printerId } : { printerName };
    aps.alert = startAlert(state.jobName as string, printerName, language);
  }
  const outbound = { aps };
  const body = JSON.stringify(outbound);
  if (new TextEncoder().encode(body).length > MAX_PAYLOAD_BYTES) return { error: "payload is too large" };
  return { token: token.toLowerCase(), environment: environment as Checked["environment"], priority, payload: outbound, body };
}

export interface CheckedNotification {
  token: string;
  environment: "development" | "production";
  body: string;
  /** Event ID chosen by the bridge: the relay delivers each one only once per token. */
  id: string | null;
}

/** Notification (print finished, paused, etc.): `{token, environment, template, job, printer, detail?, thread?,
 * language?, id?}`. */
export function validateNotification(input: unknown): CheckedNotification | { error: string } {
  if (!isObject(input)) return { error: "Expected a JSON object" };
  const { token, environment, template, thread, language = "en", id = null } = input;
  if (typeof token !== "string" || !/^[0-9a-fA-F]{32,400}$/.test(token)) return { error: "Invalid token" };
  if (typeof environment !== "string" || !ENVIRONMENTS.has(environment)) return { error: "Invalid environment" };
  if (typeof template !== "string" || !TEMPLATES.has(template as Template)) return { error: "Invalid template" };
  if (language !== "en" && language !== "de") return { error: "language must be en or de" };
  const known = new Set(["token", "environment", "template", "job", "printer", "detail", "thread", "language", "id"]);
  if (Object.keys(input).some((key) => !known.has(key))) return { error: "Unknown fields" };
  const job = clean(input.job, LIMITS.job);
  const printer = clean(input.printer, LIMITS.printer);
  const detail = input.detail === undefined || input.detail === null ? null : clean(input.detail, LIMITS.detail);
  if (!job || !printer) return { error: "Invalid job or printer" };
  if (detail === null && input.detail !== undefined && input.detail !== null) return { error: "Invalid detail" };
  if (template === "error" && !detail) return { error: "error requires detail" };
  if (thread !== undefined && (typeof thread !== "string" || !/^p[0-9a-f]{16}$/.test(thread))) return { error: "Invalid thread" };
  if (id !== null && (typeof id !== "string" || !/^[0-9A-Za-z_-]{8,64}$/.test(id))) return { error: "Invalid id" };
  const aps: Record<string, unknown> = { alert: alert(template as Template, job, printer, detail, language), sound: "default" };
  if (thread) aps["thread-id"] = thread;
  return { token: token.toLowerCase(), environment: environment as CheckedNotification["environment"], body: JSON.stringify({ aps }), id };
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
