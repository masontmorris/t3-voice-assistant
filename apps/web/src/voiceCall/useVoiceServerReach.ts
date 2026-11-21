import { getClientSettings, useClientSettings } from "../hooks/useSettings";
import { voiceServerReach } from "./voiceServerAddress";

function reachOf(address: string, secret: string) {
  return voiceServerReach({
    address,
    secret,
    pageProtocol: window.location.protocol,
    canCapture:
      typeof navigator.mediaDevices?.getUserMedia === "function" &&
      typeof AudioWorkletNode !== "undefined",
  });
}

export function useVoiceServerReach() {
  const address = useClientSettings((settings) => settings.liveVoiceServer);
  const secret = useClientSettings((settings) => settings.liveVoiceSecret);
  return reachOf(address, secret);
}

export function readVoiceServerReach() {
  const { liveVoiceServer, liveVoiceSecret } = getClientSettings();
  return { secret: liveVoiceSecret, reach: reachOf(liveVoiceServer, liveVoiceSecret) };
}
