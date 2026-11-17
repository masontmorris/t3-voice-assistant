import { assert, it } from "@effect/vitest";
import {
  EventId,
  MessageId,
  type OrchestrationV2DomainEvent,
  type OrchestrationV2StoredEvent,
  type ProjectId,
  type ThreadId,
} from "@t3tools/contracts";
import * as DateTime from "effect/DateTime";
import * as Deferred from "effect/Deferred";
import * as Effect from "effect/Effect";
import * as Layer from "effect/Layer";
import * as Queue from "effect/Queue";
import * as Stream from "effect/Stream";
import * as SqlClient from "effect/sql/SqlClient";

import * as EventSink from "../orchestration-v2/EventSink.ts";
import * as EventStore from "../orchestration-v2/EventStore.ts";
import { LiveStreamBufferError } from "../orchestration-v2/LiveStreamBudget.ts";
import {
  OrchestratorDomainEventStreamError,
  OrchestratorProjectionError,
} from "../orchestration-v2/Orchestrator.ts";
import * as ProjectionStore from "../orchestration-v2/ProjectionStore.ts";
import * as OrchestrationEventStore from "../persistence/OrchestrationEventStore.ts";
import * as Sqlite from "../persistence/Sqlite.ts";
import { distill } from "./events.ts";
import { FIXTURES } from "./testFixtures.ts";
import * as Watch from "./watch.ts";

const layer = Layer.mergeAll(
  OrchestrationEventStore.layer,
  EventSink.layer.pipe(Layer.provideMerge(Layer.merge(EventStore.layer, ProjectionStore.layer))),
).pipe(Layer.provideMerge(Sqlite.layerMemory));

const PLAIN = FIXTURES.plainReply;
const threadId = PLAIN[0]!.event.threadId;

/** One commit, so a reader sees all of these events or none. */
const write = Effect.fn("test.write")(function* (
  events: ReadonlyArray<OrchestrationV2DomainEvent>,
) {
  const sink = yield* EventSink.EventSinkV2;
  return yield* sink.write({ events });
});

const writeFixture = (from: number, to?: number) =>
  write(PLAIN.slice(from, to).map((stored) => stored.event));

const assistantMessage = PLAIN.flatMap((stored) =>
  stored.event.type === "message.updated" && stored.event.payload.role === "assistant"
    ? [stored.event]
    : [],
)[0]!;

/** A reply nothing else in the fixture yields, written to mark a point in the live stream. */
const sentinelReply = (name: string, text = name): OrchestrationV2DomainEvent => ({
  ...assistantMessage,
  id: EventId.make(`sentinel-event:${name}`),
  payload: { ...assistantMessage.payload, id: MessageId.make(`sentinel:${name}`), text },
});

const lastRunUpdate = PLAIN.flatMap((stored) =>
  stored.event.type === "run.updated" ? [stored.event] : [],
).at(-1)!;

/** What the watch sends for these stored events once none of their needs waits any more. */
const signalsOf = (stored: ReadonlyArray<OrchestrationV2StoredEvent>): Array<Watch.WatchItem> =>
  stored.flatMap((event) => {
    const signal = distill(event)?.signal;
    return signal === undefined ||
      signal === null ||
      signal.type === "pending" ||
      signal.type === "secret_card"
      ? []
      : [signal];
  });

const pruneThrough = Effect.fn("test.pruneThrough")(function* (sequence: number) {
  const sql = yield* SqlClient.SqlClient;
  const now = DateTime.formatIso(yield* DateTime.now);
  yield* sql`
    INSERT INTO orchestration_v2_projection_metadata (
      projection_name, schema_version, last_sequence, updated_at
    ) VALUES (${OrchestrationEventStore.EVENT_LOG_PRUNED_THROUGH}, 2, ${sequence}, ${now})
    ON CONFLICT (projection_name) DO UPDATE SET last_sequence = excluded.last_sequence
  `;
});

type Threads = Watch.WatchSources["threads"];
type EventStoreSource = Watch.WatchSources["eventStore"];

