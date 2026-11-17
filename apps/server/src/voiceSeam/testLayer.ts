import { type OrchestrationV2DomainEvent, ThreadId } from "@t3tools/contracts";
import { delegatedTaskProgress } from "@t3tools/provider-core/server/subagentProjection";
import * as Effect from "effect/Effect";
import * as Layer from "effect/Layer";
import * as Queue from "effect/Queue";
import * as SqlClient from "effect/sql/SqlClient";
import * as Stream from "effect/Stream";
import * as TestClock from "effect/testing/TestClock";

import * as ServerSettings from "../serverSettings.ts";
import * as CommandReceiptStore from "../orchestration-v2/CommandReceiptStore.ts";
import * as EventSink from "../orchestration-v2/EventSink.ts";
import * as EventStore from "../orchestration-v2/EventStore.ts";
import * as Orchestrator from "../orchestration-v2/Orchestrator.ts";
import * as ProjectionStore from "../orchestration-v2/ProjectionStore.ts";
import * as ProviderSessionManager from "../orchestration-v2/ProviderSessionManager.ts";
import * as ThreadManagementService from "../orchestration-v2/ThreadManagementService.ts";
import * as OrchestrationEventStore from "../persistence/OrchestrationEventStore.ts";
import * as Sqlite from "../persistence/Sqlite.ts";
import { makeSettleCheck } from "./outcome.ts";
import * as Watch from "./watch.ts";

const stores = Layer.mergeAll(
  OrchestrationEventStore.layer,
  EventSink.layer.pipe(Layer.provideMerge(Layer.merge(EventStore.layer, ProjectionStore.layer))),
).pipe(Layer.provideMerge(Sqlite.layerMemory));

/**
 * The orchestrator reads the seam reaches through ThreadManagementService, over the real stores, as
 * OrchestratorV2 wraps them. These threads never restart, so no continuation is ever pending.
 */
const storeOrchestrator = Layer.unwrap(
  Effect.gen(function* () {
    const sink = yield* EventSink.EventSinkV2;
    const projections = yield* ProjectionStore.ProjectionStoreV2;
    const projectionError =
      (threadId: Parameters<typeof projections.getThreadShell>[0]) => (cause: unknown) =>
        new Orchestrator.OrchestratorProjectionError({ threadId, cause });
    // OrchestratorV2 names no thread for a shell read across threads.
    const SHELL = ThreadId.make("thread:shell");
    return Layer.mock(Orchestrator.OrchestratorV2)({
      getThreadEventSequence: (threadId) =>
        sink.latestSequence({ threadId }).pipe(Effect.mapError(projectionError(threadId))),
      getThreadRecords: (threadId, fields, filter) =>
        projections
          .getThreadRecords(threadId, fields, filter)
          .pipe(Effect.mapError(projectionError(threadId))),
      getThreadShell: (threadId) =>
        projections.getThreadShell(threadId).pipe(Effect.mapError(projectionError(threadId))),
      listActiveShellIndex: () =>
        projections.listActiveShellIndex().pipe(Effect.mapError(projectionError(SHELL))),
      readShellSnapshot: (options) =>
        projections
          .readShellSnapshot(options)
          .pipe(
            Effect.mapError(projectionError(SHELL)),
            Effect.map(Effect.mapError(projectionError(SHELL))),
          ),
      streamStoredEventsFrom: (input) =>
        sink
          .stream({ ...input, bounded: true })
          .pipe(
            Stream.mapError(
              (cause) => new Orchestrator.OrchestratorDomainEventStreamError({ cause }),
            ),
          ),
      restartContinuationPending: () => Effect.succeed(false),
      awaitRestartContinuation: () => Effect.never,
      // OrchestratorV2's rule for a thread's own delegated work, without restart continuations.
      delegatedTaskResultPending: (threadId) =>
        projections
          .getThreadRecords(threadId, ["runs", "messages", "subagents", "providerThreads"], {
            messageRoles: ["user"],
          })
          .pipe(
            Effect.map((child) => delegatedTaskProgress(child).state !== "result_available"),
            Effect.mapError(projectionError(threadId)),
          ),
    });
  }),
);

