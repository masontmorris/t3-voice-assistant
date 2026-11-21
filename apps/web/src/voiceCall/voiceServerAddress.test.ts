import { expect, it } from "vite-plus/test";
import { voiceServerReach } from "./voiceServerAddress";

const reach = (address: string, pageProtocol = "http:", canCapture = true) => {
  const result = voiceServerReach({ address, secret: "s", pageProtocol, canCapture });
  return result.kind === "ready" ? result : result.kind;
};

it.each([
  ["127.0.0.1:8770", "http:", { kind: "ready", url: "ws://127.0.0.1:8770/" }],
  ["http://localhost:8770", "https:", { kind: "ready", url: "ws://localhost:8770/" }],
  ["100.101.1.2:8770", "http:", { kind: "ready", url: "ws://100.101.1.2:8770/" }],
  [
    "https://desk.tail1234.ts.net:8446",
    "https:",
    { kind: "ready", url: "wss://desk.tail1234.ts.net:8446/" },
  ],
  ["100.101.1.2:8770", "https:", "insecure"],
  ["100.101.1.2:8770", "t3code:", "insecure"],
  ["127.0.0.1:8770", "t3code:", { kind: "ready", url: "ws://127.0.0.1:8770/" }],
  ["voice.example.com", "http:", "not-direct"],
  ["https://voice.trycloudflare.com", "https:", "not-direct"],
  ["192.168.1.20:8770", "http:", "not-direct"],
  ["100.128.0.1:8770", "http:", "not-direct"],
  ["ftp://127.0.0.1", "http:", "invalid"],
  ["ws://127.0.0.1:8770/?secret=x", "http:", "invalid"],
  ["ws://user:pw@127.0.0.1:8770", "http:", "invalid"],
])("reaches %s from an %s page only when the call stays direct", (address, page, expected) => {
  expect(reach(address, page)).toEqual(expected);
});

it("needs an address, a secret and a microphone", () => {
  expect(
    voiceServerReach({ address: "", secret: "s", pageProtocol: "http:", canCapture: true }),
  ).toEqual({ kind: "unset" });
  expect(
    voiceServerReach({
      address: "127.0.0.1:8770",
      secret: " ",
      pageProtocol: "http:",
      canCapture: true,
    }),
  ).toEqual({ kind: "unset" });
  expect(reach("127.0.0.1:8770", "http:", false)).toBe("no-microphone");
});
