import { assert, it } from "@effect/vitest";
import { CommandId, ThreadId } from "@t3tools/contracts";
import * as Effect from "effect/Effect";
import * as Option from "effect/Option";
import * as Ref from "effect/Ref";
import * as SqlClient from "effect/sql/SqlClient";

import * as CommandReceiptStore from "../orchestration-v2/CommandReceiptStore.ts";
import * as ThreadManagement from "../orchestration-v2/ThreadManagementService.ts";
import {
  caller,
  commandIdOf,
  Launched,
  launchCount,
  layerTest,
  OUTSIDE,
  NO_MODEL,
  PROJECT,
  REPLAY,
  refusalOf,
  seedReceipt,
} from "./testWriteLayer.ts";
import * as VoiceSeam from "./VoiceSeam.ts";

const REQUEST = "01JD5Z7Q8M3N4P5R6S7T8V9W0X";
const launchInput = (requestId = REQUEST, projectId = PROJECT) => ({
  requestId,
  projectId,
  instruction: "Fix the flaky ASR test",
});

it.effect(
  "launches once per request id at the ceiling, and a retry replays the thread it started",
  () =>
    Effect.gen(function* () {
      const seam = yield* VoiceSeam.VoiceSeam;
      const threads = yield* ThreadManagement.ThreadManagementService;
      const input = launchInput();

      const first = yield* seam.launch(caller(), input);
      const second = yield* seam.launch(caller(), input);

      // The project default is full-access; the ceiling is auto-accept-edits.
      assert.deepInclude(first, {
        runtimeMode: "auto-accept-edits",
        model: "Showcase",
        replayed: false,
      });
      assert.isNotNull(first.runId);
      const shell = yield* threads.getThreadShell(first.threadId);
      assert.deepInclude(shell, {
        projectId: PROJECT,
        runtimeMode: "auto-accept-edits",
        interactionMode: "default",
        modelSelection: REPLAY,
        title: "Fix the flaky ASR test",
        deletedAt: null,
      });
      // The instruction seeds the title, and T3 is asked to generate a short one from it.
      assert.isTrue(shell?.titleRegeneration != null || shell?.lastTitleRegeneration != null);
      assert.deepStrictEqual(second, {
        ...first,
        sequence: yield* threads.getThreadEventSequence(first.threadId),
        replayed: true,
      });
      assert.isAtLeast(second.sequence, first.sequence);
      assert.strictEqual(yield* launchCount, 1);
      const listed = yield* threads.listProjectThreads({
        projectId: PROJECT,
        includeSubagents: false,
      });
      assert.deepStrictEqual(
        listed.map((thread) => thread.id),
        [first.threadId],
      );
    }).pipe(Effect.provide(layerTest)),
);

it.effect("never calls launch on a receipt, even after the event log pruned its events", () =>
  Effect.gen(function* () {
    const seam = yield* VoiceSeam.VoiceSeam;
    const sql = yield* SqlClient.SqlClient;
    const requestId = "01JD5Z7Q8M3N4P5R6S7T8V9W1A";
    const first = yield* seam.launch(caller(), launchInput(requestId));
    const before = yield* launchCount;
    const commandId = yield* commandIdOf("launch", requestId);
    yield* sql`DELETE FROM orchestration_events WHERE command_id = ${commandId}`;
    const pruned = yield* sql<{ readonly count: number }>`
      SELECT COUNT(*) AS count FROM orchestration_events WHERE command_id = ${commandId}
    `;
    assert.strictEqual(pruned[0]?.count, 0);

    const retried = yield* seam.launch(caller(), launchInput(requestId));

    assert.deepInclude(retried, { threadId: first.threadId, runId: first.runId, replayed: true });
    assert.strictEqual(yield* launchCount, before);
  }).pipe(Effect.provide(layerTest)),
);

