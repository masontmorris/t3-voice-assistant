import { assert, it } from "@effect/vitest";
import {
  EventId,
  MessageId,
  type OrchestrationV2DomainEvent,
  type OrchestrationV2StoredEvent,
  type RunId,
} from "@t3tools/contracts";
import * as Effect from "effect/Effect";
import * as SqlClient from "effect/sql/SqlClient";
import * as Stream from "effect/Stream";

import * as ThreadManagementService from "../orchestration-v2/ThreadManagementService.ts";
import * as OrchestrationEventStore from "../persistence/OrchestrationEventStore.ts";
import { cutAtSentence } from "./outcome.ts";
import { checksOnLiveClock, realWatch, seamLayer, write, writer } from "./testLayer.ts";
import { FIXTURES } from "./testFixtures.ts";
import * as Watch from "./watch.ts";

const runUpdates = (events: ReadonlyArray<OrchestrationV2StoredEvent>) =>
  events.flatMap((stored) => (stored.event.type === "run.updated" ? [stored.event] : []));

const runOf = (events: ReadonlyArray<OrchestrationV2StoredEvent>, ordinal: number): RunId =>
  runUpdates(events).find((event) => event.payload.ordinal === ordinal)!.payload.id;

const indexOf = (
  events: ReadonlyArray<OrchestrationV2StoredEvent>,
  matches: (event: OrchestrationV2DomainEvent) => boolean,
) => events.findIndex((stored) => matches(stored.event));

const isDone = (item: Watch.WatchItem) => item.type === "done";

/** The production watch, run to its first `done` or its end. */
const firstDone = Effect.fn("test.firstDone")(function* (input: Watch.WatchInput) {
  const watch = Watch.fromServices({
    sql: yield* SqlClient.SqlClient,
    threads: checksOnLiveClock(yield* ThreadManagementService.ThreadManagementService),
    eventStore: yield* OrchestrationEventStore.OrchestrationEventStore,
    isVisible: () => true,
  });
  return Array.from(
    yield* watch(input).pipe(
      Stream.takeUntil((item) => item.type === "done" || item.type === "watch_ended"),
      Stream.runCollect,
    ),
  );
});

it.effect("sends done for a settled thread with T3's own result for its run", () =>
  Effect.gen(function* () {
    const plain = FIXTURES.plainReply;
    const stored = yield* write(plain.map((event) => event.event));
    const items = yield* firstDone({ threadId: plain[0]!.event.threadId, afterSequence: 0 });
    const message = stored.find(
      (event) => event.event.type === "message.updated" && event.event.payload.role === "assistant",
    )!;
    assert.deepStrictEqual(items.at(-1), {
      type: "done",
      threadId: plain[0]!.event.threadId,
      runId: runOf(plain, 1),
      status: "completed",
      result: {
        text: "first fixture turn complete",
        messageId: message.event.type === "message.updated" ? message.event.payload.id : null,
        turnItemId: (items.at(-1) as Watch.WatchDone).result.turnItemId,
      },
      sequence: stored.at(-1)!.sequence,
      asOfSequence: stored.at(-1)!.sequence,
    });
  }).pipe(Effect.provide(seamLayer)),
);

it.effect("sends no done for an old run's terminal event replayed while a newer run works", () =>
  Effect.scoped(
    Effect.gen(function* () {
      const fixture = FIXTURES.messageQuestion;
      const threadId = fixture[0]!.event.threadId;
      const secondRuns = indexOf(
        fixture,
        (event) =>
          event.type === "run.updated" &&
          event.payload.ordinal === 2 &&
          event.payload.status === "running",
      );
      const before = yield* write(fixture.slice(0, secondRuns + 1).map((event) => event.event));
      const watch = yield* realWatch({ threadId, afterSequence: 0 });

      const atReplay = yield* watch.nextCheck;
      const replayed = yield* watch.until(
        (item) => item.type === "run" && item.sequence === before.at(-1)!.sequence,
      );
      yield* write(fixture.slice(secondRuns + 1).map((event) => event.event));
      const live = yield* watch.until(isDone);
      const done = live.at(-1) as Watch.WatchDone;

      assert.isTrue(
        replayed.some(
          (item) =>
            item.type === "run" && item.runId === runOf(fixture, 1) && item.status === "completed",
        ),
        "the replay holds the first run's terminal event",
      );
      assert.deepStrictEqual(atReplay.answer, { kind: "nothing" });
      assert.isFalse(replayed.some(isDone));
      assert.deepInclude(done, { runId: runOf(fixture, 2), status: "completed" });
      assert.strictEqual(done.result.text, "second fixture turn complete");
    }),
  ).pipe(Effect.provide(seamLayer)),
);