const sources = Effect.fn("test.sources")(function* (overrides?: {
  readonly threads?: (real: Threads) => Threads;
  readonly eventStore?: (real: EventStoreSource) => EventStoreSource;
  readonly settle?: Watch.SettleCheck;
  readonly isProjectAllowed?: (projectId: ProjectId) => boolean;
}) {
  const sink = yield* EventSink.EventSinkV2;
  const projections = yield* ProjectionStore.ProjectionStoreV2;
  const eventStore = yield* OrchestrationEventStore.OrchestrationEventStore;
  // As OrchestratorV2 wraps the sink's stream and the projection's shell read.
  const threads: Threads = {
    streamStoredEventsFrom: (input) =>
      sink
        .stream({ ...input, bounded: true })
        .pipe(Stream.mapError((cause) => new OrchestratorDomainEventStreamError({ cause }))),
    getThreadShell: (id) =>
      projections
        .getThreadShell(id)
        .pipe(Effect.mapError((cause) => new OrchestratorProjectionError({ threadId: id, cause }))),
    getThreadRecords: (id, fields, filter) =>
      projections
        .getThreadRecords(id, fields, filter)
        .pipe(Effect.mapError((cause) => new OrchestratorProjectionError({ threadId: id, cause }))),
  };
  return {
    threads: overrides?.threads?.(threads) ?? threads,
    eventStore: overrides?.eventStore?.(eventStore) ?? eventStore,
    isProjectAllowed: overrides?.isProjectAllowed ?? (() => true),
    settle: overrides?.settle ?? Watch.settleNothing,
  } satisfies Watch.WatchSources;
});

/** Runs a watch into a queue so a test can interleave writes with what it has received. */
const follow = Effect.fn("test.follow")(function* (
  watchSources: Watch.WatchSources,
  afterSequence: number,
  watched: ThreadId = threadId,
) {
  const received = yield* Queue.unbounded<Watch.WatchItem>();
  yield* Watch.make(watchSources)({ threadId: watched, afterSequence }).pipe(
    Stream.runForEach((item) => Queue.offer(received, item)),
    Effect.forkScoped,
  );
  return {
    take: (count: number) =>
      Effect.forEach(Array.from({ length: count }), () => Queue.take(received)),
  };
});

const resync: Watch.WatchItem = { type: "watch_ended", threadId, reason: "resync" };

it.effect("replays from the cursor, then follows live with no gap and no duplicate", () =>
  Effect.scoped(
    Effect.gen(function* () {
      const replayed = yield* writeFixture(0, 20);
      const watch = yield* follow(yield* sources(), 0);
      const first = yield* watch.take(signalsOf(replayed).length);
      const live = yield* writeFixture(20);
      const rest = yield* watch.take(signalsOf(live).length);
      const sentinel = yield* write([sentinelReply("after-live")]);
      const [next] = yield* watch.take(1);
      assert.deepStrictEqual([...first, ...rest], signalsOf([...replayed, ...live]));
      assert.deepStrictEqual(next, signalsOf(sentinel)[0]);
    }),
  ).pipe(Effect.provide(layer)),
);

it.effect("resyncs before reading the replay when the cursor is below the prune floor", () =>
  Effect.gen(function* () {
    const stored = yield* writeFixture(0);
    yield* pruneThrough(stored[10]!.sequence);
    let replayReads = 0;
    const watchSources = yield* sources({
      eventStore: (real) => ({
        ...real,
        readAgentEvents: (input) => {
          replayReads += 1;
          return real.readAgentEvents(input);
        },
      }),
    });
    const items = yield* Watch.make(watchSources)({
      threadId,
      afterSequence: stored[5]!.sequence,
    }).pipe(Stream.runCollect);
    assert.deepStrictEqual(Array.from(items), [resync]);
    assert.strictEqual(replayReads, 0);
  }).pipe(Effect.provide(layer)),
);

