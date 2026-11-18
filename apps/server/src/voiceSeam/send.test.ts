import { assert, it } from "@effect/vitest";
import { CommandId, MessageId, type RunId, ThreadId } from "@t3tools/contracts";
import * as Effect from "effect/Effect";
import * as Option from "effect/Option";
import * as Ref from "effect/Ref";
import * as SqlClient from "effect/sql/SqlClient";

import * as CommandReceiptStore from "../orchestration-v2/CommandReceiptStore.ts";
import * as ThreadManagement from "../orchestration-v2/ThreadManagementService.ts";
import {
  BeforeInterrupt,
  caller,
  commandIdOf,
  layerTest,
  PROJECT,
  refusalOf,
  restored,
  Restores,
  seedReceipt,
} from "./testWriteLayer.ts";
import type { VoiceInterruptResult } from "./protocol.ts";
import * as VoiceSeam from "./VoiceSeam.ts";

const sendInput = (requestId: string, threadId: ThreadId, text = "Also add a test") => ({
  requestId,
  threadId,
  text,
});

/** A launched thread whose first run waits in preparation, which counts as its active turn. */
const launchThread = Effect.fn("test.launchThread")(function* (requestId: string) {
  const seam = yield* VoiceSeam.VoiceSeam;
  const launched = yield* seam.launch(caller(), {
    requestId,
    projectId: PROJECT,
    instruction: "Fix the flaky ASR test",
  });
  assert.isNotNull(launched.runId);
  return { threadId: launched.threadId, runId: launched.runId! };
});

const recordsOf = Effect.fn("test.recordsOf")(function* (threadId: ThreadId) {
  const threads = yield* ThreadManagement.ThreadManagementService;
  return yield* threads.getThreadRecords(threadId, ["runs", "messages", "turnItems"]);
});

const raiseToFullAccess = Effect.fn("test.raiseToFullAccess")(function* (threadId: ThreadId) {
  const threads = yield* ThreadManagement.ThreadManagementService;
  yield* threads.dispatch({
    type: "thread.runtime-mode.set",
    commandId: CommandId.make(`command:raise:${threadId}`),
    threadId,
    runtimeMode: "full-access",
  });
});

/** Mason stops the active run. `name` keeps each stop its own command. */
const stopRun = Effect.fn("test.stopRun")(function* (threadId: ThreadId, name = "stop") {
  const threads = yield* ThreadManagement.ThreadManagementService;
  yield* threads.interruptThread({
    projectId: PROJECT,
    commandId: CommandId.make(`command:${name}:${threadId}`),
    threadId,
  });
});

/** Mason resumes the queue the stop held, so T3 starts the next queued run as a run end would. */
const resumeQueue = Effect.fn("test.resumeQueue")(function* (threadId: ThreadId) {
  const threads = yield* ThreadManagement.ThreadManagementService;
  yield* threads.dispatch({
    type: "queue.resume",
    commandId: CommandId.make(`command:resume:${threadId}`),
    threadId,
  });
});

/** The run that carries a voice send's message, if T3 made one. */
const voiceRunOf = Effect.fn("test.voiceRunOf")(function* (threadId: ThreadId, requestId: string) {
  const messageId = MessageId.make(yield* commandIdOf("send", requestId));
  return (yield* recordsOf(threadId)).runs.find((run) => run.userMessageId === messageId);
});

const codeOf = <A>(effect: Effect.Effect<A, VoiceSeam.VoiceSeamRefusal>) =>
  effect.pipe(
    Effect.result,
    Effect.map((result) => (result._tag === "Failure" ? result.failure.code : "sent")),
  );

const receiptOf = Effect.fn("test.receiptOf")(function* (
  operation: "send" | "interrupt",
  requestId: string,
) {
  const receipts = yield* CommandReceiptStore.CommandReceiptStoreV2;
  return yield* receipts.getByCommandId(yield* commandIdOf(operation, requestId));
});

