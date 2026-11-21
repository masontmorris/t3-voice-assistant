import * as Option from "effect/Option";
import { chooseRecordingMicrophone } from "../speech/browserMicrophones";
import captureWorkletUrl from "../speech/pcmWorklet.ts?worker&url";
import { requestMicrophone } from "../speech/requestMicrophone";
import { floatToPcm16, pcm16ToDeviceFloat } from "./voiceCallAudio";
import playbackWorkletUrl from "./voiceCallPlaybackWorklet.ts?worker&url";
import type { PlaybackMessage } from "./voiceCallPlaybackWorklet";
import { closeError, decodeVoiceServerMessage, type VoiceCallError } from "./voiceCallProtocol";
import type { VoiceCallEvent } from "./voiceCallState";

export type VoiceCallSession = { readonly hangUp: () => void };

/**
 * One call to the voice server: the microphone goes up as 16 kHz PCM16, the reply comes down
 * and plays. The secret is the socket's first frame, never part of the URL. `onEnded` runs once,
 * with no error when the caller hung up.
 */
export function openVoiceCall(input: {
  readonly url: string;
  readonly secret: string;
  readonly threadId: string;
  readonly onEvent: (event: VoiceCallEvent) => void;
  readonly onEnded: (error: VoiceCallError | null) => void;
}): VoiceCallSession {
  // Created and resumed inside the click, since browsers start audio only from a user gesture.
  const context = new AudioContext();
  const resumed = context.resume();
  let socket: WebSocket | undefined;
  let stream: MediaStream | undefined;
  let capture: AudioWorkletNode | undefined;
  let playback: AudioWorkletNode | undefined;
  let ended = false;

  const play = (message: PlaybackMessage) => {
    // oxlint-disable-next-line unicorn/require-post-message-target-origin -- AudioWorklet MessagePorts do not accept a target origin
    playback?.port.postMessage(message);
  };

  const end = (error: VoiceCallError | null) => {
    if (ended) return;
    ended = true;
    if (socket) {
      socket.onclose = socket.onmessage = socket.onopen = null;
      socket.close(1000);
    }
    for (const node of [capture, playback]) {
      node?.disconnect();
      node?.port.close();
    }
    stream?.getTracks().forEach((track) => track.stop());
    void context.close();
    input.onEnded(error);
  };

  const connect = (media: MediaStream) => {
    const rate = context.sampleRate;
    const mic = new AudioWorkletNode(context, "t3-pcm-capture", {
      channelCount: 1,
      processorOptions: { call: true },
    });
    const speaker = new AudioWorkletNode(context, "t3-voice-call-playback");
    capture = mic;
    playback = speaker;
    play({ type: "config", deviceRate: rate });
    speaker.port.onmessage = ({ data }) => {
      if (data === "drained") input.onEvent({ type: "drained" });
    };
    for (const track of media.getTracks()) {
      track.addEventListener("ended", () => end("microphone-failed"), { once: true });
    }
    context.createMediaStreamSource(media).connect(mic);
    // The capture processor emits silence; the connection keeps it running.
    mic.connect(context.destination);
    speaker.connect(context.destination);

    const ws = new WebSocket(input.url);
    socket = ws;
    ws.binaryType = "arraybuffer";
    let opened = false;
    let replyRate = 0;
    mic.port.onmessage = ({ data }: MessageEvent<unknown>) => {
      if (data instanceof Float32Array && ws.readyState === WebSocket.OPEN)
        ws.send(floatToPcm16(data));
    };
    ws.onopen = () => {
      opened = true;
      ws.send(`auth ${input.secret}`);
      ws.send(JSON.stringify({ type: "hello", mode: "vad", thread: input.threadId }));
    };
    ws.onmessage = ({ data }: MessageEvent<unknown>) => {
      if (data instanceof ArrayBuffer) {
        if (replyRate > 0) play({ type: "feed", pcm: pcm16ToDeviceFloat(data, replyRate, rate) });
        return;
      }
      const message = Option.getOrNull(decodeVoiceServerMessage(data));
      if (message === null) return;
      if (message.type === "ready") {
        // oxlint-disable-next-line unicorn/require-post-message-target-origin -- AudioWorklet MessagePorts do not accept a target origin
        mic.port.postMessage("start");
      } else if (message.type === "reply_start" && message.rate !== replyRate) {
        play({ type: "flush" });
        replyRate = message.rate;
      } else if (message.type === "reply_end") {
        play({ type: "feed", pcm: new Float32Array(0), end: true });
      } else if (message.type === "duck" || message.type === "unduck") {
        play({ type: message.type });
      } else if (message.type === "barge_in") {
        play({ type: "flush" });
      }
      input.onEvent(message);
    };
    ws.onclose = (event) => end(closeError(event.code, opened));
  };

  void (async () => {
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
    try {
      await resumed;
      await context.audioWorklet.addModule(captureWorkletUrl);
      await context.audioWorklet.addModule(playbackWorkletUrl);
    } catch {
      end("microphone-failed");
      return;
    }
    if (!ended) connect(media);
  })();

  return { hangUp: () => end(null) };
}