it.effect("resyncs without emitting the replay when a prune lands during the replay read", () =>
  Effect.gen(function* () {
    const stored = yield* writeFixture(0);
    const sql = yield* SqlClient.SqlClient;
    const prune = pruneThrough(stored.at(-1)!.sequence).pipe(
      Effect.provideService(SqlClient.SqlClient, sql),
      Effect.orDie,
    );
    const watchSources = yield* sources({
      eventStore: (real) => ({
        ...real,
        readAgentEvents: (input) =>
          Stream.unwrap(prune.pipe(Effect.as(real.readAgentEvents(input)))),
      }),
    });
    const items = yield* Watch.make(watchSources)({ threadId, afterSequence: 0 }).pipe(
      Stream.runCollect,
    );
    assert.deepStrictEqual(Array.from(items), [resync]);
  }).pipe(Effect.provide(layer)),
);

it.effect("resyncs when the replay holds more than 1 MiB of stored payload", () =>
  Effect.gen(function* () {
    yield* writeFixture(0);
    yield* write([sentinelReply("oversized", "x".repeat(1_100_000))]);
    const items = yield* Watch.make(yield* sources())({ threadId, afterSequence: 0 }).pipe(
      Stream.runCollect,
    );
    assert.deepStrictEqual(Array.from(items), [resync]);
  }).pipe(Effect.provide(layer)),
);

it.effect("replays nothing from a cursor at the high-water, even below the prune floor", () =>
  Effect.scoped(
    Effect.gen(function* () {
      const stored = yield* writeFixture(0);
      yield* pruneThrough(stored.at(-1)!.sequence);
      let replayReads = 0;
      const highWaterRead = yield* Deferred.make<void>();
      const watchSources = yield* sources({
        eventStore: (real) => ({
          ...real,
          latestAgentSequence: (id) =>
            real
              .latestAgentSequence(id)
              .pipe(Effect.tap(() => Deferred.succeed(highWaterRead, undefined))),
          readAgentEvents: (input) => {
            replayReads += 1;
            return real.readAgentEvents(input);
          },
        }),
      });
      const watch = yield* follow(watchSources, stored.at(-1)!.sequence);
      yield* Deferred.await(highWaterRead);
      const sentinel = yield* write([sentinelReply("at-high-water")]);
      const [first] = yield* watch.take(1);
      assert.deepStrictEqual(first, signalsOf(sentinel)[0]);
      assert.strictEqual(replayReads, 0);
    }),
  ).pipe(Effect.provide(layer)),
);

it.effect("resubscribes from the last scanned event after a wrapped buffer error", () =>
  Effect.scoped(
    Effect.gen(function* () {
      const replayCursors: Array<number> = [];
      let subscriptions = 0;
      const watchSources = yield* sources({
        eventStore: (real) => ({
          ...real,
          readAgentEvents: (input) => {
            replayCursors.push(input?.afterSequence ?? 0);
            return real.readAgentEvents(input);
          },
        }),
        // The first live subscription delivers four events, then overflows.
        threads: (real) => ({
          ...real,
          streamStoredEventsFrom: (input) => {
            subscriptions += 1;
            const stream = real.streamStoredEventsFrom(input);
            return subscriptions > 1
              ? stream
              : stream.pipe(
                  Stream.take(4),
                  Stream.concat(
                    Stream.fail(
                      new OrchestratorDomainEventStreamError({
                        cause: new LiveStreamBufferError({
                          message: "The live event buffer is full.",
                        }),
                      }),
                    ),
                  ),
                );
          },
        }),
      });
      const before = yield* writeFixture(0, 20);
      const watch = yield* follow(watchSources, 0);
      const first = yield* watch.take(signalsOf(before).length);
      const during = yield* writeFixture(20, 34);
      const second = yield* watch.take(signalsOf(during).length);
      const after = yield* writeFixture(34);
      const third = yield* watch.take(signalsOf(after).length);
      // A second final frame of a message the first subscription already emitted.
      yield* write([{ ...assistantMessage, id: EventId.make("duplicate-final") }]);
      const sentinel = yield* write([sentinelReply("after-resubscribe")]);
      const [next] = yield* watch.take(1);

      assert.isTrue(
        during.slice(0, 4).some((stored) => distill(stored)?.signal?.type === "reply"),
        "the first subscription emitted the reply before it overflowed",
      );
      assert.deepStrictEqual(
        [...first, ...second, ...third, next],
        signalsOf([...before, ...during, ...after, ...sentinel]),
      );
      assert.strictEqual(subscriptions, 2);
      assert.deepStrictEqual(replayCursors, [0, during[3]!.sequence]);
    }),
  ).pipe(Effect.provide(layer)),
);