it.effect("queues a send behind the running turn and never steers it", () =>
  Effect.gen(function* () {
    const seam = yield* VoiceSeam.VoiceSeam;
    const running = yield* launchThread("01JD5Z7Q8M3N4P5R6S7T8V9X0A");

    const sent = yield* seam.send(
      caller(),
      sendInput("01JD5Z7Q8M3N4P5R6S7T8V9X0B", running.threadId),
    );

    assert.deepInclude(sent, { threadId: running.threadId, delivery: "queued", replayed: false });
    assert.notStrictEqual(sent.runId, running.runId);
    const after = yield* recordsOf(running.threadId);
    assert.strictEqual(after.runs.find((run) => run.id === sent.runId)?.status, "queued");
    // The running turn keeps going on its own message: nothing steered, stopped, or restarted it.
    assert.deepInclude(
      after.runs.find((run) => run.id === running.runId),
      {
        userMessageId: MessageId.make(running.threadId),
      },
    );
    assert.include(
      ["preparing", "starting"],
      after.runs.find((run) => run.id === running.runId)?.status,
    );
    assert.deepStrictEqual(
      after.turnItems
        .filter((item) => item.type === "user_message" || item.type === "run_interrupt_request")
        .map((item) => [
          item.type,
          item.runId,
          item.type === "user_message" ? item.inputIntent : null,
        ]),
      [["user_message", running.runId, "turn_start"]],
    );
    const message = after.messages.find((candidate) => candidate.id === sent.messageId);
    assert.deepInclude(message, { text: "Also add a test", runId: sent.runId });
    assert.deepStrictEqual(yield* restored, [running.threadId]);
  }).pipe(Effect.provide(layerTest)),
);

it.effect(
  "sends once per request id, and a retry after the event log pruned its events answers the same",
  () =>
    Effect.gen(function* () {
      const seam = yield* VoiceSeam.VoiceSeam;
      const sql = yield* SqlClient.SqlClient;
      const { threadId } = yield* launchThread("01JD5Z7Q8M3N4P5R6S7T8V9X1A");
      const requestId = "01JD5Z7Q8M3N4P5R6S7T8V9X1B";
      const commandId = yield* commandIdOf("send", requestId);

      const first = yield* seam.send(caller(), sendInput(requestId, threadId));
      const second = yield* seam.send(caller(), sendInput(requestId, threadId));
      yield* sql`DELETE FROM orchestration_events WHERE command_id = ${commandId}`;
      const pruned = yield* sql<{ readonly count: number }>`
        SELECT COUNT(*) AS count FROM orchestration_events WHERE command_id = ${commandId}
      `;
      assert.strictEqual(pruned[0]?.count, 0);
      const third = yield* seam.send(caller(), sendInput(requestId, threadId));

      assert.strictEqual(first.messageId, MessageId.make(commandId));
      assert.isFalse(first.replayed);
      for (const retry of [second, third]) {
        assert.deepStrictEqual(
          { ...retry, sequence: first.sequence },
          { ...first, replayed: true },
        );
      }
      // A replay's sequence is the thread's as it reads now. The pruned retry's reads lower,
      // because this test deleted the thread's newest events, which real pruning never does.
      assert.isAtLeast(second.sequence, first.sequence);
      const records = yield* recordsOf(threadId);
      assert.strictEqual(
        records.messages.filter((message) => message.id === first.messageId).length,
        1,
      );
      assert.strictEqual(records.runs.filter((run) => run.status === "queued").length, 1);
      // Only the first call reached sendToThread.
      assert.deepStrictEqual(yield* restored, [threadId]);
    }).pipe(Effect.provide(layerTest)),
);

