import {
  type AuthSessionId,
  isDelegatedAppThread,
  isListedThreadLineage,
  type OrchestrationV2ConversationMessage,
  type OrchestrationV2Run,
  type OrchestrationV2ThreadShell,
  type RunId,
  type ThreadId,
} from "@t3tools/contracts";
import * as NodeSqliteClient from "@t3tools/shared/nodeSqliteClient";
import { backgroundWorkHoldsCompletion } from "@t3tools/shared/orchestrationV2PendingBackgroundWork";
import * as Context from "effect/Context";
import * as Digest from "effect/crypto/Digest";
import * as DateTime from "effect/DateTime";
import * as Effect from "effect/Effect";
import * as Layer from "effect/Layer";
import * as SqlClient from "effect/sql/SqlClient";
import type * as Stream from "effect/Stream";

import * as ServerEnvironment from "../environment/ServerEnvironment.ts";
import * as CommandReceiptStore from "../orchestration-v2/CommandReceiptStore.ts";
import * as ProjectStore from "../orchestration-v2/ProjectStore.ts";
import { loadSubscriberShellSnapshot } from "../orchestration-v2/ShellStream.ts";
import * as ThreadLaunch from "../orchestration-v2/ThreadLaunchService.ts";
import * as ThreadManagementService from "../orchestration-v2/ThreadManagementService.ts";
import * as OrchestrationEventStore from "../persistence/OrchestrationEventStore.ts";
import * as ProjectService from "../project/ProjectService.ts";
import * as ProviderRegistry from "../provider/ProviderRegistry.ts";
import * as ServerSettings from "../serverSettings.ts";
import {
  allowsOperation,
  allowsProject,
  type VoiceOperation,
  type VoiceSeamConfig,
} from "./ceiling.ts";
import {
  cutAtSentence,
  isFinishedReply,
  newestFirst,
  readPendingJoin,
  readThreadNow,
  runResult,
  type PendingJoin,
} from "./outcome.ts";
import * as Launch from "./launch.ts";
import * as Respond from "./respond.ts";
import * as Send from "./send.ts";
import {
  LIST_LIMIT,
  type RequestInput,
  type ThreadActivity as ThreadActivitySchema,
  type VoiceHello,
  type VoiceInterruptResult,
  type VoiceLaunchResult,
  type VoiceRespondResult,
  type VoiceSendResult,
  type VoiceThreadList,
  type VoiceThreadState,
} from "./protocol.ts";
import { VoiceSeamRefusal } from "./refusal.ts";
import * as Watch from "./watch.ts";

export { VoiceSeamRefusal };

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
    /** Starts a thread under the ceiling, or answers for the one this request id already started. */
    readonly launch: (
      caller: VoiceCaller,
      input: RequestInput<"launch">,
    ) => Effect.Effect<VoiceLaunchResult, VoiceSeamRefusal>;
    /**
     * Queues a message behind the thread's active run when T3's queue holds the ceiling, and
     * otherwise refuses while the thread is busy. A retry replays it.
     */
    readonly send: (
      caller: VoiceCaller,
      input: RequestInput<"send">,
    ) => Effect.Effect<VoiceSendResult, VoiceSeamRefusal>;
    /** Interrupts the thread's active run at any mode. A retry interrupts nothing. */
    readonly interrupt: (
      caller: VoiceCaller,
      input: RequestInput<"interrupt">,
    ) => Effect.Effect<VoiceInterruptResult, VoiceSeamRefusal>;
    /** Answers one pending approval of a visible thread. A retry answers from its receipt. */
    readonly respond: (
      caller: VoiceCaller,
      input: RequestInput<"respond">,
    ) => Effect.Effect<VoiceRespondResult, VoiceSeamRefusal>;
    /** Checks the thread now, then replays after `afterSequence` and follows live. */
    readonly watch: (
      caller: VoiceCaller,
      input: Watch.WatchInput,
    ) => Effect.Effect<Stream.Stream<Watch.WatchItem>, VoiceSeamRefusal>;
  }
>()("t3/voiceSeam/VoiceSeam") {}

const requireOperation = (caller: VoiceCaller, operation: VoiceOperation) =>
  allowsOperation(caller.config, operation)
    ? Effect.void
    : Effect.fail(VoiceSeamRefusal.operationNotAllowed());

