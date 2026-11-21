import { afterEach, expect, it, vi } from "vite-plus/test";
import type { VoiceCallError } from "./voiceCallProtocol";
import { openVoiceCall } from "./voiceCallSession";

vi.mock("../speech/browserMicrophones", () => ({ chooseRecordingMicrophone: async () => "" }));
vi.mock("../speech/requestMicrophone", () => ({ requestMicrophone: vi.fn() }));
vi.mock("../speech/pcmWorklet.ts?worker&url", () => ({ default: "capture.js" }));
vi.mock("./voiceCallPlaybackWorklet.ts?worker&url", () => ({ default: "playback.js" }));

const { requestMicrophone } = await import("../speech/requestMicrophone");

type FailAt = "worklet" | "node" | "socket" | null;

afterEach(() => {
  vi.unstubAllGlobals();
});

function setup(failAt: FailAt = null) {
  let trackStopped!: () => void;
  const stopped = new Promise<void>((resolve) => (trackStopped = resolve));
  const track = {
    stopped: false,
    stop: () => {
      track.stopped = true;
      trackStopped();
    },
    addEventListener: () => {},
  };
  const media = { getTracks: () => [track] };
  let grant!: (stream: typeof media) => void;
  vi.mocked(requestMicrophone).mockReturnValue(
    new Promise((resolve) => (grant = resolve)) as unknown as Promise<MediaStream>,
  );
  const nodes: Array<{ disconnected: boolean }> = [];
  const sockets: FakeSocket[] = [];
  let socketMade!: (socket: FakeSocket) => void;
  const socket = new Promise<FakeSocket>((resolve) => (socketMade = resolve));
  const context = { closed: false };

  class FakeContext {
    readonly sampleRate = 48_000;
    readonly destination = {};
    readonly audioWorklet = {
      addModule: async () => {
        if (failAt === "worklet") throw new Error("worklet");
      },
    };
    resume = async () => {};
    close = async () => {
      context.closed = true;
    };
    createMediaStreamSource = () => ({ connect: () => {} });
  }
  class FakeNode {
    disconnected = false;
    readonly port = { onmessage: null, postMessage: () => {}, close: () => {} };
    constructor() {
      if (failAt === "node" && nodes.length === 1) throw new Error("node");
      nodes.push(this);
    }
    connect() {}
    disconnect() {
      this.disconnected = true;
    }
  }
  class FakeSocket {
    static readonly OPEN = 1;
    readyState = 0;
    closed = false;
    sent: unknown[] = [];
    binaryType = "";
    onopen: (() => void) | null = null;
    onmessage: ((event: { data: unknown }) => void) | null = null;
    onclose: ((event: { code: number }) => void) | null = null;
    constructor(readonly url: string) {
      if (failAt === "socket") throw new SyntaxError("socket");
      sockets.push(this);
      socketMade(this);
    }
    send(data: unknown) {
      this.sent.push(data);
    }
    close() {
      this.closed = true;
    }
  }
  vi.stubGlobal("AudioContext", FakeContext);
  vi.stubGlobal("AudioWorkletNode", FakeNode);
  vi.stubGlobal("WebSocket", FakeSocket);

  let endedWith!: (error: VoiceCallError | null) => void;
  const ended = new Promise<VoiceCallError | null>((resolve) => (endedWith = resolve));
  const onEnded = vi.fn((error: VoiceCallError | null) => endedWith(error));
  const session = openVoiceCall({
    url: "ws://127.0.0.1:8770/",
    secret: "s",
    environmentId: "env",
    threadId: "t1",
    onEvent: () => {},
    onEnded,
  });
  const released = () =>
    track.stopped && context.closed && nodes.every((node) => node.disconnected);
  return { grant: () => grant(media), session, ended, onEnded, socket, sockets, stopped, released };
}

it.each([
  ["worklet", "microphone-failed"],
  ["node", "microphone-failed"],
  ["socket", "unreachable"],
] as const)("releases the microphone when %s setup fails", async (failAt, error) => {
  const call = setup(failAt);
  call.grant();
  expect(await call.ended).toBe(error);
  expect(call.released()).toBe(true);
  expect(call.onEnded).toHaveBeenCalledTimes(1);
});

it.each([
  ["a socket that drops before ready", 1006, "ended-by-server"],
  ["the server refusing the thread", 4404, "thread-unavailable"],
  ["the server refusing the environment", 4409, "other-environment"],
] as const)("releases everything on %s", async (_case, code, error) => {
  const call = setup();
  call.grant();
  const socket = await call.socket;
  socket.onopen?.();
  expect(socket.sent[1]).toBe(
    JSON.stringify({ type: "hello", mode: "vad", thread: "t1", environment: "env" }),
  );
  socket.onclose?.({ code });
  expect(await call.ended).toBe(error);
  expect(call.released()).toBe(true);
});

it("a hang-up before the microphone opens stops it as soon as it arrives", async () => {
  const call = setup();
  call.session.hangUp();
  expect(await call.ended).toBe(null);
  call.grant();
  await call.stopped;
  expect(call.released()).toBe(true);
  expect(call.sockets).toHaveLength(0);
  expect(call.onEnded).toHaveBeenCalledTimes(1);
});
