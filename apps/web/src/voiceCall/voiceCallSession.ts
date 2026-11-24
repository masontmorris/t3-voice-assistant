import * as Option from "effect/Option";
import { chooseRecordingMicrophone } from "../speech/browserMicrophones";
import captureWorkletUrl from "../speech/pcmWorklet.ts?worker&url";
import { requestMicrophone } from "../speech/requestMicrophone";
import { floatToPcm16, pcm16ToDeviceFloat } from "./voiceCallAudio";
import playbackWorkletUrl from "./voiceCallPlaybackWorklet.ts?worker&url";
import type { PlaybackMessage, PlaybackReport } from "./voiceCallPlaybackWorklet";
import {
  closeError,
  decodeVoiceServerMessage,
  type VoiceCallError,
  type VoiceServerMessage,
} from "./voiceCallProtocol";
import type { VoiceCallEvent } from "./voiceCallState";

export type VoiceCallSession = { readonly hangUp: () => void };

type Say = Extract<VoiceServerMessage, { type: "say" }>;

/**
 * One call to the voice server: the microphone goes up as 16 kHz PCM16, the reply comes down
 * and plays. The secret is the socket's first frame, never part of the URL. `onEnded` runs once,
 * never before this returns, with no error when the caller hung up. Every way a call ends releases
 * the microphone, the audio graph and the socket.
 */
