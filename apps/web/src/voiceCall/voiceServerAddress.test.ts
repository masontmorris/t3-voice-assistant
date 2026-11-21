import { expect, it } from "vite-plus/test";
import { voiceServerReach } from "./voiceServerAddress";

const reach = (address: string, pageProtocol = "http:", canCapture = true) => {
  const result = voiceServerReach({ address, secret: "s", pageProtocol, canCapture });
  return result.kind === "ready" ? result.url : result.kind;
};

it.each([
  ["127.0.0.1:8770", "http:", "ws://127.0.0.1:8770/"],
  ["127.8.9.10:8770", "http:", "ws://127.8.9.10:8770/"],
  ["http://localhost:8770", "https:", "ws://localhost:8770/"],
  ["ws://[::1]:8770", "t3code:", "ws://[::1]:8770/"],
  ["100.101.1.2:8770", "http:", "ws://100.101.1.2:8770/"],
  ["wss://100.64.0.1:8446", "https:", "wss://100.64.0.1:8446/"],
  ["https://desk.tail1234.ts.net:8446", "https:", "wss://desk.tail1234.ts.net:8446/"],
  ["WSS://Desk.Tail1234.TS.NET:8446/voice", "https:", "wss://desk.tail1234.ts.net:8446/voice"],
])("reaches %s from an %s page", (address, page, url) => {
  expect(reach(address, page)).toBe(url);
});

it.each([
  // A name the browser must resolve is direct only when TLS proves whose it is.
  ["ws://desk.tail1234.ts.net:8770", "needs-tls"],
  ["desk.tail1234.ts.net:8770", "needs-tls"],
  ["voice.example.com", "not-direct"],
  ["wss://voice.trycloudflare.com", "not-direct"],
  ["wss://ts.net", "not-direct"],
  ["wss://desk.tail1234.ts.net.:8446", "not-direct"],
  ["wss://desk.ts.net.evil.com", "not-direct"],
  ["192.168.1.20:8770", "not-direct"],
  ["100.128.0.1:8770", "not-direct"],
  ["100.63.255.255:8770", "not-direct"],
  // Encodings URL parsing would turn into loopback or a tailnet address.
  ["ws://2130706433:8770", "not-direct"],
  ["ws://0x7f.0.0.1:8770", "not-direct"],
  ["ws://0177.0.0.1:8770", "not-direct"],
  ["ws://127.1:8770", "not-direct"],
  ["ws://127.0.0.1.:8770", "not-direct"],
  ["ws://localhost.:8770", "not-direct"],
  ["ws://100.064.0.1:8770", "not-direct"],
  ["ws://[::ffff:127.0.0.1]:8770", "not-direct"],
  ["ws://[::ffff:7f00:1]:8770", "not-direct"],
  ["ws://[0:0:0:0:0:0:0:1]:8770", "not-direct"],
  ["ws://user:pw@127.0.0.1:8770", "invalid"],
  ["ws://@127.0.0.1:8770", "invalid"],
  ["ws://127.0.0.1:8770/?secret=x", "invalid"],
  ["ws://127.0.0.1:8770/#x", "invalid"],
  ["ws://127.0.0.1:99999", "invalid"],
  ["ftp://127.0.0.1", "invalid"],
])("refuses %s", (address, kind) => {
  expect(reach(address)).toBe(kind);
});

it("keeps a secure page off plain ws:// except to loopback", () => {
  expect(reach("100.101.1.2:8770", "https:")).toBe("insecure");
  expect(reach("100.101.1.2:8770", "t3code:")).toBe("insecure");
  expect(reach("127.0.0.1:8770", "t3code:")).toBe("ws://127.0.0.1:8770/");
});

it("needs an address, a secret and a microphone", () => {
  const unset = (address: string, secret: string) =>
    voiceServerReach({ address, secret, pageProtocol: "http:", canCapture: true }).kind;
  expect(unset("", "s")).toBe("unset");
  expect(unset("127.0.0.1:8770", " ")).toBe("unset");
  expect(reach("127.0.0.1:8770", "http:", false)).toBe("no-microphone");
});