it.effect("answers a send's receipt without sending: refused, or another thread's", () =>
  Effect.gen(function* () {
    const seam = yield* VoiceSeam.VoiceSeam;
    const { threadId } = yield* launchThread("01JD5Z7Q8M3N4P5R6S7T8V9X2A");
    const other = yield* launchThread("01JD5Z7Q8M3N4P5R6S7T8V9X2B");

    const rejected = "01JD5Z7Q8M3N4P5R6S7T8V9X2C";
    const rejectedId = yield* commandIdOf("send", rejected);
    yield* seedReceipt({
      commandId: rejectedId,
      threadId,
      commandType: "message.dispatch",
      status: "rejected",
      error: "Thread is archived.",
    });
    const elsewhere = "01JD5Z7Q8M3N4P5R6S7T8V9X2D";
    yield* seam.send(caller(), sendInput(elsewhere, other.threadId));
    const restoresBefore = yield* restored;

    assert.deepStrictEqual(yield* refusalOf(seam.send(caller(), sendInput(rejected, threadId))), {
      code: "refused",
      reason: "Thread is archived.",
      commandId: rejectedId,
      threadId: undefined,
    });
    assert.strictEqual(
      (yield* refusalOf(seam.send(caller(), sendInput(elsewhere, threadId)))).code,
      "invalid_request",
    );
    assert.deepStrictEqual(yield* restored, restoresBefore);
    const records = yield* recordsOf(threadId);
    assert.isFalse(records.messages.some((message) => message.id === MessageId.make(rejectedId)));
  }).pipe(Effect.provide(layerTest)),
);

it.effect(
  "refuses to send above the ceiling with no worktree restore, and interrupt still stops the thread",
  () =>
    Effect.gen(function* () {
      const seam = yield* VoiceSeam.VoiceSeam;
      const running = yield* launchThread("01JD5Z7Q8M3N4P5R6S7T8V9X3A");
      yield* raiseToFullAccess(running.threadId);
      const requestId = "01JD5Z7Q8M3N4P5R6S7T8V9X3B";

      const refusal = yield* refusalOf(seam.send(caller(), sendInput(requestId, running.threadId)));
      const stopped = yield* seam.interrupt(caller(), {
        requestId: "01JD5Z7Q8M3N4P5R6S7T8V9X3C",
        threadId: running.threadId,
      });

      assert.strictEqual(refusal.code, "above_ceiling");
      assert.deepStrictEqual(yield* restored, []);
      // A mode refusal leaves no receipt, so a retry after Mason lowers the mode can send.
      assert.isTrue(Option.isNone(yield* receiptOf("send", requestId)));
      assert.deepInclude(stopped, { kind: "requested", runId: running.runId });
    }).pipe(Effect.provide(layerTest)),
);

it.effect("refuses a send whose thread Mason raised above the ceiling after the preflight", () =>
  Effect.gen(function* () {
    const seam = yield* VoiceSeam.VoiceSeam;
    const threads = yield* ThreadManagement.ThreadManagementService;
    const { threadId } = yield* launchThread("01JD5Z7Q8M3N4P5R6S7T8V9X4A");
    const requestId = "01JD5Z7Q8M3N4P5R6S7T8V9X4B";
    // The restore runs after the preflight and before the dispatch takes the command lock.
    yield* Ref.set(
      (yield* Restores).during,
      raiseToFullAccess(threadId).pipe(
        Effect.provideService(ThreadManagement.ThreadManagementService, threads),
        Effect.orDie,
        Effect.asVoid,
      ),
    );

    const refusal = yield* refusalOf(seam.send(caller(), sendInput(requestId, threadId)));

    assert.strictEqual(refusal.code, "above_ceiling");
    assert.deepStrictEqual(yield* restored, [threadId]);
    const commandId = yield* commandIdOf("send", requestId);
    const records = yield* recordsOf(threadId);
    assert.isFalse(records.messages.some((message) => message.id === MessageId.make(commandId)));
    assert.isTrue(Option.isNone(yield* receiptOf("send", requestId)));
  }).pipe(Effect.provide(layerTest)),
);

