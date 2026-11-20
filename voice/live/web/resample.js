// Linear resampling and PCM16 conversion shared by the page. The capture worklet posts raw microphone
// Float32 to the main thread, and this is where it becomes the 16 kHz mono PCM16 the server reads, the
// same linear interpolation the Python client uses. Node's test runner covers it in resample.test.js.
// Loaded by the page as an ES module; no build step.
export function resampleLinear(input, fromRate, toRate) {
  if (!(fromRate > 0) || !(toRate > 0)) throw new RangeError("rates must be positive");
  if (input.length === 0) return new Float32Array(0);
  if (fromRate === toRate) return Float32Array.from(input);
  const outLength = Math.max(1, Math.round((input.length * toRate) / fromRate));
  const out = new Float32Array(outLength);
  for (let i = 0; i < outLength; i += 1) {
    const at = (i * (input.length - 1)) / (outLength - 1 || 1);
    const lo = Math.floor(at);
    const hi = Math.min(lo + 1, input.length - 1);
    out[i] = input[lo] + (input[hi] - input[lo]) * (at - lo);
  }
  return out;
}

export function floatToPCM16(input) {
  const out = new Int16Array(input.length);
  for (let i = 0; i < input.length; i += 1) {
    let v = input[i];
    if (!Number.isFinite(v)) v = 0;
    if (v > 1) v = 1;
    else if (v < -1) v = -1;
    out[i] = Math.round(v * 32767);
  }
  return new Uint8Array(out.buffer, out.byteOffset, out.byteLength);
}

export function pcm16ToFloat32(bytes) {
  let view;
  if (bytes instanceof Int16Array) view = bytes;
  else if (bytes instanceof ArrayBuffer) view = new Int16Array(bytes);
  else view = new Int16Array(bytes.buffer, bytes.byteOffset, Math.floor(bytes.byteLength / 2));
  const out = new Float32Array(view.length);
  for (let i = 0; i < view.length; i += 1) out[i] = view[i] / 32768;
  return out;
}

// Microphone Float32 at the device rate becomes the server's 16 kHz mono PCM16 bytes.
export function micToServerPCM16(input, fromRate) {
  return floatToPCM16(resampleLinear(input, fromRate, 16000));
}

// Reply PCM16 bytes at the reply rate become Float32 at the device rate for the playback worklet.
export function replyToDeviceFloat(bytes, replyRate, deviceRate) {
  return resampleLinear(pcm16ToFloat32(bytes), replyRate, deviceRate);
}

// PCM16 bytes as base64, for a text frame. Built in slices so a long run never overflows the argument limit.
export function bytesToBase64(bytes) {
  let text = "";
  for (let i = 0; i < bytes.length; i += 0x8000) {
    text += String.fromCharCode.apply(null, bytes.subarray(i, i + 0x8000));
  }
  return btoa(text);
}

// Where a device-clock frame falls in the microphone stream the server received. `blocks` are the sent microphone
// blocks, oldest first: {frame, frames} is where one began on the clock and how long it was, {before, samples} how
// many 16 kHz samples were sent before it and in it. The blocks resample to a rounded length, so the stream's own
// index drifts from the clock; the recording is on the stream's index, which this finds. A frame inside the block
// still being filled continues the last block's rate. Returns null for a frame before the first block.
export function micIndexAt(blocks, frame) {
  for (let i = blocks.length - 1; i >= 0; i -= 1) {
    const block = blocks[i];
    if (block.frame <= frame) return block.before + ((frame - block.frame) * block.samples) / block.frames;
  }
  return null;
}

export function levelDbfs(input) {
  if (input.length === 0) return -100;
  let sum = 0;
  for (let i = 0; i < input.length; i += 1) sum += input[i] * input[i];
  const rms = Math.sqrt(sum / input.length);
  return rms > 0 ? 20 * Math.log10(rms) : -100;
}
