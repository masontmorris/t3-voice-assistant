import {
  idleCompactionOf,
  RuntimeRequestId,
  type OrchestrationV2ConversationMessage,
  type OrchestrationV2Run,
  type OrchestrationV2RuntimeRequest,
  type OrchestrationV2ThreadProjection,
  type OrchestrationV2ThreadShell,
  type OrchestrationV2TurnItem,
  type RunId,
  type ThreadId,
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

type Threads = ThreadManagementService.ThreadManagementService["Service"];

interface ReadSources {
  readonly sql: SqlClient.SqlClient;
  readonly threads: Pick<Threads, "getThreadRecords" | "getThreadEventSequence" | "getThreadShell">;
}

export interface OutcomeSources extends ReadSources {
  readonly threads: ReadSources["threads"] & Pick<Threads, "waitForThread">;
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
  if (pick?.userOnly === true) return { kind: "need", pending: cardNeed(pick.id, "card") };
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

/** The turn items `subagentResultForRun` reads: the reply, a failure, and a stop notice. */
const RESULT_ITEM_TYPES = [
  "assistant_message",
  "error",
  "system_notice",
] as const satisfies ReadonlyArray<OrchestrationV2TurnItem["type"]>;

/** The join for a shell that shows a pending request or card, from the thread's pending records. */
export const readPendingJoin = Effect.fn("VoiceSeam.outcome.readPendingJoin")(function* (
  threads: Pick<Threads, "getThreadRecords">,
  shell: Pick<OrchestrationV2ThreadShell, "id" | "pendingRuntimeRequest">,
) {
  const { runtimeRequests } = yield* threads.getThreadRecords(shell.id, ["runtimeRequests"], {
    runtimeRequestStatuses: ["pending"],
  });
  const prompts =
    runtimeRequests.length === 0
      ? []
      : (yield* threads.getThreadRecords(shell.id, ["turnItems"], {
          turnItemRequestIds: runtimeRequests.map((request) => RuntimeRequestId.make(request.id)),
        })).turnItems;
  return pendingJoin(shell, { runtimeRequests, turnItems: prompts });
});

/** An assistant message a voice may speak: the text is final and a run owns it. */
export const isFinishedReply = (
  message: OrchestrationV2ConversationMessage,
): message is OrchestrationV2ConversationMessage & { readonly runId: RunId } =>
  message.role === "assistant" &&
  message.runId !== null &&
  !message.streaming &&
  message.text.trim().length > 0;

export const newestFirst = (
  left: OrchestrationV2ConversationMessage,
  right: OrchestrationV2ConversationMessage,
) => DateTime.toEpochMillis(right.updatedAt) - DateTime.toEpochMillis(left.updatedAt);

/**
 * The newest finished reply among `runs`, leaving out a run T3 started on its own to compact the
 * thread: its user message is T3's idle `/compact`, and the user never saw it as a turn.
 */
const newestVisibleReply = (
  runs: ReadonlyArray<OrchestrationV2Run>,
  messages: ReadonlyArray<OrchestrationV2ConversationMessage>,
) => {
  const compactionRunIds = new Set(
    runs.flatMap((run) => {
      const prompt = messages.find((message) => message.id === run.userMessageId);
      return prompt !== undefined && idleCompactionOf(prompt) !== undefined ? [run.id] : [];
    }),
  );
  return messages
    .filter(isFinishedReply)
    .filter((message) => !compactionRunIds.has(message.runId))
    .toSorted(newestFirst)[0];
};

/**
 * The thread now, in one read-only transaction: its sequence first, so a need committed after it
 * shows in the records or a later re-read. Null when no thread. `earlierRuns` also reads an earlier
 * run's reply when the latest has none.
 */
export const readThreadNow = Effect.fn("VoiceSeam.outcome.readThreadNow")(function* (
  sources: ReadSources,
  threadId: ThreadId,
  runIdsOf: (shell: OrchestrationV2ThreadShell) => ReadonlyArray<RunId>,
  earlierRuns = 0,
) {
  const { threads } = sources;
  return yield* sources.sql
    .withTransaction(
      Effect.gen(function* () {
        const asOfSequence = yield* threads.getThreadEventSequence(threadId);
        const shell = yield* threads.getThreadShell(threadId);
        if (shell === null) return null;
        const join = yield* readPendingJoin(threads, shell);
        const runIds = runIdsOf(shell);
        const records = yield* threads.getThreadRecords(
          threadId,
          ["runs", "messages", "turnItems", "providerTurns"],
          {
            runIds,
            messageRoles: ["assistant"],
            messageRunIds: runIds,
            turnItemRunIds: runIds,
            turnItemTypes: RESULT_ITEM_TYPES,
          },
        );
        const earlierReply =
          earlierRuns === 0 || records.messages.some(isFinishedReply)
            ? undefined
            : yield* Effect.gen(function* () {
                const { runs } = yield* threads.getThreadRecords(threadId, ["runs"], {
                  latestRunLimit: earlierRuns + 1,
                });
                const earlier = runs.filter((run) => !runIds.includes(run.id));
                if (earlier.length === 0) return undefined;
                const { messages } = yield* threads.getThreadRecords(threadId, ["messages"], {
                  messageRoles: ["user", "assistant"],
                  messageRunIds: earlier.map((run) => run.id),
                });
                return newestVisibleReply(earlier, messages);
              });
        return { asOfSequence, shell, join, records, earlierReply };
      }),
    )
    .pipe(NodeSqliteClient.readOnly);
});

/**
 * T3's own result for a run, cut for speech. No `runs`, so it never falls back to an earlier run's
 * reply. A run that stored no message and no result item gets empty text, not T3's stand-in line
 * ("Child task ended with status ..."), which the run never said. Python words the status.
 */
export const runResult = (
  records: Pick<OrchestrationV2ThreadProjection, "messages" | "turnItems" | "providerTurns">,
  run: OrchestrationV2Run,
): VoiceRunResult => {
  const { messages, turnItems, providerTurns } = records;
  const result = subagentResultForRun({ messages, turnItems, providerTurns }, run);
  const said = result.messageId !== null || result.turnItemId !== null;
  return {
    text: said ? cutAtSentence(result.text) : "",
    messageId: result.messageId,
    turnItemId: result.turnItemId,
  };
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
  const { threads } = sources;

  const report = Effect.fn("VoiceSeam.outcome.report")(function* (
    trigger: SettleTrigger,
    resultRun: OrchestrationV2Run,
  ) {
    const { threadId } = trigger;
    const reportedId = trigger.anchorRunId ?? resultRun.id;
    const read = yield* readThreadNow(sources, threadId, () => [reportedId]);
    if (read === null) return { kind: "gone" } as const;
    // The need ends with a stored event, and that event triggers the next check.
    if (read.join.kind !== "none") return NOTHING;
    // The anchor's status comes from its own stored record, never from `resultRun` or a guess.
    const stored = read.records.runs.find((candidate) => candidate.id === reportedId);
    const run = trigger.anchorRunId === undefined ? (stored ?? resultRun) : stored;
    if (run === undefined) return NOTHING;
    const status = run.status;
    if (!ThreadManagementService.isTerminalRunStatus(status)) return NOTHING;
    const result = runResult(read.records, run);
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
        result,
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