const created = PLAIN.flatMap((stored) =>
  stored.event.type === "thread.created" ? [stored.event] : [],
)[0]!;

it.effect.each(["missing", "deleted", "disallowed"] as const)(
  "ends with not_found before any replay for a %s thread",
  (kind) =>
    Effect.gen(function* () {
      if (kind !== "missing") yield* writeFixture(0);
      if (kind === "deleted") {
        yield* write([
          {
            ...created,
            id: EventId.make("thread-deleted"),
            type: "thread.deleted",
            payload: { ...created.payload, deletedAt: created.occurredAt },
          },
        ]);
      }
      let replayReads = 0;
      const watchSources = yield* sources({
        isProjectAllowed: () => kind !== "disallowed",
        eventStore: (real) => ({
          ...real,
          readAgentEvents: (input) => {
            replayReads += 1;
            return real.readAgentEvents(input);
          },
        }),
      });
      const items = yield* Watch.make(watchSources)({ threadId, afterSequence: 0 }).pipe(
        Stream.runCollect,
      );
      assert.deepStrictEqual(Array.from(items), [
        { type: "watch_ended", threadId, reason: "not_found" },
      ]);
      assert.strictEqual(replayReads, 0);
    }).pipe(Effect.provide(layer)),
);

/** A live source the test feeds one batch at a time; the replay and the shell stay real. */
const scriptedLive = Effect.fn("test.scriptedLive")(function* () {
  const batches = yield* Queue.unbounded<ReadonlyArray<OrchestrationV2StoredEvent>>();
  return {
    push: (events: ReadonlyArray<OrchestrationV2StoredEvent>) => Queue.offer(batches, events),
    threads: (real: Threads): Threads => ({
      ...real,
      streamStoredEventsFrom: () =>
        Stream.fromQueue(batches).pipe(Stream.flatMap((events) => Stream.fromIterable(events))),
    }),
  };
});

/** A settle check that answers from a script and hands each trigger to the test. */
const scriptedChecks = Effect.fn("test.scriptedChecks")(function* (
  answer: (trigger: Watch.SettleTrigger, call: number) => Effect.Effect<Watch.SettleAnswer>,
) {
  const triggers = yield* Queue.unbounded<Watch.SettleTrigger>();
  let calls = 0;
  const settle: Watch.SettleCheck = (trigger) =>
    Queue.offer(triggers, trigger).pipe(
      Effect.andThen(Effect.suspend(() => answer(trigger, ++calls))),
    );
  return { settle, next: Queue.take(triggers) };
});

const lastTrigger = (stored: ReadonlyArray<OrchestrationV2StoredEvent>) =>
  stored.flatMap((event) => (distill(event)?.settleCheck ? [event.sequence] : [])).at(-1);

const doneFor = (trigger: Watch.SettleTrigger, asOfSequence: number): Watch.WatchDone => ({
  type: "done",
  threadId,
  runId: lastRunUpdate.payload.id,
  status: "completed",
  result: { text: "first fixture turn complete", messageId: null, turnItemId: null },
  sequence: trigger.sequence,
  asOfSequence,
});

const nothing: Watch.SettleAnswer = { kind: "nothing" };

