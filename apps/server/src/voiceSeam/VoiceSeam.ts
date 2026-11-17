import {
  type AuthSessionId,
  isDelegatedAppThread,
  isListedThreadLineage,
  type OrchestrationV2ConversationMessage,
  type OrchestrationV2Run,
  type OrchestrationV2ThreadShell,
} from "@t3tools/contracts";
import { backgroundWorkHoldsCompletion } from "@t3tools/shared/orchestrationV2PendingBackgroundWork";
import * as Context from "effect/Context";
import * as DateTime from "effect/DateTime";
import * as Effect from "effect/Effect";
import * as Layer from "effect/Layer";
import * as Schema from "effect/Schema";
import * as SqlClient from "effect/sql/SqlClient";
import type * as Stream from "effect/Stream";

import * as ProjectStore from "../orchestration-v2/ProjectStore.ts";
import { loadSubscriberShellSnapshot } from "../orchestration-v2/ShellStream.ts";
import * as ThreadManagementService from "../orchestration-v2/ThreadManagementService.ts";
import * as OrchestrationEventStore from "../persistence/OrchestrationEventStore.ts";
import {
  allowsOperation,
  allowsProject,
  type VoiceOperation,
  type VoiceSeamConfig,
} from "./ceiling.ts";
import { cutAtSentence, readThreadNow, runResult } from "./outcome.ts";
import {
  LIST_LIMIT,
  RefusalCode,
  type RequestInput,
  type ThreadActivity as ThreadActivitySchema,
  type VoiceHello,
  type VoiceThreadList,
  type VoiceThreadState,
} from "./protocol.ts";
import * as Watch from "./watch.ts";

export class VoiceSeamRefusal extends Schema.TaggedError<VoiceSeamRefusal>()("VoiceSeamRefusal", {
  code: RefusalCode,
  /** Short text the voice may speak. Never built from a cause. */
  message: Schema.String,
  cause: Schema.optional(Schema.Defect()),
}) {
  /** Missing, deleted, unlisted, delegated, or outside the allowed projects: the voice cannot probe which. */
  static readonly notFound = () =>
    new VoiceSeamRefusal({ code: "not_found", message: "I can't see that thread." });

  static readonly unavailable = (cause: unknown) =>
    new VoiceSeamRefusal({ code: "unavailable", message: "I can't reach T3 right now.", cause });

  static readonly operationNotAllowed = () =>
    new VoiceSeamRefusal({ code: "operation_not_allowed", message: "I'm not allowed to do that." });

  static readonly projectNotAllowed = () =>
    new VoiceSeamRefusal({
      code: "project_not_allowed",
      message: "I'm not allowed to work in that project.",
    });

  static readonly tooManyWatches = () =>
    new VoiceSeamRefusal({
      code: "too_many_watches",
      message: "I'm following too many threads already.",
    });

  static readonly invalidRequest = () =>
    new VoiceSeamRefusal({ code: "invalid_request", message: "T3 couldn't read that request." });

  /** An operation the seam lists but does not perform yet. */
  static readonly notBuilt = () =>
    new VoiceSeamRefusal({ code: "unavailable", message: "I can't do that yet." });
}

export type ThreadActivity = typeof ThreadActivitySchema.Type;
export type { VoiceThreadList, VoiceThreadState };

/** A caller the route authenticated. Only sessions minted with subject "voice" get one. */
export interface VoiceCaller {
  readonly sessionId: AuthSessionId;
  /** The idempotency namespace is the subject, not the session, so a retry with a new token still replays. */
  readonly namespace: "voice";
  /** voice-seam.json as it read when this caller's socket opened. */
  readonly config: VoiceSeamConfig;
}

export class VoiceSeam extends Context.Service<
  VoiceSeam,
  {
    readonly hello: (caller: VoiceCaller) => Effect.Effect<VoiceHello, VoiceSeamRefusal>;
    readonly listThreads: (
      caller: VoiceCaller,
      input: RequestInput<"list_threads">,
    ) => Effect.Effect<VoiceThreadList, VoiceSeamRefusal>;
    readonly readThread: (
      caller: VoiceCaller,
      input: RequestInput<"read_thread">,
    ) => Effect.Effect<VoiceThreadState, VoiceSeamRefusal>;
    /** Checks the thread now, then replays after `afterSequence` and follows live. */
    readonly watch: (
      caller: VoiceCaller,
      input: Watch.WatchInput,
    ) => Effect.Effect<Stream.Stream<Watch.WatchItem>, VoiceSeamRefusal>;
  }
