import { cn } from "~/lib/utils";
import type { VoiceCallPhase } from "../../voiceCall/voiceCallState";

export type VoiceCallForm = VoiceCallPhase | "waiting";

const FORMS: Record<VoiceCallForm, string> = {
  connecting: "size-2 rounded-full border-muted-foreground bg-muted-foreground",
  listening: "size-4 rounded-full border-current bg-transparent",
  thinking: "size-3.5 rounded-[3px] border-current bg-current",
  speaking: "h-3.5 w-6 rounded-full border-current bg-current",
  waiting: "size-4 rounded-full border-warning bg-transparent",
};

/**
 * One shape whose form says where the call is. It morphs between forms and is otherwise still:
 * a speaking pill pulses once per caption phrase, and waiting on the caller ripples once.
 */
export function VoiceCallShape(props: {
  readonly form: VoiceCallForm;
  readonly relay: boolean;
  readonly phrases: number;
}) {
  const { form } = props;
  return (
    <span
      aria-hidden
      className={cn(
        "relative flex size-6 items-center justify-center",
        props.relay && form === "speaking" ? "text-info" : "text-foreground",
      )}
    >
      {form === "waiting" ? (
        <span className="absolute size-4 animate-voice-call-ripple rounded-full border-2 border-warning motion-reduce:hidden" />
      ) : null}
      <span
        className={cn(
          "flex items-center justify-center border-2 transition-[width,height,border-radius,background-color,border-color] duration-250 ease-out motion-reduce:transition-none",
          FORMS[form],
          form === "speaking" &&
            props.phrases > 0 &&
            (props.phrases % 2 === 0 ? "animate-voice-call-pulse" : "animate-voice-call-pulse-alt"),
          "motion-reduce:animate-none",
        )}
      >
        {form === "waiting" ? <span className="size-1.5 rounded-full bg-warning" /> : null}
      </span>
    </span>
  );
}