it.effect(
  "runs one settle check per batch, the replay's at its high-water, with the shell's project",
  () =>
    Effect.scoped(
      Effect.gen(function* () {
        const replayed = yield* writeFixture(0, 20);
        const live = yield* scriptedLive();
        const checks = yield* scriptedChecks(() => Effect.succeed(nothing));
        yield* follow(yield* sources({ threads: live.threads, settle: checks.settle }), 0);
        const afterReplay = yield* checks.next;

        const batch = yield* writeFixture(20, 30);
        yield* live.push(batch);
        const afterBatch = yield* checks.next;

        // A batch with no trigger runs no check, so the next check is the following batch's.
        yield* live.push(yield* write([sentinelReply("no-trigger")]));
        const last = yield* writeFixture(30);
        yield* live.push(last);
        const afterLast = yield* checks.next;

        const shell = yield* (yield* ProjectionStore.ProjectionStoreV2).getThreadShell(threadId);
        assert.deepStrictEqual(
          [afterReplay, afterBatch, afterLast].map((trigger) => trigger.sequence),
          [replayed.at(-1)!.sequence, lastTrigger(batch), lastTrigger(last)],
        );
        for (const trigger of [afterReplay, afterBatch, afterLast]) {
          assert.strictEqual(trigger.projectId, shell?.projectId);
        }
      }),
    ).pipe(Effect.provide(layer)),
);

it.effect(
  "holds a done until the watch has emitted every stored event through its asOfSequence",
  () =>
    Effect.scoped(
      Effect.gen(function* () {
        const replayed = yield* writeFixture(0, 20);
        // The first check's read saw three events the watch has not emitted yet.
        const ahead = replayed.at(-1)!.sequence + 3;
        const live = yield* scriptedLive();
        const checks = yield* scriptedChecks((trigger, call) =>
          Effect.succeed<Watch.SettleAnswer>({
            kind: "done",
            done: doneFor(trigger, call === 1 ? ahead : trigger.sequence),
          }),
        );
        const watch = yield* follow(
          yield* sources({ threads: live.threads, settle: checks.settle }),
          0,
        );
        yield* watch.take(signalsOf(replayed).length);
        yield* checks.next;

        const batch = yield* writeFixture(20, 30);
        assert.isAtLeast(batch.at(-1)!.sequence, ahead);
        yield* live.push(batch);
        const items = yield* watch.take(signalsOf(batch).length + 1);
        const second = yield* checks.next;

        assert.deepStrictEqual(items, [...signalsOf(batch), doneFor(second, second.sequence)]);
        assert.strictEqual(second.sequence, lastTrigger(batch));
      }),
    ).pipe(Effect.provide(layer)),
);

it.effect("runs a catch-up check once the watch reaches its sequence, with no new trigger", () =>
  Effect.scoped(
    Effect.gen(function* () {
      const replayed = yield* writeFixture(0);
      const target = replayed.at(-1)!.sequence + 2;
      const live = yield* scriptedLive();
      const checks = yield* scriptedChecks((trigger, call) =>
        Effect.succeed<Watch.SettleAnswer>(
          call === 1
            ? { kind: "catch_up", atSequence: target }
            : { kind: "done", done: doneFor(trigger, target) },
        ),
      );
      const watch = yield* follow(
        yield* sources({ threads: live.threads, settle: checks.settle }),
        0,
      );
      yield* watch.take(signalsOf(replayed).length);
      const first = yield* checks.next;

      const before = yield* write([sentinelReply("before-target")]);
      yield* live.push(before);
      const atTarget = yield* write([sentinelReply("at-target")]);
      assert.strictEqual(atTarget[0]!.sequence, target);
      yield* live.push(atTarget);
      const items = yield* watch.take(3);
      const second = yield* checks.next;

      assert.deepStrictEqual(second, first);
      assert.deepStrictEqual(items, [
        ...signalsOf(before),
        ...signalsOf(atTarget),
        doneFor(first, target),
      ]);
    }),
  ).pipe(Effect.provide(layer)),
);

