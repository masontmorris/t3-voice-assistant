export type VoiceServerReach =
  | { readonly kind: "ready"; readonly url: string }
  | { readonly kind: "unset" }
  | { readonly kind: "invalid" }
  | { readonly kind: "not-direct" }
  | { readonly kind: "needs-tls" }
  | { readonly kind: "insecure" }
  | { readonly kind: "no-microphone" };

type Host = "loopback" | "tailnet-ip" | "tailnet-name" | "other";

const SCHEMES: Partial<Record<string, "ws:" | "wss:">> = {
  "ws:": "ws:",
  "http:": "ws:",
  "wss:": "wss:",
  "https:": "wss:",
};

const OCTET = "(?:25[0-5]|2[0-4]\\d|1\\d\\d|[1-9]?\\d)";
const DOTTED_QUAD = new RegExp(`^${OCTET}\\.${OCTET}\\.${OCTET}\\.${OCTET}$`);
const LABEL = "[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?";
const TS_NET_NAME = new RegExp(`^(?:${LABEL}\\.)+ts\\.net$`);

/**
 * The host as typed, judged before URL parsing could normalize it: `0x7f.1`, `2130706433` or
 * `[::ffff:7f00:1]` would otherwise pass as loopback. Only canonical forms count.
 */
function classify(host: string): Host {
  if (host === "localhost" || host === "[::1]") return "loopback";
  if (DOTTED_QUAD.test(host)) {
    const [a = 0, b = 0] = host.split(".").map(Number);
    if (a === 127) return "loopback";
    if (a === 100 && b >= 64 && b <= 127) return "tailnet-ip";
    return "other";
  }
  return TS_NET_NAME.test(host) ? "tailnet-name" : "other";
}

/**
 * Where a call's audio and the secret may go. A browser cannot check what a name resolves to, so
 * only a name TLS authenticates (`wss://` to a `*.ts.net` node) or an address that needs no
 * resolution (loopback, `localhost`, a tailnet IP) is direct. The voice server checks its peer too.
 */
export function voiceServerReach(input: {
  readonly address: string;
  readonly secret: string;
  readonly pageProtocol: string;
  readonly canCapture: boolean;
}): VoiceServerReach {
  const address = input.address.trim();
  if (!address || !input.secret.trim()) return { kind: "unset" };
  const typed = address.match(/^(?:([a-z][a-z0-9+.-]*):\/\/)?([^/?#]*)([^?#]*)$/i);
  if (!typed) return { kind: "invalid" };
  const scheme = SCHEMES[`${(typed[1] ?? "ws").toLowerCase()}:`];
  const parts = (typed[2] ?? "").toLowerCase().match(/^(\[[^\]]*\]|[^:[\]@]+)(?::(\d{1,5}))?$/);
  const port = parts?.[2];
  if (!scheme || !parts?.[1] || (port !== undefined && Number(port) > 65_535)) {
    return { kind: "invalid" };
  }
  const host = parts[1];
  const kind = classify(host);
  if (kind === "other") return { kind: "not-direct" };
  if (kind === "tailnet-name" && scheme === "ws:") return { kind: "needs-tls" };
  // A secure page (https, or the desktop app's own scheme) may open ws:// only to loopback.
  if (input.pageProtocol !== "http:" && scheme === "ws:" && kind !== "loopback") {
    return { kind: "insecure" };
  }
  if (!input.canCapture) return { kind: "no-microphone" };
  return {
    kind: "ready",
    url: `${scheme}//${host}${port === undefined ? "" : `:${port}`}${typed[3] || "/"}`,
  };
}