it.effect.each([
  {
    case: "anchored on the root run, whose own answer it reports",
    anchor: 1,
    reported: 1,
    text: "ROOT_STARTED",
  },
  {
    case: "with no anchor, naming the result run",
    anchor: null,
    reported: 2,
    text: "ROOT_WAKE_DONE",
  },
] as const)(
  "sends a parent's done only after its subagent settles, $case",
  ({ anchor, reported, text }) =>
    Effect.scoped(
      Effect.gen(function* () {
        const fixture = FIXTURES.subagentAfterRoot;
        const threadId = fixture[0]!.event.threadId;
        // A run reports completed when its provider turn ends and again after its checkpoint; it ends at the last.
        const rootEnds = fixture.findLastIndex(
          ({ event }) =>
            event.type === "run.updated" &&
            event.payload.ordinal === 1 &&
            event.payload.status === "completed",
        );
        const before = yield* write(fixture.slice(0, rootEnds + 1).map((event) => event.event));
        const watch = yield* realWatch({
          threadId,
          afterSequence: 0,
          ...(anchor === null ? {} : { runId: runOf(fixture, anchor) }),
        });
        const atReplay = yield* watch.nextCheck;
        const replayed = yield* watch.until(
          (item) => item.type === "run" && item.sequence === before.at(-1)!.sequence,
        );
        const after = yield* write(fixture.slice(rootEnds + 1).map((event) => event.event));
        const live = yield* watch.until(isDone);
        const done = live.at(-1) as Watch.WatchDone;
        const subagentSettled = before.findLast(
          (stored) =>
            stored.event.type === "subagent.updated" && stored.event.payload.status === "completed",
        );

        assert.isDefined(subagentSettled, "the subagent settles before the root run ends");
        assert.deepStrictEqual(atReplay.answer, { kind: "nothing" });
        assert.isFalse(replayed.some(isDone));
        assert.deepInclude(done, { runId: runOf(fixture, reported), status: "completed" });
        assert.strictEqual(done.result.text, text);
        assert.strictEqual(
          done.sequence,
          after.findLast(
            (stored) =>
              stored.event.type === "run.updated" && stored.event.payload.status === "completed",
          )!.sequence,
        );
      }),
    ).pipe(Effect.provide(seamLayer)),
);

it.effect.each([
  { case: "anchored on run 1, which said nothing", anchor: 1, reported: 1, text: "" },
  { case: "anchored on run 2", anchor: 2, reported: 2, text: "second fixture turn complete" },
  { case: "with no anchor", anchor: null, reported: 2, text: "second fixture turn complete" },
] as const)(
  "reports the anchor's own outcome, never the result run's text: $case",
  ({ anchor, reported, text }) =>
    Effect.gen(function* () {
      const fixture = FIXTURES.messageQuestion;
      yield* write(fixture.map((event) => event.event));
      const done = (yield* firstDone({
        threadId: fixture[0]!.event.threadId,
        afterSequence: 0,
        ...(anchor === null ? {} : { runId: runOf(fixture, anchor) }),
      })).at(-1) as Watch.WatchDone;

      assert.deepInclude(done, { runId: runOf(fixture, reported), status: "completed" });
      assert.strictEqual(done.result.text, text);
      assert.strictEqual(done.result.messageId === null, text === "");
      assert.strictEqual(done.result.turnItemId === null, text === "");
    }).pipe(Effect.provide(seamLayer)),
);