it.effect("answers each row of the receipt table without launching", () =>
  Effect.gen(function* () {
    const seam = yield* VoiceSeam.VoiceSeam;
    const threads = yield* ThreadManagement.ThreadManagementService;

    // Launch receipt rejected: no thread exists, and the recorded error is the reason.
    const rejected = "01JD5Z7Q8M3N4P5R6S7T8V9W2B";
    const rejectedId = yield* commandIdOf("launch", rejected);
    yield* seedReceipt({
      commandId: rejectedId,
      threadId: ThreadId.make(rejectedId),
      commandType: "thread.create",
      status: "rejected",
      error: "Project is being cloned.",
    });
    const silent = "01JD5Z7Q8M3N4P5R6S7T8V9W3C";
    const silentId = yield* commandIdOf("launch", silent);
    yield* seedReceipt({
      commandId: silentId,
      threadId: ThreadId.make(silentId),
      commandType: "thread.create",
      status: "rejected",
      error: null,
    });

    // Launch accepted, its first message rejected: the thread exists.
    const messageRefused = "01JD5Z7Q8M3N4P5R6S7T8V9W4D";
    const started = yield* seam.launch(caller(), launchInput(messageRefused));
    const messageRefusedId = yield* commandIdOf("launch", messageRefused);
    const initialMessageId = CommandId.make(`${messageRefusedId}:initial-message`);
    yield* seedReceipt({
      commandId: initialMessageId,
      threadId: started.threadId,
      commandType: "thread.create",
      status: "rejected",
      error: "Thread is archived.",
    });

    // Launch accepted, its thread deleted since.
    const deleted = "01JD5Z7Q8M3N4P5R6S7T8V9W5E";
    const doomed = yield* seam.launch(caller(), launchInput(deleted));
    yield* threads.dispatch({
      type: "thread.delete",
      commandId: CommandId.make("command:delete-voice-thread"),
      threadId: doomed.threadId,
    });

    const before = yield* launchCount;
    assert.deepStrictEqual(yield* refusalOf(seam.launch(caller(), launchInput(rejected))), {
      code: "refused",
      reason: "Project is being cloned.",
      commandId: rejectedId,
      threadId: undefined,
    });
    assert.deepStrictEqual(yield* refusalOf(seam.launch(caller(), launchInput(silent))), {
      code: "refused",
      reason: undefined,
      commandId: silentId,
      threadId: undefined,
    });
    assert.deepStrictEqual(yield* refusalOf(seam.launch(caller(), launchInput(messageRefused))), {
      code: "refused",
      reason: "Thread is archived.",
      commandId: initialMessageId,
      threadId: started.threadId,
    });
    assert.deepStrictEqual(yield* refusalOf(seam.launch(caller(), launchInput(deleted))), {
      code: "gone",
      reason: undefined,
      commandId: undefined,
      threadId: undefined,
    });
    assert.strictEqual(yield* launchCount, before);
    // A rejected launch never became a thread.
    assert.isNull(yield* threads.getThreadShell(ThreadId.make(rejectedId)));
  }).pipe(Effect.provide(layerTest)),
);

it.effect("refuses outside the ceiling's projects and operations, and starts nothing", () =>
  Effect.gen(function* () {
    const seam = yield* VoiceSeam.VoiceSeam;
    const threads = yield* ThreadManagement.ThreadManagementService;
    const receipts = yield* CommandReceiptStore.CommandReceiptStoreV2;
    const requestId = "01JD5Z7Q8M3N4P5R6S7T8V9W6F";
    const before = yield* launchCount;

    const outside = yield* refusalOf(seam.launch(caller(), launchInput(requestId, OUTSIDE)));
    const disallowed = yield* refusalOf(
      seam.launch(caller({ operations: ["list", "read", "watch"] }), launchInput(requestId)),
    );
    const noModel = yield* refusalOf(seam.launch(caller(), launchInput(requestId, NO_MODEL)));

    assert.strictEqual(outside.code, "project_not_allowed");
    assert.strictEqual(disallowed.code, "operation_not_allowed");
    assert.strictEqual(noModel.code, "no_default_model");
    assert.strictEqual(yield* launchCount, before);
    const commandId = yield* commandIdOf("launch", requestId);
    assert.isTrue(Option.isNone(yield* receipts.getByCommandId(commandId)));
    assert.deepStrictEqual(
      yield* threads.listProjectThreads({ projectId: OUTSIDE, includeSubagents: false }),
      [],
    );
  }).pipe(Effect.provide(layerTest)),
);

it.effect("starts at the ceiling when the ceiling is below the project's mode", () =>
  Effect.gen(function* () {
    const seam = yield* VoiceSeam.VoiceSeam;
    const launched = yield* seam.launch(
      caller({ maxRuntimeMode: "approval-required" }),
      launchInput("01JD5Z7Q8M3N4P5R6S7T8V9W7G"),
    );
    assert.strictEqual(launched.runtimeMode, "approval-required");
    const inputs = yield* Ref.get(yield* Launched);
    assert.strictEqual(inputs.at(-1)?.runtimeMode, "approval-required");
    assert.strictEqual(inputs.at(-1)?.interactionMode, "default");
  }).pipe(Effect.provide(layerTest)),
);
