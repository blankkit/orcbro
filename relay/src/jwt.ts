// Authenticate with APNs using a token (ES256 JWT). Apple allows new tokens at most every 20 minutes and
// requires a new one within an hour. The issue time is therefore rounded to 40-minute windows: all worker
// instances generate tokens with the same `iat` within a window.

export const WINDOW_SECONDS = 40 * 60;

const base64url = (bytes: Uint8Array): string =>
  btoa(String.fromCharCode(...bytes)).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");

const encodeJSON = (value: unknown): string => base64url(new TextEncoder().encode(JSON.stringify(value)));

export function pemToDer(pem: string): ArrayBuffer {
  const base64 = pem.replace(/-----(BEGIN|END) PRIVATE KEY-----/g, "").replace(/\s+/g, "");
  const binary = atob(base64);
  const bytes = new Uint8Array(binary.length);
  for (let i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i);
  return bytes.buffer;
}

let cached: { token: string; window: number; keyID: string } | undefined;

export async function providerToken(pem: string, keyID: string, teamID: string, now = Date.now() / 1000): Promise<string> {
  const window = Math.floor(now / WINDOW_SECONDS) * WINDOW_SECONDS;
  if (cached && cached.window === window && cached.keyID === keyID) return cached.token;
  const key = await crypto.subtle.importKey("pkcs8", pemToDer(pem), { name: "ECDSA", namedCurve: "P-256" }, false, ["sign"]);
  const unsigned = `${encodeJSON({ alg: "ES256", kid: keyID })}.${encodeJSON({ iss: teamID, iat: window })}`;
  // WebCrypto returns the signature as r‖s (64 bytes), exactly the format JWT requires for ES256.
  const signature = new Uint8Array(await crypto.subtle.sign({ name: "ECDSA", hash: "SHA-256" }, key, new TextEncoder().encode(unsigned)));
  cached = { token: `${unsigned}.${base64url(signature)}`, window, keyID };
  return cached.token;
}

export function forgetProviderToken(): void {
  cached = undefined;
}
