// Microphone capture for the browser client. Downmixes to mono, accumulates device-rate Float32, and
// posts each block to the main thread with its RMS level, where resample.js turns it into the server's
// 16 kHz PCM16. The worklet itself never resamples, so this file holds no DSP to drift from resample.js.
class CaptureProcessor extends AudioWorkletProcessor {
  constructor() {
    super();
    this.buffer = new Float32Array(4096);
    this.length = 0;
    this.startFrame = 0;
    this.target = 2048;
  }

  process(inputs) {
    const channels = inputs[0];
    if (channels && channels.length > 0) {
      const first = channels[0];
      for (let i = 0; i < first.length; i += 1) {
        let sample = 0;
        for (let c = 0; c < channels.length; c += 1) sample += channels[c][i];
        sample /= channels.length;
        if (this.length === 0) this.startFrame = currentFrame + i;
        if (this.length === this.buffer.length) {
          const grown = new Float32Array(this.buffer.length * 2);
          grown.set(this.buffer);
          this.buffer = grown;
        }
        this.buffer[this.length] = sample;
        this.length += 1;
      }
      if (this.length >= this.target) {
        const block = this.buffer.slice(0, this.length);
        let sum = 0;
        for (let i = 0; i < block.length; i += 1) sum += block[i] * block[i];
        this.port.postMessage({
          type: "mic",
          pcm: block,
          level: Math.sqrt(sum / block.length),
          frame: this.startFrame,
        });
        this.length = 0;
      }
    }
    return true;
  }
}

registerProcessor("voice-capture", CaptureProcessor);
