import {
  type CommandId,
  MessageId,
  type OrchestrationV2ThreadShell,
  type OrchestrationV2TurnItem,
  type ThreadId,
} from "@t3tools/contracts";
import type * as Digest from "effect/crypto/Digest";
import * as Effect from "effect/Effect";
import * as Option from "effect/Option";

import type * as CommandReceiptStore from "../orchestration-v2/CommandReceiptStore.ts";
import { DispatchModeLimit } from "../orchestration-v2/DispatchModeLimit.ts";
import type * as ThreadManagementService from "../orchestration-v2/ThreadManagementService.ts";
import { dispatchModeLimit, isAboveCeiling, type VoiceSeamConfig } from "./ceiling.ts";
import { voiceCommandId } from "./launch.ts";
import type { RequestInput, VoiceInterruptResult, VoiceSendResult } from "./protocol.ts";
import { VoiceSeamRefusal } from "./refusal.ts";

type ReceiptGate =
  /** No receipt: this request has not acted. */
  | { readonly kind: "none" }
  /** The request id already named a command on another thread. */
  | { readonly kind: "elsewhere" }
  /** T3 refused the command. */
  | { readonly kind: "rejected"; readonly reason: string | null }
  /** T3 accepted the command, so the request has acted and never acts again. */
  | { readonly kind: "accepted" };

const receiptGate = (
  receipt: Option.Option<CommandReceiptStore.CommandReceiptV2>,
  threadId: ThreadId,
): ReceiptGate => {
  if (Option.isNone(receipt)) return { kind: "none" };
  if (receipt.value.threadId !== threadId) return { kind: "elsewhere" };
  if (receipt.value.status === "rejected") {
    return { kind: "rejected", reason: receipt.value.error };
  }
  return { kind: "accepted" };
};

const isUserMessage = (
  item: OrchestrationV2TurnItem,
): item is Extract<OrchestrationV2TurnItem, { readonly type: "user_message" }> =>
  item.type === "user_message";

export interface SendSources {
  readonly digest: Digest.Digest["Service"];
  readonly threads: ThreadManagementService.ThreadManagementService["Service"];
  readonly receipts: CommandReceiptStore.CommandReceiptStoreV2["Service"];
}

/**
 * Writes to a thread the service already found visible. Each request id fixes its command, and a
 * request with a receipt never reaches ThreadManagementService again.
 */
