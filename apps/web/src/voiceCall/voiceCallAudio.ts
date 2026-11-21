/** Float samples in [-1, 1] as little-endian PCM16, the voice server's upstream format. */
export function floatToPcm16(input: Float32Array): ArrayBuffer {
  const out = new Int16Array(input.length);
  for (let i = 0; i < input.length; i++) {
    const value = Math.max(-1, Math.min(1, Number.isFinite(input[i]) ? input[i]! : 0));
    out[i] = Math.round(value * 32767);
  }
  return out.buffer;
}

/** Reply PCM16 at the reply's rate as float samples at the device rate, linearly resampled like the voice page. */
export function pcm16ToDeviceFloat(
  bytes: ArrayBuffer,
  fromRate: number,
  toRate: number,
): Float32Array<ArrayBuffer> {
  const input = new Int16Array(bytes, 0, Math.floor(bytes.byteLength / 2));
  if (input.length === 0) return new Float32Array(0);
  const length =
    fromRate === toRate
      ? input.length
      : Math.max(1, Math.round((input.length * toRate) / fromRate));
  const out = new Float32Array(length);
  for (let i = 0; i < length; i++) {
    const at = length === input.length ? i : (i * (input.length - 1)) / (length - 1 || 1);
    const lo = Math.floor(at);
    const hi = Math.min(lo + 1, input.length - 1);
    out[i] = (input[lo]! + (input[hi]! - input[lo]!) * (at - lo)) / 32768;
  }
  return out;
}
