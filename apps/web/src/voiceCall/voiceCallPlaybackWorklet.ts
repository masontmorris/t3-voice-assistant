declare class AudioWorkletProcessor {
  readonly port: MessagePort;
}
declare function registerProcessor(name: string, processor: typeof AudioWorkletProcessor): void;

export type PlaybackMessage =
  | { readonly type: "config"; readonly deviceRate: number }
  | { readonly type: "feed"; readonly pcm: Float32Array; readonly end?: boolean }
  | { readonly type: "duck" | "unduck" | "flush" };

/**
 * Reply playback for a live call, ported from voice/live/web/playback-worklet.js without its
 * recording path. Starts once 80 ms is queued or the reply ended, ducks while the server weighs
 * speech over the reply, and drops what is queued on a barge-in.
 */
class VoiceCallPlaybackProcessor extends AudioWorkletProcessor {
  private queue: Float32Array[] = [];
  private consumed = 0;
  private queued = 0;
  private playing = false;
  private ended = false;
  private gain = 1;
  private prebuffer = 0;
  private played = 0;

  constructor() {
    super();
    this.port.onmessage = ({ data }: MessageEvent<PlaybackMessage>) => this.onMessage(data);
  }

  private onMessage(message: PlaybackMessage) {
    if (message.type === "config") {
      this.prebuffer = Math.floor(message.deviceRate * 0.08);
    } else if (message.type === "feed") {
      if (message.pcm.length > 0) {
        this.queue.push(message.pcm);
        this.queued += message.pcm.length;
      }
      this.ended = message.end === true;
      if (!this.playing && (this.ended || this.queued >= this.prebuffer)) this.playing = true;
    } else if (message.type === "duck") {
      this.gain = 0.35;
    } else if (message.type === "unduck") {
      this.gain = 1;
    } else {
      this.queue = [];
      this.consumed = 0;
      this.queued = 0;
      this.playing = false;
      this.ended = false;
      this.played = 0;
      this.gain = 1;
    }
  }

  process(_inputs: Float32Array[][], outputs: Float32Array[][]): boolean {
    const out = outputs[0]?.[0];
    if (!out) return true;
    if (!this.playing) {
      out.fill(0);
      return true;
    }
    let offset = 0;
    while (offset < out.length) {
      const head = this.queue[0];
      if (!head) {
        if (this.ended) {
          this.playing = false;
          this.ended = false;
          // oxlint-disable-next-line unicorn/require-post-message-target-origin -- AudioWorklet MessagePorts do not accept a target origin
          if (this.played) this.port.postMessage("drained");
          this.played = 0;
        }
        break;
      }
      const take = Math.min(out.length - offset, head.length - this.consumed);
      for (let i = 0; i < take; i++) out[offset + i] = head[this.consumed + i]! * this.gain;
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

registerProcessor("t3-voice-call-playback", VoiceCallPlaybackProcessor);
