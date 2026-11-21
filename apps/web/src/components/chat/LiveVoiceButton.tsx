import type { EnvironmentId, ThreadId } from "@t3tools/contracts";
import { useGT } from "gt-react";
import { AudioLinesIcon, PhoneOffIcon } from "lucide-react";

import { useVoiceServerReach } from "../../voiceCall/useVoiceServerReach";
import {
  selectLiveVoiceCallKey,
  toggleVoiceCall,
  useVoiceCallStore,
  voiceCallKey,
} from "../../voiceCall/voiceCallStore";
import { Button } from "../ui/button";
import { ShortcutTooltip } from "./ComposerSpeechButton";

/** Starts a live voice call about this thread, or ends the call already open on it. */
export function LiveVoiceButton(props: {
  readonly environmentId: EnvironmentId;
  readonly threadId: ThreadId;
  readonly shortcutLabel?: string | null;
  readonly disabled?: boolean;
}) {
  const gt = useGT();
  const reach = useVoiceServerReach();
  const liveThreadKey = useVoiceCallStore(selectLiveVoiceCallKey);
  if (reach.kind === "unset") return null;
  const live = liveThreadKey !== null;
  const here = liveThreadKey === voiceCallKey(props.environmentId, props.threadId);
  const blocked = {
    invalid: gt("The voice server address in Settings is not valid."),
    "not-direct": gt("Live voice needs a voice server on this device or your tailnet."),
    insecure: gt(
      "This page needs a wss:// voice server address, such as one from Tailscale Serve.",
    ),
    "no-microphone": gt("This browser cannot use a microphone here."),
    ready: null,
  }[reach.kind];
  const reason = here ? null : live ? gt("A voice call is open on another thread.") : blocked;
  const label = reason ?? (here ? gt("End voice call") : gt("Start voice call"));

  return (
    <ShortcutTooltip label={label} shortcut={reason ? null : props.shortcutLabel}>
      <Button
        type="button"
        size="icon-sm"
        variant="ghost"
        aria-label={label}
        disabled={props.disabled === true || reason !== null}
        onPointerDown={(event) => event.preventDefault()}
        onClick={() => void toggleVoiceCall(props.environmentId, props.threadId)}
        className="shrink-0"
      >
        {here ? <PhoneOffIcon /> : <AudioLinesIcon />}
      </Button>
    </ShortcutTooltip>
  );
}
