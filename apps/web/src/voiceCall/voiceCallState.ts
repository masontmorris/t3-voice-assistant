import type { EnvironmentId, ThreadId } from "@t3tools/contracts";
import type { VoiceCallError, VoiceServerMessage } from "./voiceCallProtocol";

export type VoiceCallPhase = "connecting" | "listening" | "thinking" | "speaking";

export type VoiceCaption = {
  readonly key: string;
  readonly turn: number;
  readonly speaker: "caller" | "voice";
  readonly text: string;
  readonly relay: boolean;
};

export type VoiceCall = {
  readonly environmentId: EnvironmentId;
  readonly threadId: ThreadId;
  readonly phase: VoiceCallPhase;
  readonly captions: ReadonlyArray<VoiceCaption>;
  /** Caption phrases the voice has spoken; each one pulses the call's shape once. */
  readonly phrases: number;
  /** Set once the call has ended on its own; a call the caller hangs up is simply gone. */
  readonly error: VoiceCallError | null;
};

/** The call reports its playback queue ran dry, which is when speaking ends for the caller. */
export type VoiceCallEvent = VoiceServerMessage | { readonly type: "drained" };

const KEPT_CAPTIONS = 40;

export function newVoiceCall(environmentId: EnvironmentId, threadId: ThreadId): VoiceCall {
  return { environmentId, threadId, phase: "connecting", captions: [], phrases: 0, error: null };
}

function withCaption(call: VoiceCall, caption: VoiceCaption): VoiceCall {
  const captions = [...call.captions, caption];
  return { ...call, captions: captions.slice(-KEPT_CAPTIONS) };
}

export function applyVoiceCallEvent(call: VoiceCall, event: VoiceCallEvent): VoiceCall {
  if (call.error !== null) return call;
  switch (event.type) {
    case "ready":
    case "turn_start":
    case "barge_in":
      return { ...call, phase: "listening" };
    case "turn_end":
      return { ...call, phase: "thinking" };
    case "reply_start":
      return { ...call, phase: "speaking" };
    case "drained":
      return call.phase === "speaking" ? { ...call, phase: "listening" } : call;
    case "turn_done":
      return call.phase === "speaking" ? call : { ...call, phase: "listening" };
    case "heard":
      return withCaption(call, {
        key: `caller-${event.turn}`,
        turn: event.turn,
        speaker: "caller",
        text: event.text.trim(),
        relay: false,
      });
    case "say": {
      const relay = event.relay === true;
      const last = call.captions.at(-1);
      const phrased = { ...call, phrases: call.phrases + 1 };
      if (last?.speaker === "voice" && last.turn === event.turn && last.relay === relay) {
        return {
          ...phrased,
          captions: [...call.captions.slice(0, -1), { ...last, text: last.text + event.text }],
        };
      }
      return withCaption(phrased, {
        key: `voice-${event.turn}-${call.phrases}`,
        turn: event.turn,
        speaker: "voice",
        text: event.text.trimStart(),
        relay,
      });
    }
    case "reply_end":
    case "duck":
    case "unduck":
      return call;
  }
}

export function failVoiceCall(call: VoiceCall, error: VoiceCallError): VoiceCall {
  return { ...call, error };
}
