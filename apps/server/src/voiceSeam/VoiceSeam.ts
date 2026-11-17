import {
  isDelegatedAppThread,
  isListedThreadLineage,
  type OrchestrationV2ConversationMessage,
  type OrchestrationV2Run,
  type OrchestrationV2ThreadShell,
  type ProjectId,
  type RunId,
  type ThreadId,
} from "@t3tools/contracts";
import { backgroundWorkHoldsCompletion } from "@t3tools/shared/orchestrationV2PendingBackgroundWork";
import * as Context from "effect/Context";
import * as DateTime from "effect/DateTime";
import * as Effect from "effect/Effect";
import * as Layer from "effect/Layer";
import * as Schema from "effect/Schema";
import * as SqlClient from "effect/sql/SqlClient";

import { loadSubscriberShellSnapshot } from "../orchestration-v2/ShellStream.ts";
import * as ThreadManagementService from "../orchestration-v2/ThreadManagementService.ts";
import * as OrchestrationEventStore from "../persistence/OrchestrationEventStore.ts";
import { cutAtSentence, readThreadNow, runResult } from "./outcome.ts";
import type { VoicePending } from "./pending.ts";
import type { VoiceRunResult } from "./watch.ts";

export class VoiceSeamRefusal extends Schema.TaggedError<VoiceSeamRefusal>()("VoiceSeamRefusal", {
  code: Schema.Literals([
    "operation_not_allowed",
    "project_not_allowed",
    "above_ceiling",
    "not_found",
    "gone",
    "refused",
    "too_many_watches",
    "no_default_model",
    "invalid_request",
    "unavailable",
  ]),
  /** Short text the voice may speak. Never built from a cause. */
  message: Schema.String,
  cause: Schema.optional(Schema.Defect()),
}) {
  /** Missing, deleted, unlisted, delegated, or outside the allowed projects: the voice cannot probe which. */
  static readonly notFound = () =>
    new VoiceSeamRefusal({ code: "not_found", message: "I can't see that thread." });

  static readonly unavailable = (cause: unknown) =>
    new VoiceSeamRefusal({ code: "unavailable", message: "I can't reach T3 right now.", cause });
}

/** How a thread reads to someone asking about it. */
export type ThreadActivity =
  | "working"
  | "waiting_on_you"
  | "waiting_on_background"
  | "queued"
  | "idle"
  | "finished"
  | "failed"
  | "interrupted"
  | "cancelled"
  | "rolled_back";

export interface VoiceThreadSummary {
  readonly threadId: ThreadId;
  readonly projectId: ProjectId;
  readonly title: string;
  readonly activity: ThreadActivity;
  readonly lastActivityAt: string;
  readonly pinned: boolean;
}

export interface VoiceThreadList {
  /** Every active thread with listed lineage in an allowed project, newest activity first, cut to `limit`. */
  readonly threads: ReadonlyArray<VoiceThreadSummary>;
  /** How many threads matched before the cut. */
  readonly total: number;
  /** When true, "no such thread" is not a claim the voice may make. */
  readonly truncated: boolean;
  /** The rows are the state as of this sequence: both come from one read-only transaction. */
  readonly asOfSequence: number;
}

export interface VoiceThreadState {
  readonly threadId: ThreadId;
  readonly projectId: ProjectId;
  readonly title: string;
  readonly activity: ThreadActivity;
  /** The shell's latest run, read in the same transaction as the shell. */
  readonly run: {
    readonly runId: RunId;
    readonly status: OrchestrationV2Run["status"];
    readonly startedAt: string | null;
  } | null;
  readonly pending: VoicePending | null;
  /** A settled terminal run's result, else the latest run's newest final assistant message. */
  readonly lastReply: (VoiceRunResult & { readonly at: string }) | null;
  /** This thread's event sequence, read first in the same read-only transaction as the rest. */
  readonly asOfSequence: number;
}

export interface VoiceSeamConfig {
  readonly allowedProjects: ReadonlySet<ProjectId>;
}

export class VoiceSeam extends Context.Service<
  VoiceSeam,
  {
    readonly listThreads: (input: {
      readonly limit?: number;
    }) => Effect.Effect<VoiceThreadList, VoiceSeamRefusal>;
    readonly readThread: (input: {
      readonly threadId: ThreadId;
    }) => Effect.Effect<VoiceThreadState, VoiceSeamRefusal>;
  }
>()("t3/voiceSeam/VoiceSeam") {}

/** The most threads a list carries; Python's picker reads at most this many. */
const LIST_LIMIT = 200;

const TERMINAL_ACTIVITY = {
  completed: "finished",
  failed: "failed",
  interrupted: "interrupted",
  cancelled: "cancelled",
  rolled_back: "rolled_back",
} as const satisfies Partial<Record<OrchestrationV2Run["status"], ThreadActivity>>;

const TERMINAL_ACTIVITIES: ReadonlySet<ThreadActivity> = new Set(Object.values(TERMINAL_ACTIVITY));

