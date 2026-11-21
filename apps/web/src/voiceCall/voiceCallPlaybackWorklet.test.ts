import { expect, it, vi } from "vite-plus/test";
import type { PlaybackMessage } from "./voiceCallPlaybackWorklet";

type Processor = {
  readonly port: { onmessage: ((event: { data: PlaybackMessage }) => void) | null };
  process(inputs: Float32Array[][], outputs: Float32Array[][]): boolean;
};

async function loadProcessor() {
  using globals = new DisposableStack();
  const posted: unknown[] = [];
  class Worklet {
    readonly port = {
      onmessage: null as Processor["port"]["onmessage"],
      postMessage: (data: unknown) => posted.push(data),
    };
  }
  let Playback!: new () => Processor;
  for (const [name, value] of Object.entries({
    AudioWorkletProcessor: Worklet,
    registerProcessor: (_name: string, processor: typeof Playback) => {
      Playback = processor;
    },
  })) {
    const original = Object.getOwnPropertyDescriptor(globalThis, name);
    globals.defer(() => {
      if (original) Object.defineProperty(globalThis, name, original);
      else Reflect.deleteProperty(globalThis, name);
    });
    Object.defineProperty(globalThis, name, { value, configurable: true, writable: true });
  }
  vi.resetModules();
  await import("./voiceCallPlaybackWorklet");
  const processor = new Playback();
  const send = (data: PlaybackMessage) => processor.port.onmessage?.({ data });
  const render = (frames: number) => processor.process([], [[new Float32Array(frames)]]);
  return { posted, send, render };
}

it("reports a caption's mark only once its audio starts playing, and drops the rest on a barge-in", async () => {
  const { posted, send, render } = await loadProcessor();
  send({ type: "config", deviceRate: 1_000 });
  send({ type: "mark", id: 1 });
  send({ type: "feed", pcm: new Float32Array(100).fill(0.5) });
  send({ type: "mark", id: 2 });
  send({ type: "feed", pcm: new Float32Array(100).fill(0.5) });
  expect(posted).toEqual([]);
  render(50);
  expect(posted).toEqual([{ type: "mark", id: 1 }]);
  send({ type: "flush" });
  render(200);
  expect(posted).toEqual([{ type: "mark", id: 1 }]);

  send({ type: "mark", id: 3 });
  send({ type: "feed", pcm: new Float32Array(10).fill(0.5), end: true });
  render(50);
  expect(posted).toEqual([{ type: "mark", id: 1 }, { type: "mark", id: 3 }, "drained"]);
});
