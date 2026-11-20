"use strict";
// A 48 kHz sine reaches 16 kHz with the right length and frequency, and PCM16 clamps.
// Run with Node's built-in runner, no dependencies: node --test voice/live/web/resample.test.js
import { describe, it } from "node:test";
import assert from "node:assert/strict";
import {
  resampleLinear,
  floatToPCM16,
  pcm16ToFloat32,
  micToServerPCM16,
  levelDbfs,
  bytesToBase64,
  micIndexAt,
} from "./resample.js";

function sineHz(rate, hz, seconds) {
  const out = new Float32Array(Math.round(rate * seconds));
  for (let i = 0; i < out.length; i += 1) out[i] = Math.sin((2 * Math.PI * hz * i) / rate);
  return out;
}

function zeroCrossHz(samples, rate) {
  let crossings = 0;
  for (let i = 1; i < samples.length; i += 1) {
    if ((samples[i - 1] < 0) !== (samples[i] < 0)) crossings += 1;
  }
  return (crossings * rate) / (2 * (samples.length - 1));
}

describe("resampleLinear", () => {
  it("brings a 48 kHz sine to 16 kHz with its length and pitch intact", () => {
    const down = resampleLinear(sineHz(48000, 440, 1), 48000, 16000);
    assert.equal(down.length, 16000);
    assert.ok(Math.abs(zeroCrossHz(down, 16000) - 440) < 5, `got ${zeroCrossHz(down, 16000)} Hz`);
  });

  it("brings 16 kHz audio up to the 48 kHz device rate", () => {
    const up = resampleLinear(sineHz(16000, 440, 1), 16000, 48000);
    assert.equal(up.length, 48000);
    assert.ok(Math.abs(zeroCrossHz(up, 48000) - 440) < 5, `got ${zeroCrossHz(up, 48000)} Hz`);
  });

  it("copies at equal rates and keeps empty input empty", () => {
    const same = resampleLinear(sineHz(16000, 440, 0.1), 16000, 16000);
    assert.equal(same.length, 1600);
    assert.deepEqual(resampleLinear(new Float32Array(0), 48000, 16000), new Float32Array(0));
  });

  it("round-trips 16 kHz through 48 kHz within a small error", () => {
    const original = sineHz(16000, 440, 0.5);
    const back = resampleLinear(resampleLinear(original, 16000, 48000), 48000, 16000);
    let worst = 0;
    for (let i = 0; i < original.length; i += 1) worst = Math.max(worst, Math.abs(original[i] - back[i]));
    assert.ok(worst < 0.02, `worst sample error ${worst}`);
  });
});

describe("floatToPCM16", () => {
  it("clamps past full scale and silences non-finite samples", () => {
    const bytes = floatToPCM16(new Float32Array([1.5, -2, 0, NaN, Infinity, 0.5]));
    const view = new Int16Array(bytes.buffer, bytes.byteOffset, bytes.byteLength / 2);
    assert.deepEqual(Array.from(view), [32767, -32767, 0, 0, 0, 16384]);
  });
});

describe("pcm16ToFloat32", () => {
  it("decodes full scale both ways", () => {
    const bytes = floatToPCM16(new Float32Array([1, -1]));
    const floats = pcm16ToFloat32(bytes);
    assert.ok(Math.abs(floats[0] - 32767 / 32768) < 1e-6);
    assert.ok(Math.abs(floats[1] + 32767 / 32768) < 1e-6);
  });
});

describe("micToServerPCM16", () => {
  it("turns device-rate microphone audio into 16 kHz PCM16 mono", () => {
    const bytes = micToServerPCM16(sineHz(48000, 440, 1), 48000);
    assert.equal(bytes.byteLength, 16000 * 2);
    const floats = pcm16ToFloat32(bytes);
    assert.ok(Math.abs(zeroCrossHz(floats, 16000) - 440) < 5, `got ${zeroCrossHz(floats, 16000)} Hz`);
  });
});

describe("levelDbfs", () => {
  it("reads a full-scale sine near -3 dBFS and silence at the floor", () => {
    assert.ok(Math.abs(levelDbfs(sineHz(16000, 440, 0.1)) + 3.01) < 0.1);
    assert.equal(levelDbfs(new Float32Array(128)), -100);
  });
});

describe("bytesToBase64", () => {
  it("round-trips a run longer than one slice", () => {
    const bytes = Uint8Array.from({ length: 0x8000 * 2 + 5 }, (_, i) => i % 251);
    assert.deepEqual(new Uint8Array(Buffer.from(bytesToBase64(bytes), "base64")), bytes);
  });
});

describe("micIndexAt", () => {
  // 2048-frame blocks at 48 kHz send 683 samples, so the stream's index runs ahead of the clock.
  const blocks = [0, 1, 2].map((n) => ({ frame: 1000 + n * 2048, frames: 2048, before: n * 683, samples: 683 }));

  it("places a frame on the stream by the block it falls in, not by the nominal rate", () => {
    assert.equal(micIndexAt(blocks, 1000 + 2048 + 1024), 683 + 341.5);
    assert.equal(micIndexAt(blocks, 1000 + 2 * 2048), 2 * 683);
  });

  it("continues the last block's rate past its end and knows nothing before the first block", () => {
    assert.equal(micIndexAt(blocks, 1000 + 3 * 2048), 3 * 683);
    assert.equal(micIndexAt(blocks, 999), null);
    assert.equal(micIndexAt([], 5), null);
  });
});