it.effect(
  "never starts a voice follow-up above the ceiling after Mason raises its busy thread",
  () =>
    Effect.gen(function* () {
      const seam = yield* VoiceSeam.VoiceSeam;
      const running = yield* launchThread("01JD5Z7Q8M3N4P5R6S7T8V9X7A");
      const requestId = "01JD5Z7Q8M3N4P5R6S7T8V9X7B";

      const code = yield* codeOf(seam.send(caller(), sendInput(requestId, running.threadId)));
      yield* raiseToFullAccess(running.threadId);
      yield* stopRun(running.threadId);
      yield* resumeQueue(running.threadId);

      // A queued message would start here, in full-access.
      assert.include(
        [undefined, "cancelled"],
        (yield* voiceRunOf(running.threadId, requestId))?.status,
      );
      assert.strictEqual(code, "thread_busy");
      // Refused before T3 saw it, so a retry once the thread is idle can send.
      assert.deepStrictEqual(yield* restored, []);
      assert.isTrue(Option.isNone(yield* receiptOf("send", requestId)));
    }).pipe(Effect.provide(layerTest)),
);

it.effect(
  "cancels a voice follow-up that queued because the thread got busy after the preflight",
  () =>
    Effect.gen(function* () {
      const seam = yield* VoiceSeam.VoiceSeam;
      const threads = yield* ThreadManagement.ThreadManagementService;
      const { threadId } = yield* launchThread("01JD5Z7Q8M3N4P5R6S7T8V9X8A");
      yield* stopRun(threadId, "launch");
      const requestId = "01JD5Z7Q8M3N4P5R6S7T8V9X8B";
      // Mason's own message starts a turn after the preflight read the thread as idle.
      yield* Ref.set(
        (yield* Restores).during,
        threads
          .dispatch({
            type: "message.dispatch",
            commandId: CommandId.make("command:mason-sends"),
            threadId,
            messageId: MessageId.make("message:mason-sends"),
            text: "Run the linter",
            attachments: [],
            dispatchMode: { type: "start_immediately" },
            createdBy: "user",
            creationSource: "web",
          })
          .pipe(Effect.orDie, Effect.asVoid),
      );

      const code = yield* codeOf(seam.send(caller(), sendInput(requestId, threadId)));
      yield* Ref.set((yield* Restores).during, Effect.void);
      const retry = yield* codeOf(seam.send(caller(), sendInput(requestId, threadId)));
      yield* raiseToFullAccess(threadId);
      yield* stopRun(threadId);
      yield* resumeQueue(threadId);

      assert.include([undefined, "cancelled"], (yield* voiceRunOf(threadId, requestId))?.status);
      assert.deepStrictEqual([code, retry], ["thread_busy", "thread_busy"]);
    }).pipe(Effect.provide(layerTest)),
);

it.effect("an interrupt that found nothing running never stops a later run on retry", () =>
  Effect.gen(function* () {
    const seam = yield* VoiceSeam.VoiceSeam;
    const { threadId } = yield* launchThread("01JD5Z7Q8M3N4P5R6S7T8V9X9A");
    yield* stopRun(threadId);
    const stop = { requestId: "01JD5Z7Q8M3N4P5R6S7T8V9X9B", threadId };

    const idle = yield* seam.interrupt(caller(), stop);
    const next = yield* seam.send(caller(), sendInput("01JD5Z7Q8M3N4P5R6S7T8V9X9C", threadId));
    const before = yield* recordsOf(threadId);
    const retry = yield* seam.interrupt(caller(), stop);

    assert.deepStrictEqual(
      [idle, retry],
      [{ kind: "nothing_running" }, { kind: "nothing_running" }],
    );
    assert.strictEqual(next.delivery, "started");
    const after = yield* recordsOf(threadId);
    assert.deepStrictEqual(
      after.runs.find((run) => run.id === next.runId),
      before.runs.find((run) => run.id === next.runId),
    );
    assert.isFalse(
      after.turnItems.some(
        (item) => item.type === "run_interrupt_request" && item.runId === next.runId,
      ),
    );
  }).pipe(Effect.provide(layerTest)),
);