it.effect("applies a follow-up wait's answer on the watch fiber and keeps one wait at a time", () =>
  Effect.scoped(
    Effect.gen(function* () {
      const replayed = yield* writeFixture(0);
      const release = yield* Deferred.make<void>();
      let secondWaitRan = false;
      const live = yield* scriptedLive();
      const checks = yield* scriptedChecks((trigger, call) =>
        Effect.succeed<Watch.SettleAnswer>({
          kind: "follow_up",
          wait:
            call === 1
              ? Deferred.await(release).pipe(
                  Effect.as<Watch.SettleAnswer>({
                    kind: "done",
                    done: doneFor(trigger, trigger.sequence),
                  }),
                )
              : Effect.sync(() => {
                  secondWaitRan = true;
                  return nothing;
                }),
        }),
      );
      const watch = yield* follow(
        yield* sources({ threads: live.threads, settle: checks.settle }),
        0,
      );
      yield* watch.take(signalsOf(replayed).length);
      const first = yield* checks.next;

      // The wait does not hold the watch: a later batch still arrives, and its check runs.
      const later = yield* write([{ ...lastRunUpdate, id: EventId.make("later-run-update") }]);
      yield* live.push(later);
      const [laterSignal] = yield* watch.take(1);
      yield* checks.next;
      yield* Deferred.succeed(release, undefined);
      const [done] = yield* watch.take(1);

      assert.deepStrictEqual(laterSignal, signalsOf(later)[0]);
      assert.deepStrictEqual(done, doneFor(first, first.sequence));
      assert.isFalse(secondWaitRan);
    }),
  ).pipe(Effect.provide(layer)),
);

it.effect("ends with gone when a settle check finds the thread gone", () =>
  Effect.gen(function* () {
    const replayed = yield* writeFixture(0);
    const watchSources = yield* sources({ settle: () => Effect.succeed({ kind: "gone" }) });
    const items = yield* Watch.make(watchSources)({ threadId, afterSequence: 0 }).pipe(
      Stream.runCollect,
    );
    assert.deepStrictEqual(Array.from(items), [
      ...signalsOf(replayed),
      { type: "watch_ended", threadId, reason: "gone" },
    ]);
  }).pipe(Effect.provide(layer)),
);

it.effect(
  "checks once when a watch starts at the high-water of a thread that already settled",
  () =>
    Effect.scoped(
      Effect.gen(function* () {
        const stored = yield* writeFixture(0);
        const live = yield* scriptedLive();
        const checks = yield* scriptedChecks(() => Effect.succeed(nothing));
        yield* follow(
          yield* sources({ threads: live.threads, settle: checks.settle }),
          stored.at(-1)!.sequence,
        );
        const atStart = yield* checks.next;

        // A quiet batch runs no check, so the next check is the trigger's.
        yield* live.push(yield* write([sentinelReply("quiet")]));
        const later = yield* write([{ ...lastRunUpdate, id: EventId.make("run-update-later") }]);
        yield* live.push(later);
        const next = yield* checks.next;

        assert.deepStrictEqual(
          [atStart.sequence, next.sequence],
          [stored.at(-1)!.sequence, later[0]!.sequence],
        );
      }),
    ).pipe(Effect.provide(layer)),
);

it.effect("checks once after the replay of a resubscribe that follows a buffer error", () =>
  Effect.scoped(
    Effect.gen(function* () {
      const replayed = yield* writeFixture(0, 20);
      const overflow = yield* Deferred.make<void>();
      const live = yield* scriptedLive();
      let subscriptions = 0;
      const checks = yield* scriptedChecks(() => Effect.succeed(nothing));
      const watchSources = yield* sources({
        settle: checks.settle,
        threads: (real) => {
          const scripted = live.threads(real);
          return {
            ...scripted,
            streamStoredEventsFrom: (input) => {
              subscriptions += 1;
              return subscriptions > 1
                ? scripted.streamStoredEventsFrom(input)
                : Stream.fromEffect(Deferred.await(overflow)).pipe(
                    Stream.flatMap(() =>
                      Stream.fail(
                        new OrchestratorDomainEventStreamError({
                          cause: new LiveStreamBufferError({ message: "full" }),
                        }),
                      ),
                    ),
                  );
            },
          };
        },
      });
      yield* follow(watchSources, 0);
      const first = yield* checks.next;

      const missed = yield* writeFixture(20, 30);
      yield* Deferred.succeed(overflow, undefined);
      const afterResubscribe = yield* checks.next;
      const last = yield* writeFixture(30);
      yield* live.push(last);
      const next = yield* checks.next;

      assert.deepStrictEqual(
        [first.sequence, afterResubscribe.sequence, next.sequence],
        [replayed.at(-1)!.sequence, missed.at(-1)!.sequence, lastTrigger(last)],
      );
      assert.strictEqual(subscriptions, 2);
    }),
  ).pipe(Effect.provide(layer)),
);