>()("t3/voiceSeam/VoiceSeam") {}

export const requireOperation = (caller: VoiceCaller, operation: VoiceOperation) =>
  allowsOperation(caller.config, operation)
    ? Effect.void
    : Effect.fail(VoiceSeamRefusal.operationNotAllowed());

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

/** The rule of listProjectThreads, narrowed to the allowed projects. List, read and watch share it. */
const isVisible = (config: VoiceSeamConfig) => (shell: OrchestrationV2ThreadShell) =>
  isListedThreadLineage(shell.lineage) &&
  !isDelegatedAppThread(shell) &&
  allowsProject(config, shell.projectId);

const make = Effect.gen(function* () {
  const sql = yield* SqlClient.SqlClient;
  const threads = yield* ThreadManagementService.ThreadManagementService;
  const eventStore = yield* OrchestrationEventStore.OrchestrationEventStore;
  const projects = yield* ProjectStore.ProjectStoreV2;

  const hello = Effect.fn("VoiceSeam.hello")(function* (caller: VoiceCaller) {
    const { config } = caller;
    const rows = yield* projects.list({
      projectIds: config.projects.map((project) => project.projectId),
    });
    const latestSequence = yield* eventStore.latestApplicationSequence;
    return {
      type: "hello",
      ceiling: {
        maxRuntimeMode: config.maxRuntimeMode,
        interactionMode: "default",
        operations: config.operations,
        maxWatches: config.maxWatches,
      },
      // A configured project that no longer exists is not one the voice can work in.
      projects: config.projects.flatMap((project) => {
        const row = rows.find((candidate) => candidate.projectId === project.projectId);
        return row === undefined
          ? []
          : [{ projectId: project.projectId, name: row.title, spokenNames: project.spokenNames }];
      }),
      latestSequence,
    } satisfies VoiceHello;
  }, Effect.mapError(VoiceSeamRefusal.unavailable));

  const listThreads = Effect.fn("VoiceSeam.listThreads")(function* (
    caller: VoiceCaller,
    input: RequestInput<"list_threads">,
  ) {
    yield* requireOperation(caller, "list");
    const { projectId } = input;
    if (projectId !== undefined && !allowsProject(caller.config, projectId)) {
      return yield* VoiceSeamRefusal.projectNotAllowed();
    }
    const visible = isVisible(caller.config);
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
      .filter(
        (shell) => visible(shell) && (projectId === undefined || shell.projectId === projectId),
      )
      .toSorted(
        (left, right) =>
          DateTime.toEpochMillis(right.updatedAt) - DateTime.toEpochMillis(left.updatedAt) ||
          right.id.localeCompare(left.id),
      );
    const listed = kept.slice(0, Math.min(input.limit ?? LIST_LIMIT, LIST_LIMIT));
    return {
      threads: listed.map((shell): VoiceThreadList["threads"][number] => ({
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

  const readThread = Effect.fn("VoiceSeam.readThread")(function* (
    caller: VoiceCaller,
    input: RequestInput<"read_thread">,
  ) {
    yield* requireOperation(caller, "read");
    const { threadId } = input;
    // The only write: a migrated v1 thread imports its transcript before any records read.
    yield* threads
      .ensureLegacyTranscript(threadId)
      .pipe(Effect.mapError(VoiceSeamRefusal.unavailable));
    const read = yield* readThreadNow({ sql, threads }, threadId, (shell) =>
      shell.latestRunId === null ? [] : [shell.latestRunId],
    ).pipe(Effect.mapError(VoiceSeamRefusal.unavailable));
    if (read === null || !isVisible(caller.config)(read.shell)) {
      return yield* VoiceSeamRefusal.notFound();
    }
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

  const watch = Effect.fn("VoiceSeam.watch")(function* (
    caller: VoiceCaller,
    input: Watch.WatchInput,
  ) {
    yield* requireOperation(caller, "watch");
    const visible = isVisible(caller.config);
    // The watch checks again as it starts, and ends with not_found if the thread went in between.
    const shell = yield* threads
      .getThreadShell(input.threadId)
      .pipe(Effect.mapError(VoiceSeamRefusal.unavailable));
    if (shell === null || !visible(shell)) return yield* VoiceSeamRefusal.notFound();
    return Watch.fromServices({ sql, threads, eventStore, isVisible: visible })(input);
  });

  return VoiceSeam.of({ hello, listThreads, readThread, watch });
});

export const layer = Layer.effect(VoiceSeam, make);