it.effect("two copies of one interrupt stop one run, and only one answers requested", () =>
  Effect.gen(function* () {
    const seam = yield* VoiceSeam.VoiceSeam;
    const running = yield* launchThread("01JD5Z7Q8M3N4P5R6S7T8V9XAA");
    const stop = { requestId: "01JD5Z7Q8M3N4P5R6S7T8V9XAB", threadId: running.threadId };
    const copies = yield* Ref.make<ReadonlyArray<VoiceInterruptResult>>([]);
    const nextRun = yield* Ref.make<RunId | null>(null);
    // The second copy runs in full after the first passed its receipt check, then a new turn starts.
    yield* Ref.set(
      yield* BeforeInterrupt,
      Effect.gen(function* () {
        const second = yield* seam.interrupt(caller(), stop);
        yield* Ref.update(copies, (all) => [...all, second]);
        const next = yield* seam.send(
          caller(),
          sendInput("01JD5Z7Q8M3N4P5R6S7T8V9XAC", running.threadId),
        );
        yield* Ref.set(nextRun, next.runId);
      }).pipe(Effect.orDie),
    );

    const first = yield* seam.interrupt(caller(), stop);
    const answers = [...(yield* Ref.get(copies)), first];
    const later = yield* Ref.get(nextRun);

    assert.isNotNull(later);
    assert.deepStrictEqual(
      answers.filter((answer) => answer.kind === "requested").map((answer) => answer.runId),
      [running.runId],
    );
    assert.deepInclude(answers, { kind: "nothing_running" });
    const records = yield* recordsOf(running.threadId);
    assert.isFalse(
      records.turnItems.some(
        (item) => item.type === "run_interrupt_request" && item.runId === later,
      ),
    );
  }).pipe(Effect.provide(layerTest)),
);

it.effect(
  "interrupts once per request id: a retry answers nothing_running and never stops a later run",
  () =>
    Effect.gen(function* () {
      const seam = yield* VoiceSeam.VoiceSeam;
      const running = yield* launchThread("01JD5Z7Q8M3N4P5R6S7T8V9X5A");
      const stop = { requestId: "01JD5Z7Q8M3N4P5R6S7T8V9X5B", threadId: running.threadId };

      const first = yield* seam.interrupt(caller(), stop);
      const second = yield* seam.interrupt(caller(), stop);
      const next = yield* seam.send(
        caller(),
        sendInput("01JD5Z7Q8M3N4P5R6S7T8V9X5C", running.threadId),
      );
      const later = yield* recordsOf(running.threadId);
      const third = yield* seam.interrupt(caller(), stop);

      assert.deepInclude(first, { kind: "requested", runId: running.runId });
      assert.deepStrictEqual(second, { kind: "nothing_running" });
      assert.deepStrictEqual(third, { kind: "nothing_running" });
      assert.strictEqual(next.delivery, "started");
      // The later run is exactly as it was before the retry.
      assert.deepStrictEqual(
        (yield* recordsOf(running.threadId)).runs.find((run) => run.id === next.runId),
        later.runs.find((run) => run.id === next.runId),
      );
    }).pipe(Effect.provide(layerTest)),
);

it.effect("refuses send and interrupt outside the ceiling's operations and threads", () =>
  Effect.gen(function* () {
    const seam = yield* VoiceSeam.VoiceSeam;
    const { threadId } = yield* launchThread("01JD5Z7Q8M3N4P5R6S7T8V9X6A");
    const readOnly = caller({ operations: ["list", "read", "watch"] });
    const missing = ThreadId.make("thread:missing");
    const requestId = "01JD5Z7Q8M3N4P5R6S7T8V9X6B";

    const codes = [
      yield* refusalOf(seam.send(readOnly, sendInput(requestId, threadId))),
      yield* refusalOf(seam.interrupt(readOnly, { requestId, threadId })),
      yield* refusalOf(seam.send(caller(), sendInput(requestId, missing))),
      yield* refusalOf(seam.interrupt(caller(), { requestId, threadId: missing })),
    ].map((refusal) => refusal.code);

    assert.deepStrictEqual(codes, [
      "operation_not_allowed",
      "operation_not_allowed",
      "not_found",
      "not_found",
    ]);
    assert.deepStrictEqual(yield* restored, []);
  }).pipe(Effect.provide(layerTest)),
);