export const make = (sources: SendSources) => {
  const { threads, receipts } = sources;
  const unavailable = Effect.mapError(VoiceSeamRefusal.unavailable);

  const readGate = Effect.fn("VoiceSeam.readWriteGate")(function* (
    commandId: CommandId,
    threadId: ThreadId,
  ) {
    return receiptGate(yield* receipts.getByCommandId(commandId).pipe(unavailable), threadId);
  });

  const refuseGate = (
    gate: Extract<ReceiptGate, { readonly kind: "elsewhere" | "rejected" }>,
    commandId: CommandId,
  ) =>
    gate.kind === "elsewhere"
      ? VoiceSeamRefusal.invalidRequest("That request already went to another thread.")
      : VoiceSeamRefusal.refused({ reason: gate.reason, commandId });

  /** An accepted send, answered from the message it wrote. It never sends again. */
  const replaySend = Effect.fn("VoiceSeam.replaySend")(function* (
    threadId: ThreadId,
    messageId: MessageId,
  ) {
    // Read first, so anything the thread commits after it is in a watch from it.
    const sequence = yield* threads.getThreadEventSequence(threadId).pipe(unavailable);
    const shell = yield* threads.getThreadShell(threadId).pipe(unavailable);
    if (shell === null || shell.deletedAt !== null) return yield* VoiceSeamRefusal.gone();
    const records = yield* threads
      .getThreadRecords(threadId, ["messages", "turnItems"], {
        messageIds: [messageId],
        turnItemTypes: ["user_message"],
      })
      .pipe(unavailable);
    const runId = records.messages.find((message) => message.id === messageId)?.runId;
    if (runId == null) {
      return yield* VoiceSeamRefusal.unavailable(
        `Send ${messageId} was accepted with no durable run.`,
      );
    }
    // Mason can promote a queued message to a steer in the app; the voice still queued it.
    const started =
      records.turnItems.filter(isUserMessage).find((item) => item.messageId === messageId)
        ?.inputIntent === "turn_start";
    return {
      threadId,
      messageId,
      runId,
      delivery: started ? "started" : "queued",
      sequence,
      replayed: true,
    } satisfies VoiceSendResult;
  });

  const answerSend = (
    gate: Exclude<ReceiptGate, { readonly kind: "none" }>,
    ids: {
      readonly commandId: CommandId;
      readonly threadId: ThreadId;
      readonly messageId: MessageId;
    },
  ): Effect.Effect<VoiceSendResult, VoiceSeamRefusal> =>
    gate.kind === "accepted"
      ? replaySend(ids.threadId, ids.messageId)
      : Effect.fail(refuseGate(gate, ids.commandId));

  const send = Effect.fn("VoiceSeam.sendToThread")(function* (
    config: VoiceSeamConfig,
    shell: OrchestrationV2ThreadShell,
    input: RequestInput<"send">,
  ) {
    const commandId = yield* voiceCommandId(sources.digest, "send", input.requestId);
    const ids = { commandId, threadId: input.threadId, messageId: MessageId.make(commandId) };
    const gate = yield* readGate(commandId, input.threadId);
    if (gate.kind !== "none") return yield* answerSend(gate, ids);
    // sendToThread restores a missing worktree before the orchestrator checks the mode.
    if (isAboveCeiling(config, shell)) return yield* VoiceSeamRefusal.threadAboveCeiling();
    const sent = yield* threads
      .sendToThread({
        projectId: shell.projectId,
        commandId,
        threadId: input.threadId,
        messageId: ids.messageId,
        text: input.text,
        attachments: [],
        mode: "queue",
        createdBy: "user",
        creationSource: "server",
      })
      .pipe(
        // The orchestrator rechecks under the command lock: Mason may raise the mode after the preflight.
        Effect.provideService(DispatchModeLimit, dispatchModeLimit(config)),
        Effect.result,
      );
    if (sent._tag === "Failure") {
      const error = sent.failure;
      switch (error._tag) {
        // Neither refusal leaves a receipt, so a retry checks the mode again.
        case "OrchestratorThreadAboveModeLimitError":
          return yield* VoiceSeamRefusal.threadAboveCeiling(error);
        case "OrchestratorRuntimeModeAboveCeilingError":
          return yield* VoiceSeamRefusal.aboveCeiling(error);
        case "ThreadManagementThreadNotFoundError":
          return yield* VoiceSeamRefusal.notFound();
        case "ThreadManagementThreadArchivedError":
          return yield* VoiceSeamRefusal.invalidRequest("That thread is archived.");
        case "ThreadManagementProviderInstanceDisabledError":
          return yield* new VoiceSeamRefusal({
            code: "no_default_model",
            message: "That thread's model is turned off.",
          });
      }
      // A send that failed after T3 recorded its command answers as its retry would.
      const after = yield* readGate(commandId, input.threadId);
      if (after.kind === "none") return yield* VoiceSeamRefusal.unavailable(error);
      return yield* answerSend(after, ids);
    }
    const { delivery, dispatch, run } = sent.success;
    if (delivery !== "started" && delivery !== "queued") {
      return yield* Effect.die(`A queued voice send was ${delivery}.`);
    }
    const replayed = dispatch.replayed === true;
    return {
      threadId: input.threadId,
      messageId: ids.messageId,
      runId: run.id,
      delivery,
      sequence: replayed
        ? yield* threads.getThreadEventSequence(input.threadId).pipe(unavailable)
        : dispatch.sequence,
      replayed,
    } satisfies VoiceSendResult;
  });

  /** A request with a receipt already interrupted its run, so it has nothing left to stop. */
  const answerInterrupt = (
    gate: Exclude<ReceiptGate, { readonly kind: "none" }>,
    commandId: CommandId,
  ): Effect.Effect<VoiceInterruptResult, VoiceSeamRefusal> =>
    gate.kind === "accepted"
      ? Effect.succeed({ kind: "nothing_running" })
      : Effect.fail(refuseGate(gate, commandId));

  const interrupt = Effect.fn("VoiceSeam.interruptThread")(function* (
    shell: OrchestrationV2ThreadShell,
    input: RequestInput<"interrupt">,
  ) {
    const commandId = yield* voiceCommandId(sources.digest, "interrupt", input.requestId);
    // A retry must not reach interruptThread: it would aim this command at whatever run is active
    // now, and the orchestrator's receipt replay would report that run as interrupted.
    const gate = yield* readGate(commandId, input.threadId);
    if (gate.kind !== "none") return yield* answerInterrupt(gate, commandId);
    const interrupted = yield* threads
      .interruptThread({ projectId: shell.projectId, commandId, threadId: input.threadId })
      .pipe(Effect.result);
    if (interrupted._tag === "Failure") {
      const error = interrupted.failure;
      if (error._tag === "ThreadManagementThreadNotFoundError") {
        return yield* VoiceSeamRefusal.notFound();
      }
      const after = yield* readGate(commandId, input.threadId);
      if (after.kind === "none") return yield* VoiceSeamRefusal.unavailable(error);
      return yield* answerInterrupt(after, commandId);
    }
    const result = interrupted.success;
    return result.type === "interrupt_requested"
      ? ({
          kind: "requested",
          runId: result.run.id,
          sequence: result.dispatch.sequence,
        } satisfies VoiceInterruptResult)
      : ({ kind: "nothing_running" } satisfies VoiceInterruptResult);
  });

  return { send, interrupt };
};