it.effect("does not send a secret card that was recorded after its run ended", () =>
  Effect.scoped(
    Effect.gen(function* () {
      const late = FIXTURES.lateSecretCard;
      const stored = yield* write(late.map((event) => event.event));
      const card = distill(stored.at(-1)!)?.signal;
      assert.strictEqual(card?.type, "secret_card");
      const watch = yield* follow(yield* sources(), 0, late[0]!.event.threadId);
      const replayed = yield* watch.take(signalsOf(stored).length);
      const runUpdate = late
        .flatMap((event) => (event.event.type === "run.updated" ? [event.event] : []))
        .at(-1)!;
      const after = yield* write([{ ...runUpdate, id: EventId.make("run-update-after-card") }]);
      const [next] = yield* watch.take(1);

      const cardId = card?.type === "secret_card" ? card.requestId : null;
      assert.isFalse(
        replayed.some((item) => item.type === "needs_you" && item.requestId === cardId),
      );
      assert.deepStrictEqual(next, signalsOf(after)[0]);
    }),
  ).pipe(Effect.provide(layer)),
);

it.effect("checks once after a replay that holds no trigger", () =>
  Effect.scoped(
    Effect.gen(function* () {
      const stored = yield* writeFixture(0);
      const quiet = yield* write([sentinelReply("quiet-replay")]);
      assert.isFalse(distill(quiet[0]!)?.settleCheck ?? false);
      const live = yield* scriptedLive();
      const checks = yield* scriptedChecks(() => Effect.succeed(nothing));
      const watch = yield* follow(
        yield* sources({ threads: live.threads, settle: checks.settle }),
        stored.at(-1)!.sequence,
      );
      const [replayed] = yield* watch.take(1);
      const afterReplay = yield* checks.next;
      const later = yield* write([
        { ...lastRunUpdate, id: EventId.make("run-update-after-quiet") },
      ]);
      yield* live.push(later);
      const next = yield* checks.next;

      assert.deepStrictEqual(replayed, signalsOf(quiet)[0]);
      assert.deepStrictEqual(
        [afterReplay.sequence, next.sequence],
        [quiet[0]!.sequence, later[0]!.sequence],
      );
    }),
  ).pipe(Effect.provide(layer)),
);

/** A reply nothing in `fixture` yields, in `fixture`'s thread, and its message id. */
const fixtureSentinel = (fixture: ReadonlyArray<OrchestrationV2StoredEvent>, name: string) => {
  const message = fixture.flatMap((stored) =>
    stored.event.type === "message.updated" && stored.event.payload.role === "assistant"
      ? [stored.event]
      : [],
  )[0]!;
  const messageId = MessageId.make(`sentinel:${name}`);
  const event: OrchestrationV2DomainEvent = {
    ...message,
    id: EventId.make(`sentinel-event:${name}`),
    payload: { ...message.payload, id: messageId, text: name },
  };
  return { event, messageId };
};

/** Everything a watch from the cursor sends for what is stored now, up to a reply written after it. */
const sentThrough = Effect.fn("test.sentThrough")(function* (
  fixture: ReadonlyArray<OrchestrationV2StoredEvent>,
  afterSequence = 0,
) {
  const sentinel = fixtureSentinel(fixture, "end-of-replay");
  yield* write([sentinel.event]);
  const items = yield* Watch.make(yield* sources())({
    threadId: fixture[0]!.event.threadId,
    afterSequence,
  }).pipe(
    Stream.takeUntil((item) => item.type === "reply" && item.messageId === sentinel.messageId),
    Stream.runCollect,
  );
  return Array.from(items).slice(0, -1);
});

const indexOf = (
  fixture: ReadonlyArray<OrchestrationV2StoredEvent>,
  matches: (event: OrchestrationV2DomainEvent) => boolean,
) => fixture.findIndex((stored) => matches(stored.event));

const needsOf = (items: ReadonlyArray<Watch.WatchItem>) =>
  items.filter((item) => item.type === "needs_you");

