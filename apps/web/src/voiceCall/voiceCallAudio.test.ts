import { expect, it } from "vite-plus/test";
import { floatToPcm16, pcm16ToDeviceFloat } from "./voiceCallAudio";

it("round-trips samples through PCM16 and clamps what is out of range", () => {
  const pcm = floatToPcm16(Float32Array.from([0, 0.5, -0.5, 2, Number.NaN]));
  expect(Array.from(new Int16Array(pcm))).toEqual([0, 16384, -16383, 32767, 0]);
  const back = pcm16ToDeviceFloat(pcm, 16_000, 16_000);
  expect(back[1]).toBe(0.5);
  expect(back[2]).toBeCloseTo(-0.5, 4);
});

it("resamples a reply to the device rate, keeping its duration and its ends", () => {
  const reply = floatToPcm16(Float32Array.from({ length: 2400 }, (_, i) => i / 2400));
  const device = pcm16ToDeviceFloat(reply, 24_000, 48_000);
  expect(device.length).toBe(4800);
  expect(device[0]).toBe(0);
  expect(device.at(-1)).toBeCloseTo(2399 / 2400, 3);
  expect(pcm16ToDeviceFloat(new ArrayBuffer(1), 24_000, 48_000)).toHaveLength(0);
});