/** From the shell row alone, which is all a list has. */
const deriveActivity = (shell: OrchestrationV2ThreadShell): ThreadActivity => {
  const asks = shell.pendingRuntimeRequest;
  // T3 does not count these two kinds as waiting on the user (RestartContinuation.ts).
  if (asks !== null && asks.kind !== "dynamic_tool_call" && asks.kind !== "auth_refresh") {
    return "waiting_on_you";
  }
  if (shell.activityRunStatus != null) return "working";
  switch (shell.status) {
    case "idle":
      return "idle";
    case "queued":
      return "queued";
    // Older servers leave out activityRunStatus.
    case "preparing":
    case "starting":
    case "running":
    case "waiting":
      return "working";
    default:
      return backgroundWorkHoldsCompletion(shell.pendingBackgroundTasks ?? [])
        ? "waiting_on_background"
        : TERMINAL_ACTIVITY[shell.status];
  }
};

const iso = (time: DateTime.Utc) => DateTime.formatIso(time);

/** The latest run's newest final assistant message, for a thread that has not settled. */
const latestFinalReply = (
  messages: ReadonlyArray<OrchestrationV2ConversationMessage>,
): VoiceThreadState["lastReply"] => {
  const message = messages
    .filter((candidate) => !candidate.streaming && candidate.text.trim().length > 0)
    .toSorted(
      (left, right) =>
        DateTime.toEpochMillis(right.updatedAt) - DateTime.toEpochMillis(left.updatedAt),
    )[0];
  return message === undefined
    ? null
    : {
        text: cutAtSentence(message.text),
        messageId: message.id,
        turnItemId: null,
        at: iso(message.updatedAt),
      };
};

const make = Effect.fn("VoiceSeam.make")(function* (config: VoiceSeamConfig) {
  const sql = yield* SqlClient.SqlClient;
  const threads = yield* ThreadManagementService.ThreadManagementService;
  const eventStore = yield* OrchestrationEventStore.OrchestrationEventStore;

  /** The rule of listProjectThreads, narrowed to the allowed projects. */
  const isVisible = (shell: OrchestrationV2ThreadShell) =>
    isListedThreadLineage(shell.lineage) &&
    !isDelegatedAppThread(shell) &&
    config.allowedProjects.has(shell.projectId);

  const listThreads = Effect.fn("VoiceSeam.listThreads")(function* (input: {
    readonly limit?: number;
  }) {
    const { snapshot } = yield* loadSubscriberShellSnapshot({
      sql,
      listShellIndex: threads.listActiveShellIndex(),
      readThreads: (threadIds) =>
        threads.readShellSnapshot({
          location: "active",
          ...(threadIds === undefined ? {} : { threadIds }),
        }),
      listProjects: Effect.succeed([]),
      latestSequence: eventStore.latestApplicationSequence,
      // Opting into the older-settled trim keeps only the 25 newest settled threads and counts
      // the rest, which would make "no such thread" untrue. Idle subagents are never listed.
      options: { omitIdleSubagents: true, omitOlderSettled: false },
    }).pipe(Effect.mapError(VoiceSeamRefusal.unavailable));
    const kept = snapshot.threads
      .filter(isVisible)
      .toSorted(
        (left, right) =>
          DateTime.toEpochMillis(right.updatedAt) - DateTime.toEpochMillis(left.updatedAt) ||
          right.id.localeCompare(left.id),
      );
    const listed = kept.slice(0, Math.min(input.limit ?? LIST_LIMIT, LIST_LIMIT));
    return {
      threads: listed.map((shell): VoiceThreadSummary => ({
        threadId: shell.id,
        projectId: shell.projectId,
        title: shell.title,
        activity: deriveActivity(shell),
        lastActivityAt: iso(shell.updatedAt),
        pinned: shell.pinnedAt != null,
      })),
      total: kept.length,
      truncated: kept.length > listed.length,
      asOfSequence: snapshot.snapshotSequence,
    } satisfies VoiceThreadList;
  });

  const readThread = Effect.fn("VoiceSeam.readThread")(function* (input: {
    readonly threadId: ThreadId;
  }) {
    const { threadId } = input;
    // The only write: a migrated v1 thread imports its transcript before any records read.
    yield* threads
      .ensureLegacyTranscript(threadId)
      .pipe(Effect.mapError(VoiceSeamRefusal.unavailable));
    const read = yield* readThreadNow({ sql, threads }, threadId, (shell) =>
      shell.latestRunId === null ? [] : [shell.latestRunId],
    ).pipe(Effect.mapError(VoiceSeamRefusal.unavailable));
    if (read === null || !isVisible(read.shell)) return yield* VoiceSeamRefusal.notFound();
    const { shell, records } = read;
    const pending = read.join.kind === "need" ? read.join.pending : null;
    // The join can find a need the shell's pick hides behind a request that asks no one.
    const activity = pending === null ? deriveActivity(shell) : "waiting_on_you";
    const run = records.runs.find((candidate) => candidate.id === shell.latestRunId) ?? null;
    const lastReply =
      run === null
        ? null
        : TERMINAL_ACTIVITIES.has(activity)
          ? { ...runResult(records, run), at: iso(run.completedAt ?? run.requestedAt) }
          : latestFinalReply(records.messages);
    return {
      threadId,
      projectId: shell.projectId,
      title: shell.title,
      activity,
      run:
        run === null
          ? null
          : {
              runId: run.id,
              status: run.status,
              startedAt: run.startedAt === null ? null : iso(run.startedAt),
            },
      pending,
      lastReply,
      asOfSequence: read.asOfSequence,
    } satisfies VoiceThreadState;
  });

  return VoiceSeam.of({ listThreads, readThread });
});

export const layer = (config: VoiceSeamConfig) => Layer.effect(VoiceSeam, make(config));