it.effect(
  "sends needs_you with the turn item's prompt for a replayed request that still waits",
  () =>
    Effect.scoped(
      Effect.gen(function* () {
        const approval = FIXTURES.approval;
        const promptAt = indexOf(
          approval,
          (event) =>
            event.type === "turn-item.updated" && event.payload.type === "approval_request",
        );
        const stored = yield* write(approval.slice(0, promptAt + 1).map((event) => event.event));
        const asked = stored.find(
          (event) =>
            event.event.type === "runtime-request.updated" &&
            event.event.payload.status === "pending",
        )!;
        const prompt = stored[promptAt]!.event;

        const needs = needsOf(yield* sentThrough(approval));
        assert.deepStrictEqual(needs, [
          {
            type: "needs_you",
            threadId: asked.event.threadId,
            sequence: asked.sequence,
            requestId: asked.event.type === "runtime-request.updated" ? asked.event.payload.id : "",
            kind: "approval",
            prompt:
              prompt.type === "turn-item.updated" && prompt.payload.type === "approval_request"
                ? prompt.payload.prompt!
                : "",
          },
        ]);
      }),
    ).pipe(Effect.provide(layer)),
);

it.effect("sends no needs_you for a replay that holds a request's pending event and its end", () =>
  Effect.scoped(
    Effect.gen(function* () {
      yield* write(FIXTURES.approval.map((event) => event.event));
      const items = yield* sentThrough(FIXTURES.approval);
      assert.deepStrictEqual(needsOf(items), []);
      assert.isTrue(items.some((item) => item.type === "resolved"));
    }),
  ).pipe(Effect.provide(layer)),
);

it.effect("sends no needs_you for a replay that holds a card's waiting event and its answer", () =>
  Effect.scoped(
    Effect.gen(function* () {
      yield* write(FIXTURES.linkCard.map((event) => event.event));
      assert.deepStrictEqual(needsOf(yield* sentThrough(FIXTURES.linkCard)), []);
    }),
  ).pipe(Effect.provide(layer)),
);

/** The link-card fixture until just before Mason answers the card, with its run complete. */
const LINK_UNANSWERED = FIXTURES.linkCard.slice(
  0,
  indexOf(
    FIXTURES.linkCard,
    (event) =>
      event.type === "turn-item.updated" &&
      event.payload.type === "link_request" &&
      event.payload.status !== "waiting",
  ),
);

/** A message Mason writes in the link-card thread one second after the card asked. */
const userWritesAfterCard = (): OrchestrationV2DomainEvent => {
  const message = FIXTURES.linkCard.flatMap((stored) =>
    stored.event.type === "message.updated" && stored.event.payload.role === "user"
      ? [stored.event]
      : [],
  )[0]!;
  const later = DateTime.add(message.payload.updatedAt, { seconds: 1 });
  return {
    ...message,
    id: EventId.make("user-writes-after-card"),
    payload: {
      ...message.payload,
      id: MessageId.make("user-writes-after-card"),
      text: "Never mind the link, run them here.",
      createdBy: "user",
      createdAt: later,
      updatedAt: later,
    },
  };
};

it.effect.each([
  { case: "only the card", userWrites: false },
  { case: "a user-authored message after the card", userWrites: true },
])("replays the unanswered link card after $case", ({ userWrites }) =>
  Effect.scoped(
    Effect.gen(function* () {
      const stored = yield* write(LINK_UNANSWERED.map((event) => event.event));
      if (userWrites) yield* write([userWritesAfterCard()]);
      const card = stored.find(
        (event) =>
          event.event.type === "turn-item.updated" && event.event.payload.type === "link_request",
      )!;

      const needs = needsOf(yield* sentThrough(FIXTURES.linkCard));
      assert.deepStrictEqual(
        needs,
        userWrites
          ? []
          : [
              {
                type: "needs_you",
                threadId: card.event.threadId,
                sequence: card.sequence,
                requestId: card.event.type === "turn-item.updated" ? card.event.payload.id : "",
                kind: "in_app",
                prompt: "It needs something from you in the app.",
              },
            ],
      );
    }),
  ).pipe(Effect.provide(layer)),
);
