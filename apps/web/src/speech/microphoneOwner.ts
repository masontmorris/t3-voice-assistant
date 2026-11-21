import { create } from "zustand";

export type MicrophoneUser = "dictation" | "call";

export const useMicrophoneUser = create<{ readonly user: MicrophoneUser | null }>(() => ({
  user: null,
}));

/**
 * One capture at a time, so dictation and a live call never both open the microphone, whichever
 * entry point starts them. Returns a release, or null while the other user holds it.
 */
export function claimMicrophone(user: MicrophoneUser): (() => void) | null {
  if (useMicrophoneUser.getState().user !== null) return null;
  useMicrophoneUser.setState({ user });
  let held = true;
  return () => {
    if (!held) return;
    held = false;
    useMicrophoneUser.setState({ user: null });
  };
}
