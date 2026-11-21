export type VoiceServerReach =
  | { readonly kind: "ready"; readonly url: string }
  | { readonly kind: "unset" }
  | { readonly kind: "invalid" }
  | { readonly kind: "not-direct" }
  | { readonly kind: "insecure" }
  | { readonly kind: "no-microphone" };

function isLoopback(host: string): boolean {
  return host === "localhost" || host === "[::1]" || /^127(?:\.\d{1,3}){3}$/.test(host);
}

// Tailscale's CGNAT range, 100.64.0.0/10, and its MagicDNS names.
function isTailnet(host: string): boolean {
  if (host.endsWith(".ts.net")) return true;
  const octets = host.match(/^100\.(\d{1,3})\.\d{1,3}\.\d{1,3}$/);
  return octets !== null && Number(octets[1]) >= 64 && Number(octets[1]) <= 127;
}

/**
 * Where a call's audio may go: only a voice server on this machine or the user's tailnet, so
 * call audio never crosses T3 Connect, the relay, or a public tunnel.
 */
export function voiceServerReach(input: {
  readonly address: string;
  readonly secret: string;
  readonly pageProtocol: string;
  readonly canCapture: boolean;
}): VoiceServerReach {
  const address = input.address.trim();
  if (!address || !input.secret.trim()) return { kind: "unset" };
  let url: URL;
  try {
    url = new URL(address.includes("://") ? address : `ws://${address}`);
  } catch {
    return { kind: "invalid" };
  }
  const protocol = { "ws:": "ws:", "http:": "ws:", "wss:": "wss:", "https:": "wss:" }[url.protocol];
  if (!protocol || url.username || url.password || url.search || url.hash) {
    return { kind: "invalid" };
  }
  const host = url.hostname.toLowerCase();
  if (!isLoopback(host) && !isTailnet(host)) return { kind: "not-direct" };
  // A secure page (https, or the desktop app's own scheme) may open ws:// only to loopback.
  if (input.pageProtocol !== "http:" && protocol === "ws:" && !isLoopback(host)) {
    return { kind: "insecure" };
  }
  if (!input.canCapture) return { kind: "no-microphone" };
  return { kind: "ready", url: `${protocol}//${url.host}${url.pathname}` };
}
