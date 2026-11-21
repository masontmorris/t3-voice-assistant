import * as Schema from "effect/Schema";

// The voice server's control frames (voice/live/server.py). Fields the call does not use are ignored.
const Message = <Type extends string, Fields extends Schema.Struct.Fields>(
  type: Type,
  fields: Fields,
) => Schema.Struct({ type: Schema.Literal(type), ...fields });

export const VoiceServerMessage = Schema.Union([
  Message("ready", {}),
  Message("turn_start", {}),
  Message("turn_end", {}),
  Message("heard", { turn: Schema.Number, text: Schema.String }),
  Message("reply_start", { rate: Schema.Number }),
  Message("say", {
    turn: Schema.Number,
    text: Schema.String,
    relay: Schema.optional(Schema.Boolean),
  }),
  Message("reply_end", {}),
  Message("duck", {}),
  Message("unduck", {}),
  Message("barge_in", {}),
  Message("turn_done", {}),
]);
export type VoiceServerMessage = typeof VoiceServerMessage.Type;

export const decodeVoiceServerMessage = Schema.decodeUnknownOption(
  Schema.fromJsonString(VoiceServerMessage),
);

export type VoiceCallError =
  | "microphone-denied"
  | "microphone-failed"
  | "unreachable"
  | "wrong-secret"
  | "busy"
  | "thread-unavailable"
  | "other-environment"
  | "archived"
  | "no-t3"
  | "ended-by-server";

const CLOSE_ERRORS: Partial<Record<number, VoiceCallError>> = {
  1008: "wrong-secret",
  1013: "busy",
  4404: "thread-unavailable",
  4409: "other-environment",
  4410: "archived",
  4501: "no-t3",
};

/** Why the server closed a call. A socket that never opened did not reach the server. */
export function closeError(code: number, opened: boolean): VoiceCallError {
  if (!opened) return "unreachable";
  return CLOSE_ERRORS[code] ?? "ended-by-server";
}