/** The real stores, with the real ThreadManagementService reading them. */
export const seamLayer = ThreadManagementService.layer.pipe(
  Layer.provide(Layer.mock(ProviderSessionManager.ProviderSessionManagerV2)({})),
  Layer.provide(storeOrchestrator),
  Layer.provide(ServerSettings.layerTest()),
  Layer.provide(CommandReceiptStore.layer.pipe(Layer.provide(Sqlite.layerMemory))),
  Layer.provideMerge(stores),
);

/** One commit, so a reader sees all of these events or none. */
export const write = Effect.fn("test.write")(function* (
  events: ReadonlyArray<OrchestrationV2DomainEvent>,
) {
  const sink = yield* EventSink.EventSinkV2;
  return yield* sink.write({ events });
});

/**
 * The settle check's 1 ms `waitForThread` is a check against the store, not a wait the test drives,
 * so it runs on the live clock. Longer waits stay on the TestClock and end only on a stored event.
 */
export const checksOnLiveClock = (
  threads: ThreadManagementService.ThreadManagementService["Service"],
): ThreadManagementService.ThreadManagementService["Service"] => ({
  ...threads,
  waitForThread: (input) =>
    input.timeoutMs <= 1
      ? TestClock.withLive(threads.waitForThread(input))
      : threads.waitForThread(input),
});

type Threads = ThreadManagementService.ThreadManagementService["Service"];

export interface Checked {
  readonly trigger: Watch.SettleTrigger;
  readonly answer: Watch.SettleAnswer;
}

/**
 * A watch with the real settle read, which hands the test each check's answer, follow-ups
 * included, and can run a step before each check.
 */
export const realWatch = Effect.fn("test.realWatch")(function* (
  input: Watch.WatchInput,
  options?: {
    readonly threads?: (real: Threads) => Threads;
    readonly beforeCheck?: (trigger: Watch.SettleTrigger) => Effect.Effect<void>;
  },
) {
  const real = checksOnLiveClock(yield* ThreadManagementService.ThreadManagementService);
  const threads = options?.threads?.(real) ?? real;
  const checked = yield* Queue.unbounded<Checked>();
  const items = yield* Queue.unbounded<Watch.WatchItem>();
  const check = makeSettleCheck({ sql: yield* SqlClient.SqlClient, threads });
  const observed = (
    trigger: Watch.SettleTrigger,
    answer: Watch.SettleAnswer,
  ): Watch.SettleAnswer =>
    answer.kind === "follow_up"
      ? {
          kind: "follow_up",
          wait: answer.wait.pipe(
            Effect.map((next) => observed(trigger, next)),
            Effect.tap((next) => Queue.offer(checked, { trigger, answer: next })),
          ),
        }
      : answer;
  const settle: Watch.SettleCheck = (trigger) =>
    (options?.beforeCheck?.(trigger) ?? Effect.void).pipe(
      Effect.andThen(check(trigger)),
      Effect.map((answer) => observed(trigger, answer)),
      Effect.tap((answer) => Queue.offer(checked, { trigger, answer })),
    );
  yield* Watch.make({
    threads,
    eventStore: yield* OrchestrationEventStore.OrchestrationEventStore,
    isProjectAllowed: () => true,
    settle,
  })(input).pipe(
    Stream.runForEach((item) => Queue.offer(items, item)),
    Effect.forkScoped,
  );
  /** Takes items through the first that matches. */
  const until = Effect.fn("test.realWatch.until")(function* (
    matches: (item: Watch.WatchItem) => boolean,
  ) {
    const taken: Array<Watch.WatchItem> = [];
    while (true) {
      const item = yield* Queue.take(items);
      taken.push(item);
      if (matches(item)) return taken;
    }
  });
  return { nextCheck: Queue.take(checked), until };
});

/** Writes in one commit from inside a check, where the test's services are out of scope. */
export const writer = Effect.gen(function* () {
  const sink = yield* EventSink.EventSinkV2;
  return (events: ReadonlyArray<OrchestrationV2DomainEvent>) =>
    sink.write({ events }).pipe(Effect.orDie);
});
