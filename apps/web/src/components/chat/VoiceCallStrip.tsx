import type { EnvironmentId, ThreadId } from "@t3tools/contracts";
import { useNavigate } from "@tanstack/react-router";
import { useGT } from "gt-react";

import { cn } from "~/lib/utils";
import { buildThreadRouteParams } from "../../threadRoutes";
import type { VoiceCallError } from "../../voiceCall/voiceCallProtocol";
import { dismissVoiceCall, endVoiceCall, useVoiceCallStore } from "../../voiceCall/voiceCallStore";
import { Button } from "../ui/button";
import { ComposerBanner } from "./ComposerBanner";
import { VoiceCallShape, type VoiceCallForm } from "./VoiceCallShape";

const SHOWN_CAPTIONS = 2;

/**
 * The live call's state and captions above the composer. It shows on every thread while the call
 * is open, so a call kept across navigation can always be seen and ended.
 */
export function VoiceCallStrip(props: {
  readonly environmentId: EnvironmentId;
  readonly threadId: ThreadId | null;
  /** Whether this thread is waiting on the caller, which only this thread's view knows. */
  readonly waitingOnYou: boolean;
}) {
  const gt = useGT();
  const navigate = useNavigate();
  const call = useVoiceCallStore((state) => state.call);
  if (call === null) return null;
  const here = call.threadId === props.threadId && call.environmentId === props.environmentId;
  const form: VoiceCallForm =
    here && props.waitingOnYou && call.error === null ? "waiting" : call.phase;
  const captions = call.captions.slice(-SHOWN_CAPTIONS);
  const relay = captions.at(-1)?.relay === true && call.phase === "speaking";
  const phaseLabel = {
    connecting: gt("Connecting"),
    listening: gt("Listening"),
    thinking: gt("Thinking"),
    speaking: gt("Speaking"),
    waiting: gt("This thread is waiting on you"),
  }[form];
  const errors: Record<VoiceCallError, string> = {
    "microphone-denied": gt(
      "Microphone access is blocked. Allow it for this app, then call again.",
    ),
    "microphone-failed": gt("The microphone could not start."),
    unreachable: gt(
      "Can't reach the voice server. Check that it is running and its address in Settings.",
    ),
    "wrong-secret": gt("The voice server refused the secret. Paste it again in Settings."),
    busy: gt("The voice server is already on another call."),
    "thread-unavailable": gt("The voice server can't see this thread."),
    "other-environment": gt(
      "The voice server works with a different T3 environment than this thread's.",
    ),
    "no-t3": gt("The voice server is running without T3."),
    "ended-by-server": gt("The voice server ended the call."),
  };

  return (
    <ComposerBanner.Attachment>
      <ComposerBanner.Root
        variant={call.error ? "error" : form === "waiting" ? "warning" : "default"}
      >
        <ComposerBanner.Row>
          <ComposerBanner.Icon>
            <VoiceCallShape form={form} relay={relay} phrases={call.phrases} />
          </ComposerBanner.Icon>
          <ComposerBanner.Content>
            <span className="sr-only">{phaseLabel}</span>
            {call.error ? (
              <span className="min-w-0 truncate">{errors[call.error]}</span>
            ) : (
              <span className="flex min-w-0 flex-col">
                {captions.length === 0 ? (
                  <span className="text-muted-foreground">{phaseLabel}</span>
                ) : (
                  captions.map((caption) => (
                    <span
                      key={caption.key}
                      className={cn(
                        "min-w-0 truncate",
                        caption.speaker === "caller" && "text-muted-foreground",
                        caption.relay && "text-info",
                      )}
                    >
                      {caption.text}
                    </span>
                  ))
                )}
              </span>
            )}
          </ComposerBanner.Content>
          <ComposerBanner.Actions>
            {here ? null : (
              <Button
                size="xs"
                variant="ghost"
                onClick={() =>
                  void navigate({
                    to: "/$environmentId/$threadId",
                    params: buildThreadRouteParams({
                      environmentId: call.environmentId,
                      threadId: call.threadId,
                    }),
                  })
                }
              >
                {gt("Open call thread")}
              </Button>
            )}
            {call.error ? (
              <Button size="xs" variant="ghost" onClick={dismissVoiceCall}>
                {gt("Dismiss")}
              </Button>
            ) : (
              <Button size="xs" variant="outline" onClick={endVoiceCall}>
                {gt("End call")}
              </Button>
            )}
          </ComposerBanner.Actions>
        </ComposerBanner.Row>
      </ComposerBanner.Root>
    </ComposerBanner.Attachment>
  );
}
