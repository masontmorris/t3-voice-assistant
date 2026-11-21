import type { EnvironmentId, ThreadId } from "@t3tools/contracts";
import { create } from "zustand";
import { readVoiceServerReach } from "./useVoiceServerReach";
import { openVoiceCall, type VoiceCallSession } from "./voiceCallSession";
import { applyVoiceCallEvent, failVoiceCall, newVoiceCall, type VoiceCall } from "./voiceCallState";

/** The app's one live call. It outlives navigation, so a call stays open on its thread until ended. */
export const useVoiceCallStore = create<{ readonly call: VoiceCall | null }>(() => ({
  call: null,
}));

let session: VoiceCallSession | null = null;

export const voiceCallKey = (environmentId: EnvironmentId, threadId: ThreadId) =>
  `${environmentId}:${threadId}`;

/** The thread a live call is on, as `voiceCallKey`, so a subscriber skips caption updates. */
export const selectLiveVoiceCallKey = ({ call }: { readonly call: VoiceCall | null }) =>
  call !== null && call.error === null ? voiceCallKey(call.environmentId, call.threadId) : null;

function startVoiceCall(input: {
  readonly environmentId: EnvironmentId;
  readonly threadId: ThreadId;
  readonly url: string;
  readonly secret: string;
}): void {
  if (session) return;
  useVoiceCallStore.setState({ call: newVoiceCall(input.environmentId, input.threadId) });
  const opened: VoiceCallSession = openVoiceCall({
    url: input.url,
    secret: input.secret,
    threadId: input.threadId,
    onEvent: (event) => {
      if (session !== opened) return;
      useVoiceCallStore.setState(({ call }) => ({
        call: call && applyVoiceCallEvent(call, event),
      }));
    },
    onEnded: (error) => {
      if (session !== opened) return;
      session = null;
      useVoiceCallStore.setState(({ call }) => ({
        call: error === null || call === null ? null : failVoiceCall(call, error),
      }));
    },
  });
  session = opened;
}

export function endVoiceCall(): void {
  session?.hangUp();
}

export function dismissVoiceCall(): void {
  if (useVoiceCallStore.getState().call?.error) useVoiceCallStore.setState({ call: null });
}

/**
 * Ends the call open on this thread, or starts one when none is open and the voice server can be
 * reached directly. False when neither applies, so a shortcut falls through.
 */
export function toggleVoiceCall(environmentId: EnvironmentId, threadId: ThreadId): boolean {
  const call = useVoiceCallStore.getState().call;
  if (call !== null && call.error === null) {
    if (call.environmentId !== environmentId || call.threadId !== threadId) return false;
    endVoiceCall();
    return true;
  }
  const { reach, secret } = readVoiceServerReach();
  if (reach.kind !== "ready") return false;
  startVoiceCall({ environmentId, threadId, url: reach.url, secret });
  return true;
}
