// Reply playback for the browser client. Keeps a FIFO of device-rate Float32 the main thread resampled
// from the reply's PCM16, and starts only once the prebuffer is queued or the reply has ended, the way the
// Python client's Player waits before its stream opens. Ducking multiplies the gain while the server weighs
// speech over the reply; a barge-in flushes everything still queued, like the Python client's buffer drop.
// With recording on, the samples it hands to the output, after the gain, go back to the main thread in runs, each
// stamped with the audio clock's frame it started on, so the recording is what was played rather than what was sent.
class PlaybackProcessor extends AudioWorkletProcessor {
  constructor() {
    super();
    this.queue = [];
    this.consumed = 0;
    this.queued = 0;
    this.playing = false;
    this.ended = false;
    this.gain = 1;
    this.prebuffer = 0;
    this.played = 0;
    this.recording = false;
    this.run = new Float32Array(4096);
    this.runLength = 0;
    this.runFrame = 0;
    this.port.onmessage = (event) => this.onMessage(event.data);
  }

  onMessage(message) {
    if (message.type === "config") {
      // Eighty milliseconds of reply audio queued before playback starts, as in client.py.
      this.prebuffer = Math.floor(message.deviceRate * 0.08);
    } else if (message.type === "feed") {
      if (message.pcm.length > 0) {
        this.queue.push(message.pcm);
        this.queued += message.pcm.length;
      }
      this.ended = !!message.end;
      if (!this.playing && (this.ended || this.queued >= this.prebuffer)) {
        this.playing = true;
      }
    } else if (message.type === "duck") {
      this.gain = 0.35;
    } else if (message.type === "unduck") {
      this.gain = 1;
    } else if (message.type === "record") {
      this.recording = !!message.on;
    } else if (message.type === "drain") {
      this.flushRun();
      this.port.postMessage({ type: "recorded" });
    } else if (message.type === "flush") {
      this.flushRun();
      this.queue = [];
      this.consumed = 0;
      this.queued = 0;
      this.playing = false;
      this.ended = false;
      this.played = 0;
      this.gain = 1;
    }
  }

  flushRun() {
    if (this.runLength === 0) return;
    this.port.postMessage({
      type: "played",
      frame: this.runFrame,
      pcm: this.run.slice(0, this.runLength),
    });
    this.runLength = 0;
  }

  record(out, count) {
    if (this.runLength > 0 && currentFrame !== this.runFrame + this.runLength) this.flushRun();
    if (this.runLength === 0) this.runFrame = currentFrame;
    this.run.set(out.subarray(0, count), this.runLength);
    this.runLength += count;
    if (this.runLength >= 2048) this.flushRun();
  }

  process(inputs, outputs) {
    const out = outputs[0][0];
    if (!this.playing) {
      out.fill(0);
      return true;
    }
    let offset = 0;
    while (offset < out.length) {
      if (this.queue.length === 0) {
        if (this.ended) {
          this.playing = false;
          this.ended = false;
          if (this.played) this.port.postMessage({ type: "drained", frames: this.played });
          this.played = 0;
        }
        break;
      }
      const head = this.queue[0];
      const need = out.length - offset;
      const take = Math.min(need, head.length - this.consumed);
      for (let i = 0; i < take; i += 1) out[offset + i] = head[this.consumed + i] * this.gain;
      this.consumed += take;
      this.queued -= take;
      this.played += take;
      offset += take;
      if (this.consumed >= head.length) {
        this.queue.shift();
        this.consumed = 0;
      }
    }
    if (this.recording && offset > 0) this.record(out, offset);
    if (offset < out.length) {
      out.fill(0, offset);
      this.flushRun();
    }
    return true;
  }
}

registerProcessor("voice-playback", PlaybackProcessor);