it.effect("sends done with its status and empty text for a run that stored no answer", () =>
  Effect.gen(function* () {
    // Run 1 asks a question and says nothing. The cut ends where the question resolves.
    const resolved = FIXTURES.messageQuestion.findIndex(
      ({ event }) =>
        event.type === "runtime-request.updated" && event.payload.status === "resolved",
    );
    const fixture = FIXTURES.messageQuestion.slice(0, resolved + 1);
    yield* write(fixture.map((event) => event.event));
    const done = (yield* firstDone({
      threadId: fixture[0]!.event.threadId,
      afterSequence: 0,
    })).at(-1) as Watch.WatchDone;

    assert.deepInclude(done, { runId: runOf(fixture, 1), status: "completed" });
    assert.deepStrictEqual(done.result, { text: "", messageId: null, turnItemId: null });
  }).pipe(Effect.provide(seamLayer)),
);

it.effect("reports a failed run's error text as its result", () =>
  Effect.gen(function* () {
    const fixture = FIXTURES.failedRun;
    yield* write(fixture.map((event) => event.event));
    const error = fixture.flatMap((stored) =>
      stored.event.type === "turn-item.updated" && stored.event.payload.type === "error"
        ? [stored.event.payload]
        : [],
    )[0]!;
    const done = (yield* firstDone({
      threadId: fixture[0]!.event.threadId,
      afterSequence: 0,
    })).at(-1) as Watch.WatchDone;

    assert.deepInclude(done, { runId: runOf(fixture, 1), status: "failed" });
    assert.deepStrictEqual(
      { ...done.result, text: done.result.text.split("\n")[0] },
      {
        text: error.type === "error" ? error.failure.message : "",
        messageId: null,
        turnItemId: error.id,
      },
    );
  }).pipe(Effect.provide(seamLayer)),
);

it.effect.each([
  { case: "anchored on the rolled-back run", anchor: 2, reported: 2, status: "rolled_back" },
  { case: "with no anchor", anchor: null, reported: 1, status: "completed" },
] as const)(
  "reports a thread whose last run was rolled back, $case",
  ({ anchor, reported, status }) =>
    Effect.gen(function* () {
      const fixture = FIXTURES.rolledBack;
      yield* write(fixture.map((event) => event.event));
      const done = (yield* firstDone({
        threadId: fixture[0]!.event.threadId,
        afterSequence: 0,
        ...(anchor === null ? {} : { runId: runOf(fixture, anchor) }),
      })).at(-1);

      assert.deepInclude(done, { type: "done", runId: runOf(fixture, reported), status });
    }).pipe(Effect.provide(seamLayer)),
);

it.effect("sends one reply for a message with two final frames, and names it in done", () =>
  Effect.gen(function* () {
    const plain = FIXTURES.plainReply;
    const final = indexOf(
      plain,
      (event) => event.type === "message.updated" && event.payload.role === "assistant",
    );
    const message = plain[final]!.event;
    yield* write(plain.slice(0, final + 1).map((event) => event.event));
    yield* write([{ ...message, id: EventId.make("second-final-frame") }]);
    yield* write(plain.slice(final + 1).map((event) => event.event));
    const items = yield* firstDone({ threadId: message.threadId, afterSequence: 0 });

    assert.strictEqual(items.filter((item) => item.type === "reply").length, 1);
    assert.strictEqual(
      (items.at(-1) as Watch.WatchDone).result.messageId,
      message.type === "message.updated" ? message.payload.id : null,
    );
  }).pipe(Effect.provide(seamLayer)),
);

const deletion = (
  fixture: ReadonlyArray<OrchestrationV2StoredEvent>,
): OrchestrationV2DomainEvent => {
  const created = fixture.find((stored) => stored.event.type === "thread.created")!.event;
  if (created.type !== "thread.created") throw new Error("no thread.created");
  return {
    ...created,
    id: EventId.make("thread-deleted"),
    type: "thread.deleted",
    payload: { ...created.payload, deletedAt: created.occurredAt },
  };
};

