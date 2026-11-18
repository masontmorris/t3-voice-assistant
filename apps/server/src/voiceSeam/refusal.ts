import { CommandId, ThreadId } from "@t3tools/contracts";
import * as Schema from "effect/Schema";

import { RefusalCode } from "./protocol.ts";

/** The most of T3's recorded rejection a refusal carries. */
const REASON_LIMIT = 200;

export class VoiceSeamRefusal extends Schema.TaggedError<VoiceSeamRefusal>()("VoiceSeamRefusal", {
  code: RefusalCode,
  /** Short text the voice may speak. Never built from a cause. */
  message: Schema.String,
  /** For "refused" only: T3's recorded rejection, the receipt's `error`. Absent when it recorded none. */
  reason: Schema.optional(Schema.String),
  /** For "refused" only: the command whose receipt was read, so the claim traces to it. */
  commandId: Schema.optional(CommandId),
  /** For "refused" after an accepted launch: the thread that exists although its first message was refused. */
  threadId: Schema.optional(ThreadId),
  cause: Schema.optional(Schema.Defect()),
}) {
  /** Missing, deleted, unlisted, delegated, or outside the allowed projects: the voice cannot probe which. */
  static readonly notFound = (message = "I can't see that thread.") =>
    new VoiceSeamRefusal({ code: "not_found", message });

  static readonly projectNotAllowed = () =>
    new VoiceSeamRefusal({
      code: "project_not_allowed",
      message: "I'm not allowed to look at that project.",
    });

  static readonly invalidRequest = (message = "T3 couldn't read that request.") =>
    new VoiceSeamRefusal({ code: "invalid_request", message });

  static readonly unavailable = (cause: unknown) =>
    new VoiceSeamRefusal({ code: "unavailable", message: "I can't reach T3 right now.", cause });

  static readonly operationNotAllowed = () =>
    new VoiceSeamRefusal({ code: "operation_not_allowed", message: "I'm not allowed to do that." });

  static readonly tooManyWatches = () =>
    new VoiceSeamRefusal({
      code: "too_many_watches",
      message: "I'm following too many threads already.",
    });

  /** An operation the seam lists but does not perform yet. */
  static readonly notBuilt = () =>
    new VoiceSeamRefusal({ code: "unavailable", message: "I can't do that yet." });

  /** T3 recorded a rejected receipt for the command. `threadId` names a thread that exists anyway. */
  static readonly refused = (input: {
    readonly reason: string | null;
    readonly commandId: CommandId;
    readonly threadId?: ThreadId;
  }) =>
    new VoiceSeamRefusal({
      code: "refused",
      message: "T3 refused it.",
      ...(input.reason === null ? {} : { reason: input.reason.slice(0, REASON_LIMIT) }),
      commandId: input.commandId,
      ...(input.threadId === undefined ? {} : { threadId: input.threadId }),
    });

  /** The command was accepted, and its thread has since been deleted. */
  static readonly gone = () =>
    new VoiceSeamRefusal({ code: "gone", message: "That thread was deleted." });

  static readonly aboveCeiling = (cause: unknown) =>
    new VoiceSeamRefusal({
      code: "above_ceiling",
      message: "T3's policy doesn't allow that mode.",
      cause,
    });

  static readonly noDefaultModel = () =>
    new VoiceSeamRefusal({
      code: "no_default_model",
      message: "That project has no default model I can start.",
    });
}
