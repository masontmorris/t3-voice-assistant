// Reply playback for the browser client. Keeps a FIFO of device-rate Float32 the main thread resampled
// from the reply's PCM16, and starts only once the prebuffer is queued or the reply has ended, the way the
// Python client's Player waits before its stream opens. Ducking multiplies the gain while the server weighs
// speech over the reply; a barge-in flushes everything still queued, like the Python client's buffer drop.
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
        this.port.postMessage({ type: "started", queued: this.queued });
      }
    } else if (message.type === "duck") {
      this.gain = 0.35;
    } else if (message.type === "unduck") {
      this.gain = 1;
    } else if (message.type === "flush") {
      this.queue = [];
      this.consumed = 0;
      this.queued = 0;
      this.playing = false;
      this.ended = false;
      this.gain = 1;
    }
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
          this.port.postMessage({ type: "drained" });
          break;
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
    if (offset < out.length) out.fill(0, offset);
    return true;
  }
}

registerProcessor("voice-playback", PlaybackProcessor);