/** How many runs before the latest one a read looks through for a reply the latest run lacks. */
const EARLIER_REPLY_RUNS = 5;

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

/** The one rule for a thread's activity: a list and a read both apply it, so they cannot disagree. */
const activityOf = (shell: OrchestrationV2ThreadShell, join: PendingJoin): ThreadActivity =>
  // The join can find a need the shell's pick hides behind a request that asks no one.
  join.kind === "need" ? "waiting_on_you" : deriveActivity(shell);

const iso = (time: DateTime.Utc) => DateTime.formatIso(time);

const replyOf = (
  message: OrchestrationV2ConversationMessage & { readonly runId: RunId },
): NonNullable<VoiceThreadState["lastReply"]> => ({
  text: cutAtSentence(message.text),
  messageId: message.id,
  turnItemId: null,
  runId: message.runId,
  at: iso(message.updatedAt),
});

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
  const digest = yield* Digest.Digest;
  const receipts = yield* CommandReceiptStore.CommandReceiptStoreV2;
  const settings = yield* ServerSettings.ServerSettingsService;
  const environmentId = yield* (yield* ServerEnvironment.ServerEnvironment).getEnvironmentId;
  const writes = Send.make({
    digest,
    threads,
    receipts,
    managedMaxRuntimeMode: settings.managedPolicy?.limits?.maxRuntimeMode,
  });
  const answers = Respond.make({ digest, threads, receipts });
  const launchThread = Launch.make({
    digest,
    threads,
    launches: yield* ThreadLaunch.ThreadLaunchService,
    receipts,
    projects: yield* ProjectService.ProjectService,
    settings,
    providers: yield* ProviderRegistry.ProviderRegistry,
  });

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
      environmentId,
    } satisfies VoiceHello;
  }, Effect.mapError(VoiceSeamRefusal.unavailable));

  const listThreads = Effect.fn("VoiceSeam.listThreads")(function* (
    caller: VoiceCaller,
    input: RequestInput<"list_threads">,
  ) {
    yield* requireOperation(caller, "list");
    const limit = input.limit ?? LIST_LIMIT;
    if (!Number.isInteger(limit) || limit < 1 || limit > LIST_LIMIT) {
      return yield* VoiceSeamRefusal.invalidRequest(
        `limit must be a whole number from 1 to ${LIST_LIMIT}.`,
      );
    }
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
    // The project filter comes before the sort and the cut, so `total` and `truncated` describe it.
    const kept = snapshot.threads
      .filter(
        (shell) => visible(shell) && (projectId === undefined || shell.projectId === projectId),
      )
      .toSorted(
        (left, right) =>
          DateTime.toEpochMillis(right.updatedAt) - DateTime.toEpochMillis(left.updatedAt) ||
          right.id.localeCompare(left.id),
      );
    const listed = kept.slice(0, limit);
    // A shell with no pending pick has no pending request or waiting card, so only the others
    // can differ from `deriveActivity`. These reads run at or after the snapshot's sequence, so
    // a difference is an event a watch from `asOfSequence` replays.
    const joins = new Map(
      yield* sql
        .withTransaction(
          Effect.forEach(
            listed.filter((shell) => shell.pendingRuntimeRequest !== null),
            (shell) =>
              readPendingJoin(threads, shell).pipe(Effect.map((join) => [shell.id, join] as const)),
          ),
        )
        .pipe(NodeSqliteClient.readOnly, Effect.mapError(VoiceSeamRefusal.unavailable)),
    );
    return {
      threads: listed.map((shell): VoiceThreadList["threads"][number] => ({
        threadId: shell.id,
        projectId: shell.projectId,
        title: shell.title,
        activity: activityOf(shell, joins.get(shell.id) ?? { kind: "none" }),
        lastActivityAt: iso(shell.updatedAt),
        pinned: shell.pinnedAt != null,
      })),
      total: kept.length,
      truncated: kept.length > listed.length,
      asOfSequence: snapshot.snapshotSequence,
    } satisfies VoiceThreadList;
  });

  /** Refuses a thread the caller cannot see before any read or write touches it. */
  const requireVisible = Effect.fn("VoiceSeam.requireVisible")(function* (
    caller: VoiceCaller,
    threadId: ThreadId,
  ) {
    const shell = yield* threads
      .getThreadShell(threadId)
      .pipe(Effect.mapError(VoiceSeamRefusal.unavailable));
    if (shell === null || !isVisible(caller.config)(shell)) {
      return yield* VoiceSeamRefusal.notFound();
    }
    return shell;
  });

  const readThread = Effect.fn("VoiceSeam.readThread")(function* (
    caller: VoiceCaller,
    input: RequestInput<"read_thread">,
  ) {
    yield* requireOperation(caller, "read");
    const { threadId } = input;
    yield* requireVisible(caller, threadId);
    // The only write: a migrated v1 thread imports its transcript before any records read.
    yield* threads
      .ensureLegacyTranscript(threadId)
      .pipe(Effect.mapError(VoiceSeamRefusal.unavailable));
    const read = yield* readThreadNow(
      { sql, threads },
      threadId,
      (shell) => (shell.latestRunId === null ? [] : [shell.latestRunId]),
      EARLIER_REPLY_RUNS,
    ).pipe(Effect.mapError(VoiceSeamRefusal.unavailable));
    if (read === null || !isVisible(caller.config)(read.shell)) {
      return yield* VoiceSeamRefusal.notFound();
    }
    const { shell, records } = read;
    const pending = read.join.kind === "need" ? read.join.pending : null;
    const pendingDetailed = read.join.kind === "need" && read.join.detailed;
    const activity = activityOf(shell, read.join);
    const run = records.runs.find((candidate) => candidate.id === shell.latestRunId) ?? null;
    const reply =
      records.messages.filter(isFinishedReply).toSorted(newestFirst)[0] ?? read.earlierReply;
    const lastReply =
      run === null
        ? null
        : TERMINAL_ACTIVITIES.has(activity)
          ? {
              ...runResult(records, run),
              runId: run.id,
              at: iso(run.completedAt ?? run.requestedAt),
            }
          : reply === undefined
            ? null
            : replyOf(reply);
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
      pendingDetailed,
      lastReply,
      asOfSequence: read.asOfSequence,
    } satisfies VoiceThreadState;
  });

  const launch = Effect.fn("VoiceSeam.launch")(function* (
    caller: VoiceCaller,
    input: RequestInput<"launch">,
  ) {
    yield* requireOperation(caller, "launch");
    // Before any read: a project outside the ceiling starts nothing and replays nothing.
    if (!allowsProject(caller.config, input.projectId)) {
      return yield* VoiceSeamRefusal.projectNotAllowed();
    }
    return yield* launchThread(caller.config, input);
  });

  const send = Effect.fn("VoiceSeam.send")(function* (
    caller: VoiceCaller,
    input: RequestInput<"send">,
  ) {
    yield* requireOperation(caller, "send");
    const shell = yield* requireVisible(caller, input.threadId);
    return yield* writes.send(caller.config, shell, input);
  });

  // No ceiling check: interrupting starts no work, and the voice must be able to stop a thread
  // Mason raised above it.
  const interrupt = Effect.fn("VoiceSeam.interrupt")(function* (
    caller: VoiceCaller,
    input: RequestInput<"interrupt">,
  ) {
    yield* requireOperation(caller, "interrupt");
    const shell = yield* requireVisible(caller, input.threadId);
    return yield* writes.interrupt(shell, input);
  });

  // No ceiling check: answering a request starts no work beyond what the thread already runs at, and the ceiling's
  // `respond` operation is what lets the voice answer at all.
  const respond = Effect.fn("VoiceSeam.respond")(function* (
    caller: VoiceCaller,
    input: RequestInput<"respond">,
  ) {
    yield* requireOperation(caller, "respond");
    yield* requireVisible(caller, input.threadId);
    return yield* answers.respond(input);
  });

  const watch = Effect.fn("VoiceSeam.watch")(function* (
    caller: VoiceCaller,
    input: Watch.WatchInput,
  ) {
    yield* requireOperation(caller, "watch");
    // The watch checks again as it starts, and ends with not_found if the thread went in between.
    yield* requireVisible(caller, input.threadId);
    return Watch.fromServices({ sql, threads, eventStore, isVisible: isVisible(caller.config) })(
      input,
    );
  });

  return VoiceSeam.of({ hello, listThreads, readThread, launch, send, interrupt, respond, watch });
});

/** The seam reads receipts through its own store, which the route's context does not carry. */
export const layer = Layer.effect(VoiceSeam, make).pipe(
  Layer.provide(Layer.merge(CommandReceiptStore.layer, Digest.layerWebCrypto)),
);
