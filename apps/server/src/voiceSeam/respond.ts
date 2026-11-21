import { RuntimeRequestId } from "@t3tools/contracts";
import type * as Digest from "effect/crypto/Digest";
import * as Effect from "effect/Effect";
import * as Option from "effect/Option";

import type * as CommandReceiptStore from "../orchestration-v2/CommandReceiptStore.ts";
import type * as ThreadManagementService from "../orchestration-v2/ThreadManagementService.ts";
import { voiceCommandId } from "./launch.ts";
import { requestNeed } from "./pending.ts";
import type { RequestInput, VoiceRespondResult } from "./protocol.ts";
import { VoiceSeamRefusal } from "./refusal.ts";

export interface RespondSources {
  readonly digest: Digest.Digest["Service"];
  readonly threads: Pick<
    ThreadManagementService.ThreadManagementService["Service"],
    "dispatch" | "getThreadRecords" | "getThreadEventSequence"
  >;
  readonly receipts: Pick<CommandReceiptStore.CommandReceiptStoreV2["Service"], "getByCommandId">;
}

/** The T3 decision each answer the voice may give maps to. Only these two: nothing is approved for a session or always. */
const DECISION = { approve: "accept", deny: "decline" } as const;

/**
 * Answers one pending approval for the caller, from a pending approval of a visible thread only. A receipt does not hold
 * the payload, so a replay answers only when T3 resolved the named request with this call's decision.
 */
export const make = (sources: RespondSources) => {
  const { threads, receipts } = sources;
  const unavailable = Effect.mapError(VoiceSeamRefusal.unavailable);

  const respond = Effect.fn("VoiceSeam.respondToRequest")(function* (
    input: RequestInput<"respond">,
  ) {
    const commandId = yield* voiceCommandId(sources.digest, "respond", input.requestId);
    const requestId = RuntimeRequestId.make(input.pendingRequestId);
    const result = (sequence: number, replayed: boolean) =>
      ({
        threadId: input.threadId,
        pendingRequestId: input.pendingRequestId,
        decision: input.decision,
        sequence,
        replayed,
      }) satisfies VoiceRespondResult;
    /** A replay of this request id answers only when T3 resolved this request with this decision. */
    const replay = Effect.fn("VoiceSeam.respondReplay")(function* () {
      const answered = yield* threads
        .getThreadRecords(input.threadId, ["runtimeRequests"], {
          runtimeRequestStatuses: ["resolved"],
        })
        .pipe(unavailable);
      const request = answered.runtimeRequests.find((candidate) => candidate.id === requestId);
      if (request === undefined || request.decision !== DECISION[input.decision]) {
        return yield* VoiceSeamRefusal.invalidRequest(
          "That request id already answered something else.",
        );
      }
      const sequence = yield* threads.getThreadEventSequence(input.threadId).pipe(unavailable);
      return result(sequence, true);
    });
    const fromReceipt = (receipt: CommandReceiptStore.CommandReceiptV2) => {
      if (receipt.threadId !== input.threadId) {
        return Effect.fail(
          VoiceSeamRefusal.invalidRequest("That request already went to another thread."),
        );
      }
      if (receipt.status === "rejected") {
        return Effect.fail(VoiceSeamRefusal.refused({ reason: receipt.error, commandId }));
      }
      return replay();
    };
    const receipt = yield* receipts.getByCommandId(commandId).pipe(unavailable);
    if (Option.isSome(receipt)) return yield* fromReceipt(receipt.value);
    const records = yield* threads
      .getThreadRecords(input.threadId, ["runtimeRequests", "turnItems"], {
        runtimeRequestStatuses: ["pending"],
        turnItemRequestIds: [requestId],
      })
      .pipe(unavailable);
    const request = records.runtimeRequests.find((candidate) => candidate.id === requestId);
    if (request === undefined) {
      return yield* VoiceSeamRefusal.invalidRequest("That request is not waiting any more.");
    }
    const need = requestNeed(request, records.turnItems);
    if (need === null || need.kind !== "approval") {
      return yield* VoiceSeamRefusal.invalidRequest("That request needs an answer in the app.");
    }
    const dispatched = yield* threads
      .dispatch({
        type: "runtime-request.respond",
        commandId,
        threadId: input.threadId,
        requestId,
        decision: DECISION[input.decision],
      })
      .pipe(Effect.result);
    if (dispatched._tag === "Failure") {
      // A response that failed after T3 recorded its command answers as its retry would.
      const after = yield* receipts.getByCommandId(commandId).pipe(unavailable);
      if (Option.isNone(after)) return yield* VoiceSeamRefusal.unavailable(dispatched.failure);
      return yield* fromReceipt(after.value);
    }
    const { sequence, replayed } = dispatched.success;
    // The orchestrator replays a command id it already recorded, whatever this call asked: a concurrent copy.
    if (replayed === true) return yield* replay();
    return result(sequence, false);
  });

  return { respond };
};
