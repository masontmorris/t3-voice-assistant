import {
  RuntimeRequestId,
  type OrchestrationV2Run,
  type OrchestrationV2RuntimeRequest,
  type OrchestrationV2ThreadShell,
  type OrchestrationV2TurnItem,
} from "@t3tools/contracts";
import { subagentResultForRun } from "@t3tools/provider-core/server/subagentProjection";
import * as NodeSqliteClient from "@t3tools/shared/nodeSqliteClient";
import * as DateTime from "effect/DateTime";
import * as Effect from "effect/Effect";
import * as Result from "effect/Result";
import * as Schema from "effect/Schema";
import type * as SqlClient from "effect/sql/SqlClient";

import * as ThreadManagementService from "../orchestration-v2/ThreadManagementService.ts";
import { cardNeed, requestNeed, type VoicePending } from "./pending.ts";
import type { SettleAnswer, SettleCheck, SettleTrigger, VoiceRunResult } from "./watch.ts";

/** A check reads current state; it does not wait (`waitForThread` waits at least 1 ms). */
const CHECK_TIMEOUT_MS = 1;
/** A terminal run that has not settled stores no event when its continuation is declined. */
const FOLLOW_UP_TIMEOUT_MS = 60_000;
const RESULT_TEXT_LIMIT = 1_200;

export interface OutcomeSources {
  readonly sql: SqlClient.SqlClient;
  readonly threads: Pick<
    ThreadManagementService.ThreadManagementService["Service"],
    "waitForThread" | "getThreadRecords" | "getThreadEventSequence" | "getThreadShell"
  >;
}

export type PendingJoin =
  | { readonly kind: "none" }
  | { readonly kind: "need"; readonly pending: VoicePending }
  /** The shell picked a request that asks no one, and its pick can hide a waiting card. */
  | { readonly kind: "blind" };

/**
 * The one rule for "a need is pending". A card counts when the shell picked it, so T3's own rule
 * decides whether a link card still asks. Otherwise the newest relayable pending request is the need.
 */
const pendingJoin = (
  shell: Pick<OrchestrationV2ThreadShell, "pendingRuntimeRequest">,
  records: {
    readonly runtimeRequests: ReadonlyArray<OrchestrationV2RuntimeRequest>;
    readonly turnItems: ReadonlyArray<OrchestrationV2TurnItem>;
  },
): PendingJoin => {
  const pick = shell.pendingRuntimeRequest;
  if (pick?.userOnly === true) return { kind: "need", pending: cardNeed(pick.id) };
  const newest = records.runtimeRequests
    .filter((request) => request.status === "pending")
    .toSorted(
      (left, right) =>
        DateTime.toEpochMillis(right.createdAt) - DateTime.toEpochMillis(left.createdAt),
    )
    .flatMap((request) => requestNeed(request, records.turnItems) ?? [])[0];
  if (newest !== undefined) return { kind: "need", pending: newest };
  return pick?.kind === "dynamic_tool_call" || pick?.kind === "auth_refresh"
    ? { kind: "blind" }
    : { kind: "none" };
};

/** Cuts at the last sentence end inside the limit, else at the last space. */
export const cutAtSentence = (text: string, limit = RESULT_TEXT_LIMIT) => {
  if (text.length <= limit) return text;
  const head = text.slice(0, limit);
  const sentenceEnd = Math.max(
    ...[". ", "! ", "? ", ".\n", "!\n", "?\n", "\n\n"].map((end) => head.lastIndexOf(end)),
  );
  if (sentenceEnd > 0) return head.slice(0, sentenceEnd + 1).trimEnd();
  const space = head.lastIndexOf(" ");
  return `${(space > 0 ? head.slice(0, space) : head).trimEnd()}…`;
};

const isThreadNotFound = Schema.is(ThreadManagementService.ThreadManagementThreadNotFoundError);

const NOTHING: SettleAnswer = { kind: "nothing" };

/**
 * The settle read: `done` only from a fresh read of T3's own settle rule. T3 says when the thread
 * settled. The run `done` names is `resultRun`, or, for a watch anchored on a run, that run's own
 * stored record, with its own final message or none. `resultRun`'s text is never spoken as the
 * anchor's. Every failure becomes an answer, so the watch fiber decides what happens next.
 */