export function openVoiceCall(input: {
  readonly url: string;
  readonly secret: string;
  readonly environmentId: string;
  readonly threadId: string;
  readonly onEvent: (event: VoiceCallEvent) => void;
  readonly onEnded: (error: VoiceCallError | null) => void;
}): VoiceCallSession {
  let context: AudioContext | undefined;
  let socket: WebSocket | undefined;
  let stream: MediaStream | undefined;
  const nodes: AudioWorkletNode[] = [];
  let ended = false;

  const end = (error: VoiceCallError | null) => {
    if (ended) return;
    ended = true;
    if (socket) {
      socket.onclose = socket.onmessage = socket.onopen = null;
      socket.close(1000);
    }
    for (const node of nodes) {
      node.disconnect();
      node.port.close();
    }
    stream?.getTracks().forEach((track) => track.stop());
    void context?.close().catch(() => undefined);
    input.onEnded(error);
  };

  // A caption waits for its audio: the playback worklet reports its mark when that audio plays,
  // and a barge-in drops the marks with the audio they belonged to.
  const sayings = new Map<number, Say>();
  let nextMark = 0;
  // Where a barge-in stopped the reply, for the server's trim: frames fed so far, the reply now
  // playing and how many frames had been fed when it began, and the one a barge-in asked about.
  type Playing = { turn: number; reply: number; from: number; rate: number };
  let fed = 0;
  let playing: Playing | null = null;
  let stopped: Playing | null = null;

  const wire = (audio: AudioContext, media: MediaStream) => {
    const mic = new AudioWorkletNode(audio, "t3-pcm-capture", {
      channelCount: 1,
      processorOptions: { call: true },
    });
    nodes.push(mic);
    const speaker = new AudioWorkletNode(audio, "t3-voice-call-playback");
    nodes.push(speaker);
    // oxlint-disable-next-line unicorn/require-post-message-target-origin -- AudioWorklet MessagePorts do not accept a target origin
    const play = (message: PlaybackMessage) => speaker.port.postMessage(message);
    play({ type: "config", deviceRate: audio.sampleRate });
    speaker.port.onmessage = ({ data }: MessageEvent<PlaybackReport>) => {
      if (data === "drained") {
        input.onEvent({ type: "drained" });
        return;
      }
      if (data.type === "position") {
        const at = stopped;
        stopped = null;
        if (at && socket?.readyState === WebSocket.OPEN) {
          const samples = Math.max(
            0,
            Math.round(((data.frames - at.from) * at.rate) / audio.sampleRate),
          );
          socket.send(
            JSON.stringify({ type: "position", turn: at.turn, reply: at.reply, samples }),
          );
        }
        return;
      }
      const said = sayings.get(data.id);
      sayings.delete(data.id);
      if (said) input.onEvent(said);
    };
    for (const track of media.getTracks()) {
      track.addEventListener("ended", () => end("microphone-failed"), { once: true });
    }
    audio.createMediaStreamSource(media).connect(mic);
    // The capture processor emits silence; the connection keeps it running.
    mic.connect(audio.destination);
    speaker.connect(audio.destination);
    return { mic, play, rate: audio.sampleRate };
  };

  const call = ({ mic, play, rate }: ReturnType<typeof wire>) => {
    const ws = new WebSocket(input.url);
    socket = ws;
    ws.binaryType = "arraybuffer";
    let opened = false;
    let replyRate = 0;
    mic.port.onmessage = ({ data }: MessageEvent<unknown>) => {
      if (data instanceof Float32Array && ws.readyState === WebSocket.OPEN) {
        ws.send(floatToPcm16(data));
      }
    };
    ws.onopen = () => {
      opened = true;
      ws.send(`auth ${input.secret}`);
      ws.send(
        JSON.stringify({
          type: "hello",
          mode: "vad",
          thread: input.threadId,
          environment: input.environmentId,
        }),
      );
    };
    ws.onmessage = ({ data }: MessageEvent<unknown>) => {
      if (data instanceof ArrayBuffer) {
        if (replyRate > 0) {
          const pcm = pcm16ToDeviceFloat(data, replyRate, rate);
          fed += pcm.length;
          play({ type: "feed", pcm });
        }
        return;
      }
      const message = Option.getOrNull(decodeVoiceServerMessage(data));
      if (message === null) return;
      if (message.type === "say") {
        sayings.set(nextMark, message);
        play({ type: "mark", id: nextMark });
        nextMark += 1;
        return;
      }
      if (message.type === "ready") {
        // oxlint-disable-next-line unicorn/require-post-message-target-origin -- AudioWorklet MessagePorts do not accept a target origin
        mic.port.postMessage("start");
      } else if (message.type === "reply_start") {
        if (message.rate !== replyRate) {
          play({ type: "flush" });
          sayings.clear();
          replyRate = message.rate;
        }
        playing =
          message.turn === undefined || message.reply === undefined
            ? null
            : { turn: message.turn, reply: message.reply, from: fed, rate: message.rate };
      } else if (message.type === "reply_end") {
        play({ type: "feed", pcm: new Float32Array(0), end: true });
      } else if (message.type === "duck" || message.type === "unduck") {
        play({ type: message.type });
      } else if (message.type === "barge_in") {
        if (playing && playing.turn === message.turn) {
          stopped = playing;
          play({ type: "position" });
        }
        play({ type: "flush" });
        sayings.clear();
      }
      input.onEvent(message);
    };
    ws.onclose = (event) => end(closeError(event.code, opened));
  };

  const start = async (audio: AudioContext) => {
    const resumed = audio.resume().then(
      () => true,
      () => false,
    );
    let media: MediaStream;
    try {
      media = await requestMicrophone(await chooseRecordingMicrophone());
    } catch (error) {
      end(
        error instanceof DOMException && error.name === "NotAllowedError"
          ? "microphone-denied"
          : "microphone-failed",
      );
      return;
    }
    stream = media;
    if (ended) {
      media.getTracks().forEach((track) => track.stop());
      return;
    }
    let graph: ReturnType<typeof wire>;
    try {
      if (!(await resumed)) throw new Error("audio did not start");
      await audio.audioWorklet.addModule(captureWorkletUrl);
      await audio.audioWorklet.addModule(playbackWorkletUrl);
      if (ended) return;
      graph = wire(audio, media);
    } catch {
      end("microphone-failed");
      return;
    }
    try {
      call(graph);
    } catch {
      end("unreachable");
    }
  };

  try {
    // Made and resumed inside the click, since browsers start audio only from a user gesture.
    context = new AudioContext();
    void start(context);
  } catch {
    queueMicrotask(() => end("microphone-failed"));
  }
  return { hangUp: () => end(null) };
}