it.effect("a settle check that fails with thread not found ends the watch with gone", () =>
  Effect.scoped(
    Effect.gen(function* () {
      const plain = FIXTURES.plainReply;
      const threadId = plain[0]!.event.threadId;
      yield* write(plain.map((event) => event.event));
      const commit = yield* writer;
      const watch = yield* realWatch(
        { threadId, afterSequence: 0 },
        { beforeCheck: () => commit([deletion(plain)]).pipe(Effect.asVoid) },
      );
      const items = yield* watch.until((item) => item.type === "watch_ended");
      const check = yield* watch.nextCheck;

      assert.deepStrictEqual(check.answer, { kind: "gone" });
      assert.deepStrictEqual(items.at(-1), { type: "watch_ended", threadId, reason: "gone" });
      assert.isFalse(items.some(isDone));
    }),
  ).pipe(Effect.provide(seamLayer)),
);

it.effect.each([
  { case: "a settle check that finds the thread deleted", via: "check" },
  { case: "the thread's stored deletion", via: "event" },
] as const)("after done, $case ends the watch with gone and changes no result", ({ via }) =>
  Effect.scoped(
    Effect.gen(function* () {
      const plain = FIXTURES.plainReply;
      const threadId = plain[0]!.event.threadId;
      const stored = yield* write(plain.map((event) => event.event));
      const commit = yield* writer;
      let deleteBeforeCheck = false;
      const watch = yield* realWatch(
        { threadId, afterSequence: 0 },
        {
          beforeCheck: () =>
            Effect.suspend(() => {
              if (!deleteBeforeCheck) return Effect.void;
              deleteBeforeCheck = false;
              return commit([deletion(plain)]).pipe(Effect.asVoid);
            }),
        },
      );
      const first = yield* watch.until(isDone);
      // Real threads store the same terminal run.updated twice (the interrupted fixture).
      const terminal = runUpdates(plain).at(-1)!;
      deleteBeforeCheck = via === "check";
      const after = yield* write(
        via === "check" ? [{ ...terminal, id: EventId.make("terminal-again") }] : [deletion(plain)],
      );
      const rest = yield* watch.until((item) => item.type === "watch_ended");
      const done = first.at(-1) as Watch.WatchDone;

      assert.strictEqual(done.sequence, stored.at(-1)!.sequence);
      assert.deepStrictEqual(
        rest,
        via === "check"
          ? [
              {
                type: "run",
                threadId,
                runId: done.runId,
                status: done.status,
                sequence: after[0]!.sequence,
              },
              { type: "watch_ended", threadId, reason: "gone" },
            ]
          : [
              { type: "gone", threadId, sequence: after[0]!.sequence },
              { type: "watch_ended", threadId, reason: "gone" },
            ],
      );
    }),
  ).pipe(Effect.provide(seamLayer)),
);

it.effect.each([
  { case: "anchored on the cancelled follow-up", anchor: 2 },
  { case: "with no anchor", anchor: null },
] as const)(
  "reports a follow-up cancelled while it waited behind a running turn, $case",
  ({ anchor }) =>
    Effect.gen(function* () {
      const fixture = FIXTURES.queuedCancelled;
      yield* write(fixture.map((event) => event.event));
      const items = yield* firstDone({
        threadId: fixture[0]!.event.threadId,
        afterSequence: 0,
        ...(anchor === null ? {} : { runId: runOf(fixture, anchor) }),
      });
      const done = items.at(-1) as Watch.WatchDone;

      if (anchor === null) {
        assert.deepInclude(done, { runId: runOf(fixture, 1), status: "completed" });
        assert.strictEqual(done.result.text, "first fixture turn complete");
      } else {
        // Run A's answer is never reported as run B's.
        assert.deepInclude(done, { runId: runOf(fixture, 2), status: "cancelled" });
        assert.isNull(done.result.messageId);
        assert.notInclude(done.result.text, "first fixture turn complete");
      }
    }).pipe(Effect.provide(seamLayer)),
);

