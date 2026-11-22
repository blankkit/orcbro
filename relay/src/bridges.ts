// Permission to send pushes, per bridge.
//
// A bridge obtains an ID and key once (`POST /v1/bridges`). The key is an HMAC of the ID using the relay secret;
// the relay does not need to store anything for this. While the secret is rotated, keys made with the previous
// secret still work and the bridge receives its key under the new secret, so it keeps its ID and its tokens.
// A push token belongs to the first bridge that uses it: the relay stores only a token hash and bridge ID in KV.
// The binding is refreshed on use and expires after seven days without a push; another bridge may then use the
// token. KV has no compare-and-swap, so a first claim is read back before it counts (see `bind`).

export interface TokenStore {
  getWithMetadata<M>(key: string): Promise<{ value: string | null; metadata: M | null }>;
  put(key: string, value: string, options: { expirationTtl: number; metadata: unknown }): Promise<void>;
}

export const BIND_SECONDS = 7 * 24 * 3600;
const REFRESH_SECONDS = 24 * 3600; // Reduce KV writes: refresh at most once a day
const BRIDGE_ID = /^[0-9a-f]{32}$/;

const base64url = (bytes: ArrayBuffer): string =>
  btoa(String.fromCharCode(...new Uint8Array(bytes))).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");

const fromBase64url = (text: string): Uint8Array | null => {
  try {
    return Uint8Array.from(atob(text.replace(/-/g, "+").replace(/_/g, "/")), (c) => c.charCodeAt(0));
  } catch {
    return null;
  }
};

const hmacKey = (secret: string) =>
  crypto.subtle.importKey("raw", new TextEncoder().encode(secret), { name: "HMAC", hash: "SHA-256" }, false, ["sign", "verify"]);

const label = (bridge: string) => new TextEncoder().encode(`bridge:${bridge}`);

const keyFor = async (secret: string, bridge: string): Promise<string> =>
  base64url(await crypto.subtle.sign("HMAC", await hmacKey(secret), label(bridge)));

/** New bridge: random ID and its key. */
export async function issue(secret: string): Promise<{ bridge: string; key: string }> {
  const bridge = [...crypto.getRandomValues(new Uint8Array(16))].map((b) => b.toString(16).padStart(2, "0")).join("");
  return { bridge, key: await keyFor(secret, bridge) };
}

/** `authorization: Bearer <id>.<key>` → bridge ID, otherwise `null` (constant-time comparison). */
export async function authenticate(header: string | null, secret: string): Promise<string | null> {
  const match = /^Bearer ([0-9a-f]{32})\.([A-Za-z0-9_-]{43})$/.exec(header ?? "");
  if (!match || !BRIDGE_ID.test(match[1])) return null;
  const signature = fromBase64url(match[2]);
  if (!signature) return null;
  const valid = await crypto.subtle.verify("HMAC", await hmacKey(secret), signature, label(match[1]));
  return valid ? match[1] : null;
}

/** Bridge ID for `authorization`, also accepting keys made with `previous` while the secret is rotated. `renewed`
 * is then the bridge's key under the current secret, which the relay hands back (`x-bridge-key`). */
export async function identify(header: string | null, secret: string, previous?: string):
    Promise<{ bridge: string; renewed: string | null } | null> {
  const bridge = await authenticate(header, secret);
  if (bridge) return { bridge, renewed: null };
  const old = previous ? await authenticate(header, previous) : null;
  return old ? { bridge: old, renewed: await keyFor(secret, old) } : null;
}

const hex = (bytes: ArrayBuffer): string => [...new Uint8Array(bytes)].map((b) => b.toString(16).padStart(2, "0")).join("");

export const digest = async (text: string): Promise<string> =>
  hex(await crypto.subtle.digest("SHA-256", new TextEncoder().encode(text)));

const tokenKey = async (token: string): Promise<string> => "t:" + (await digest(token.toLowerCase()));

export interface Binding {
  key: string;
  bridge: string | null;
  at: number | null;
}

/** Which bridge a token belongs to; nothing is written. */
export async function lookup(store: TokenStore, token: string): Promise<Binding> {
  const key = await tokenKey(token);
  const { value, metadata } = await store.getWithMetadata<{ at: number }>(key);
  return { key, bridge: value, at: typeof metadata?.at === "number" ? metadata.at : null };
}

/** Bind an unclaimed token to `bridge`, or refresh its own binding. False if another bridge holds it. */
export async function bind(store: TokenStore, binding: Binding, bridge: string, now = Date.now() / 1000): Promise<boolean> {
  if (binding.bridge !== null && binding.bridge !== bridge) return false;
  if (binding.bridge !== null && binding.at !== null && now - binding.at <= REFRESH_SECONDS) return true;
  await store.put(binding.key, bridge, { expirationTtl: BIND_SECONDS, metadata: { at: Math.floor(now) } });
  if (binding.bridge !== null) return true;
  // First claim: read it back, so that of two bridges claiming at once only the one whose write KV kept proceeds.
  // KV is eventually consistent across locations; claims made at the same moment elsewhere cannot be excluded.
  return (await store.getWithMetadata(binding.key)).value === bridge;
}

/** May `bridge` send to `token`? Bind the token to the bridge if it is unclaimed. */
export async function claim(store: TokenStore, token: string, bridge: string, now = Date.now() / 1000): Promise<boolean> {
  return bind(store, await lookup(store, token), bridge, now);
}
