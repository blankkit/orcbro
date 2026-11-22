// Permission to send pushes, per bridge.
//
// A bridge obtains an ID and key once (`POST /v1/bridges`). The key is an HMAC of the ID using the relay secret;
// the relay does not need to store anything for this. A push token belongs to the first bridge that uses it:
// the relay stores only a token hash and bridge ID in KV. The binding is refreshed on use and expires after
// seven days without a push; another bridge may then use the token.

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

/** New bridge: random ID and its key. */
export async function issue(secret: string): Promise<{ bridge: string; key: string }> {
  const bridge = [...crypto.getRandomValues(new Uint8Array(16))].map((b) => b.toString(16).padStart(2, "0")).join("");
  const key = base64url(await crypto.subtle.sign("HMAC", await hmacKey(secret), label(bridge)));
  return { bridge, key };
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

async function tokenKey(token: string): Promise<string> {
  const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(token.toLowerCase()));
  return "t:" + [...new Uint8Array(digest)].map((b) => b.toString(16).padStart(2, "0")).join("");
}

/** May `bridge` send to `token`? Bind the token to the bridge if it is unclaimed. */
export async function claim(store: TokenStore, token: string, bridge: string, now = Date.now() / 1000): Promise<boolean> {
  const key = await tokenKey(token);
  const { value, metadata } = await store.getWithMetadata<{ at: number }>(key);
  if (value !== null && value !== bridge) return false;
  if (value === null || !metadata || now - metadata.at > REFRESH_SECONDS) {
    await store.put(key, bridge, { expirationTtl: BIND_SECONDS, metadata: { at: Math.floor(now) } });
  }
  return true;
}