export const makeSettleCheck = (sources: OutcomeSources): SettleCheck => {
  const { sql, threads } = sources;

  const report = Effect.fn("VoiceSeam.outcome.report")(function* (
    trigger: SettleTrigger,
    resultRun: OrchestrationV2Run,
  ) {
    const { threadId } = trigger;
    const reportedId = trigger.anchorRunId ?? resultRun.id;
    const runIds = [reportedId];
    const read = yield* sql
      .withTransaction(
        Effect.gen(function* () {
          // Read first, so a need committed after it shows in the records or in the re-read.
          const asOfSequence = yield* threads.getThreadEventSequence(threadId);
          const shell = yield* threads.getThreadShell(threadId);
          if (shell === null) return null;
          const { runtimeRequests } = yield* threads.getThreadRecords(
            threadId,
            ["runtimeRequests"],
            { runtimeRequestStatuses: ["pending"] },
          );
          const prompts =
            runtimeRequests.length === 0
              ? []
              : (yield* threads.getThreadRecords(threadId, ["turnItems"], {
                  turnItemRequestIds: runtimeRequests.map((request) =>
                    RuntimeRequestId.make(request.id),
                  ),
                })).turnItems;
          const result = yield* threads.getThreadRecords(
            threadId,
            ["runs", "messages", "turnItems", "providerTurns"],
            {
              runIds,
              messageRoles: ["assistant"],
              messageRunIds: runIds,
              turnItemRunIds: runIds,
            },
          );
          return { asOfSequence, shell, runtimeRequests, prompts, result };
        }),
      )
      .pipe(NodeSqliteClient.readOnly);
    if (read === null) return { kind: "gone" } as const;
    if (
      pendingJoin(read.shell, { runtimeRequests: read.runtimeRequests, turnItems: read.prompts })
        .kind !== "none"
    ) {
      // The need ends with a stored event, and that event triggers the next check.
      return NOTHING;
    }
    // The anchor's status comes from its own stored record, never from `resultRun` or a guess.
    const stored = read.result.runs.find((candidate) => candidate.id === reportedId);
    const run = trigger.anchorRunId === undefined ? (stored ?? resultRun) : stored;
    if (run === undefined) return NOTHING;
    const status = run.status;
    if (!ThreadManagementService.isTerminalRunStatus(status)) return NOTHING;
    // No `runs`, so the result never falls back to another run's reply.
    const own = subagentResultForRun(read.result, run);
    // An anchor that is not the result run speaks only what that run itself stored. T3's stand-in
    // line for a run with no answer ("Child task ended with status ...") is not that.
    const result =
      run.id !== resultRun.id && own.messageId === null && own.turnItemId === null
        ? { ...own, text: "" }
        : own;
    // A need stored after the first read is not in the records, so the send waits for the watch
    // to emit everything through the re-read and checks again.
    const reread = yield* threads.getThreadEventSequence(threadId);
    if (reread !== read.asOfSequence) return { kind: "catch_up", atSequence: reread } as const;
    return {
      kind: "done",
      done: {
        type: "done",
        threadId,
        runId: run.id,
        status,
        result: {
          text: cutAtSentence(result.text),
          messageId: result.messageId,
          turnItemId: result.turnItemId,
        } satisfies VoiceRunResult,
        sequence: trigger.sequence,
        asOfSequence: read.asOfSequence,
      },
    } as const;
  });

  const check: (trigger: SettleTrigger, timeoutMs: number) => Effect.Effect<SettleAnswer> =
    Effect.fn("VoiceSeam.outcome.check")(function* (trigger: SettleTrigger, timeoutMs: number) {
      const waited = yield* threads
        .waitForThread({
          projectId: trigger.projectId,
          threadId: trigger.threadId,
          timeoutMs,
          untilDelegatedWorkSettled: true,
        })
        .pipe(Effect.result);
      if (Result.isFailure(waited)) {
        // The thread was deleted or left its project.
        if (isThreadNotFound(waited.failure)) return { kind: "gone" } as const;
        return { kind: "unavailable", cause: waited.failure } as const;
      }
      const { run, timedOut, resultRun } = waited.success;
      if (!timedOut && resultRun !== undefined) {
        return yield* report(trigger, resultRun).pipe(
          Effect.catch((cause) => Effect.succeed({ kind: "unavailable", cause } as const)),
        );
      }
      // A terminal run that has not settled waits on a continuation or delegated work.
      if (run !== null && ThreadManagementService.isTerminalRunStatus(run.status)) {
        return { kind: "follow_up", wait: check(trigger, FOLLOW_UP_TIMEOUT_MS) } as const;
      }
      return NOTHING;
    });

  return (trigger) => check(trigger, CHECK_TIMEOUT_MS);
};
