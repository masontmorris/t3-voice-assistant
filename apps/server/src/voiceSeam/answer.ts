import {
  RuntimeRequestId,
  type OrchestrationV2RuntimeRequest,
  type OrchestrationV2ThreadShell,
  type OrchestrationV2TurnItem,
} from "@t3tools/contracts";
import type * as Digest from "effect/crypto/Digest";
import * as Effect from "effect/Effect";
import * as Option from "effect/Option";

import type * as CommandReceiptStore from "../orchestration-v2/CommandReceiptStore.ts";
import { DispatchModeLimit } from "../orchestration-v2/DispatchModeLimit.ts";
import type * as ThreadManagementService from "../orchestration-v2/ThreadManagementService.ts";
import { dispatchModeLimit, isAboveCeiling, type VoiceSeamConfig } from "./ceiling.ts";
import { voiceCommandId } from "./launch.ts";
import { answerableQuestions, requestNeed, VOICE_QUESTION_LIMITS } from "./pending.ts";
import type { RequestInput, VoiceAnswerResult } from "./protocol.ts";
import { VoiceSeamRefusal } from "./refusal.ts";

export interface AnswerSources {
  readonly digest: Digest.Digest["Service"];
  readonly threads: Pick<
    ThreadManagementService.ThreadManagementService["Service"],
    "dispatch" | "getThreadRecords" | "getThreadEventSequence"
  >;
  readonly receipts: Pick<CommandReceiptStore.CommandReceiptStoreV2["Service"], "getByCommandId">;
}

type Question = NonNullable<ReturnType<typeof answerableQuestions>>[number];
type Given = RequestInput<"answer_question">["answers"][number];

/**
 * What T3 is sent for each question, or why the answers do not fit the request. Every question needs one answer, an
 * option must be one offered, and the caller's own words count only where the question takes them. An option goes in
 * as the client sends it: its `value`, else its label.
 */
const resolveAnswers = (
  questions: ReadonlyArray<Question>,
  given: ReadonlyArray<Given>,
):
  | { readonly ok: true; readonly answers: Record<string, string> }
  | { readonly ok: false; readonly message: string } => {
  const byId = new Map(given.map((answer) => [answer.questionId, answer]));
  if (byId.size !== given.length) return { ok: false, message: "A question was answered twice." };
  if (given.some((answer) => !questions.some((question) => question.id === answer.questionId))) {
    return { ok: false, message: "That answer names a question this request does not ask." };
  }
  const answers: Record<string, string> = {};
  for (const question of questions) {
    const answer = byId.get(question.id);
    if (answer === undefined) return { ok: false, message: "Every question needs an answer." };
    if (answer.kind === "own_words") {
      if (question.allowCustomAnswer === false) {
        return { ok: false, message: "That question takes one of its options only." };
      }
      if (answer.text.length > VOICE_QUESTION_LIMITS.ownWordsChars) {
        return { ok: false, message: "That answer is too long to give by voice." };
      }
      answers[question.id] = answer.text;
      continue;
    }
    const option = question.options.find((candidate) => candidate.label.trim() === answer.text);
    if (option === undefined) return { ok: false, message: "That is not one of the options." };
    answers[question.id] = option.value ?? option.label;
  }
  return { ok: true, answers };
};

const isUserInput = (
  item: OrchestrationV2TurnItem,
  requestId: string,
): item is Extract<OrchestrationV2TurnItem, { readonly type: "user_input_request" }> =>
  item.type === "user_input_request" && item.requestId === requestId;

/**
 * Answers one pending user-input request for the caller, from a pending request of a visible thread only. A receipt
 * does not hold the answers, so a replay answers only when T3 resolved the named request with the answers this call
 * would send, and every result is read back from what T3 recorded.
 */