/**
 * The subagent fixture with no wake run, and with the subagent's completion stored after the root
 * run ends: a terminal run that waits on delegated work, which stores no run.updated when it ends.
 */
const rootWaitsOnSubagent = (() => {
  const fixture = FIXTURES.subagentAfterRoot;
  const wakeRun = JSON.stringify(runOf(fixture, 2));
  const kept = fixture.filter((stored) => !JSON.stringify(stored.event).includes(wakeRun));
  const isSettled = (stored: OrchestrationV2StoredEvent) =>
    stored.event.type === "subagent.updated" && stored.event.payload.status === "completed";
  const rootEnds = kept.findIndex(
    (stored) =>
      stored.event.type === "run.updated" &&
      stored.event.payload.ordinal === 1 &&
      stored.event.payload.status === "completed",
  );
  return {
    before: kept.slice(0, rootEnds + 1).filter((stored) => !isSettled(stored)),
    settles: kept.filter(isSettled),
  };
})();

it.effect("follows up on a terminal run that waits on its subagent, and sends one done", () =>
  Effect.scoped(
    Effect.gen(function* () {
      const fixture = FIXTURES.subagentAfterRoot;
      const threadId = fixture[0]!.event.threadId;
      yield* write(rootWaitsOnSubagent.before.map((event) => event.event));
      const followUps: Array<number> = [];
      const watch = yield* realWatch(
        { threadId, afterSequence: 0 },
        {
          // The first follow-up runs out at once, so the test sees it repeat; the next one waits.
          threads: (real) => ({
            ...real,
            waitForThread: (input) => {
              if (input.timeoutMs <= 1) return real.waitForThread(input);
              followUps.push(input.timeoutMs);
              return followUps.length === 1
                ? real.waitForThread({ ...input, timeoutMs: 1 })
                : real.waitForThread(input);
            },
          }),
        },
      );
      const atReplay = yield* watch.nextCheck;
      const repeated = yield* watch.nextCheck;
      yield* write(rootWaitsOnSubagent.settles.map((event) => event.event));
      const answers = [(yield* watch.nextCheck).answer.kind, (yield* watch.nextCheck).answer.kind];
      const items = yield* watch.until(isDone);
      const reply = fixture.find(
        (stored) =>
          stored.event.type === "message.updated" && stored.event.payload.role === "assistant",
      )!.event;
      if (reply.type !== "message.updated") throw new Error("no assistant message");
      const sentinel = yield* write([
        {
          ...reply,
          id: EventId.make("after-both-dones"),
          payload: { ...reply.payload, id: MessageId.make("after-both-dones") },
        },
      ]);
      const rest = yield* watch.until(
        (item) => item.type === "reply" && item.sequence === sentinel[0]!.sequence,
      );
      const done = items.at(-1) as Watch.WatchDone;

      assert.strictEqual(atReplay.answer.kind, "follow_up");
      assert.strictEqual(repeated.answer.kind, "follow_up");
      // The settle trigger's check and the forked wait both read the settled thread.
      assert.deepStrictEqual(answers, ["done", "done"]);
      assert.deepStrictEqual(followUps, [60_000, 60_000]);
      assert.isFalse(items.slice(0, -1).some(isDone));
      assert.deepInclude(done, { runId: runOf(fixture, 1), status: "completed" });
      assert.strictEqual(done.result.text, "ROOT_STARTED");
      assert.isFalse(rest.some(isDone), "the second read of the same answer sends nothing");
    }),
  ).pipe(Effect.provide(seamLayer)),
);

it.each([
  ["a short text", "Done.", "Done."],
  [
    "a cut at the last sentence end",
    `${"a".repeat(1_190)}. ${"b".repeat(50)}`,
    `${"a".repeat(1_190)}.`,
  ],
  [
    "a cut at the last space with no sentence end",
    "a ".repeat(700),
    `${"a ".repeat(600).trimEnd()}…`,
  ],
])("cuts a result to 1,200 characters: %s", (_case, text, expected) => {
  assert.strictEqual(cutAtSentence(text), expected);
});
