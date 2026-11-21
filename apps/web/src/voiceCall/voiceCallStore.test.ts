import { EnvironmentId, ThreadId } from "@t3tools/contracts";
import { expect, it, vi } from "vite-plus/test";
import { claimMicrophone } from "../speech/microphoneOwner";
import type { VoiceCallError } from "./voiceCallProtocol";
import { openVoiceCall } from "./voiceCallSession";
import { toggleVoiceCall, useVoiceCallStore } from "./voiceCallStore";

vi.mock("./voiceCallSession", () => ({ openVoiceCall: vi.fn() }));
vi.mock("./useVoiceServerReach", () => ({
  readVoiceServerReach: () => ({
    secret: "s",
    reach: { kind: "ready", url: "ws://127.0.0.1:8770/" },
  }),
}));

const environmentId = EnvironmentId.make("env");
const threadId = ThreadId.make("t1");

it("lets dictation and a call never hold the microphone together, from any entry point", () => {
  let endCall!: (error: VoiceCallError | null) => void;
  vi.mocked(openVoiceCall).mockImplementation((input) => {
    endCall = input.onEnded;
    return { hangUp: () => input.onEnded(null) };
  });

  const releaseDictation = claimMicrophone("dictation");
  expect(toggleVoiceCall(environmentId, threadId)).toBe(false);
  expect(openVoiceCall).not.toHaveBeenCalled();
  expect(useVoiceCallStore.getState().call).toBeNull();
  releaseDictation?.();

  expect(toggleVoiceCall(environmentId, threadId)).toBe(true);
  expect(openVoiceCall).toHaveBeenCalledTimes(1);
  expect(claimMicrophone("dictation")).toBeNull();

  endCall("ended-by-server");
  expect(useVoiceCallStore.getState().call?.error).toBe("ended-by-server");
  const again = claimMicrophone("dictation");
  expect(again).not.toBeNull();
  again?.();
});