export const make = (sources: AnswerSources) => {
  const { threads, receipts } = sources;
  const unavailable = Effect.mapError(VoiceSeamRefusal.unavailable);

  const answer = Effect.fn("VoiceSeam.answerQuestions")(function* (
    config: VoiceSeamConfig,
    shell: Pick<OrchestrationV2ThreadShell, "runtimeMode" | "interactionMode">,
    input: RequestInput<"answer_question">,
  ) {
    const commandId = yield* voiceCommandId(sources.digest, "answer", input.requestId);
    const requestId = RuntimeRequestId.make(input.pendingRequestId);

    /** The request as T3 holds it with the given status, and the question it asked. */
    const read = Effect.fn("VoiceSeam.answerRead")(function* (
      status: OrchestrationV2RuntimeRequest["status"],
    ) {
      const records = yield* threads
        .getThreadRecords(input.threadId, ["runtimeRequests", "turnItems"], {
          runtimeRequestStatuses: [status],
          turnItemRequestIds: [requestId],
        })
        .pipe(unavailable);
      return {
        request: records.runtimeRequests.find((candidate) => candidate.id === requestId),
        turnItems: records.turnItems,
        item: records.turnItems.find((candidate) => isUserInput(candidate, input.pendingRequestId)),
      };
    });

    /** What T3 recorded for the request, which must be what this call asks for. */
    const recorded = Effect.fn("VoiceSeam.answerRecorded")(function* (
      sequence: number,
      replayed: boolean,
    ) {
      const { request, item } = yield* read("resolved");
      const questions = answerableQuestions(item);
      const resolved =
        questions === undefined ? undefined : resolveAnswers(questions, input.answers);
      if (questions === undefined || resolved === undefined || !resolved.ok) {
        return yield* VoiceSeamRefusal.invalidRequest(
          "That request id already answered something else.",
        );
      }
      const stored = request?.answers;
      const same =
        stored !== undefined &&
        questions.every((question) => stored[question.id] === resolved.answers[question.id]);
      if (!same) {
        // A retry of this request id with other answers, or an answer T3 recorded differently: never reported as sent.
        return yield* VoiceSeamRefusal.invalidRequest(
          "That request id already answered something else.",
        );
      }
      return {
        threadId: input.threadId,
        pendingRequestId: input.pendingRequestId,
        answers: questions.map((question) => ({
          questionId: question.id,
          answer: String(stored[question.id]),
        })),
        sequence,
        replayed,
      } satisfies VoiceAnswerResult;
    });

    const replay = Effect.fn("VoiceSeam.answerReplay")(function* () {
      const sequence = yield* threads.getThreadEventSequence(input.threadId).pipe(unavailable);
      return yield* recorded(sequence, true);
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
    if (isAboveCeiling(config, shell)) return yield* VoiceSeamRefusal.threadAboveCeiling();
    const { request, turnItems, item } = yield* read("pending");
    if (request === undefined) {
      return yield* VoiceSeamRefusal.invalidRequest("That request is not waiting any more.");
    }
    const need = requestNeed(request, turnItems);
    const questions = answerableQuestions(item);
    if (need === null || need.kind !== "question" || questions === undefined) {
      return yield* VoiceSeamRefusal.invalidRequest("That request needs an answer in the app.");
    }
    const resolved = resolveAnswers(questions, input.answers);
    if (!resolved.ok) return yield* VoiceSeamRefusal.invalidRequest(resolved.message);
    const dispatched = yield* threads
      .dispatch({
        type: "runtime-request.respond",
        commandId,
        threadId: input.threadId,
        requestId,
        answers: resolved.answers,
      })
      .pipe(
        // The orchestrator rechecks under the command lock: Mason may raise the mode after the preflight.
        Effect.provideService(DispatchModeLimit, dispatchModeLimit(config)),
        Effect.result,
      );
    if (dispatched._tag === "Failure") {
      const error = dispatched.failure;
      if ("_tag" in error) {
        if (error._tag === "OrchestratorThreadAboveModeLimitError") {
          return yield* VoiceSeamRefusal.threadAboveCeiling(error);
        }
        if (error._tag === "OrchestratorRuntimeModeAboveCeilingError") {
          return yield* VoiceSeamRefusal.aboveCeiling(error);
        }
      }
      // An answer that failed after T3 recorded its command answers as its retry would.
      const after = yield* receipts.getByCommandId(commandId).pipe(unavailable);
      if (Option.isNone(after)) return yield* VoiceSeamRefusal.unavailable(error);
      return yield* fromReceipt(after.value);
    }
    const { sequence, replayed } = dispatched.success;
    // The orchestrator replays a command id it already recorded, whatever this call asked: a concurrent copy.
    if (replayed === true) return yield* replay();
    return yield* recorded(sequence, false).pipe(
      // A fresh answer T3 does not show as recorded is not reported as given, and was not refused either.
      Effect.catchIf(
        (refusal) => refusal.code === "invalid_request",
        () => VoiceSeamRefusal.unavailable("T3 did not record the answers it was sent."),
      ),
    );
  });

  return { answer };
};
