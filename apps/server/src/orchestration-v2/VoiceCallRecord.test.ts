import { assert, it } from "@effect/vitest";
import {
  CommandId,
  EventId,
  type OrchestrationV2TurnItem,
  MessageId,
  ProjectId,
  ProviderDriverKind,
  ProviderInstanceId,
  ThreadId,
  TurnItemId,
} from "@t3tools/contracts";
import { handoffCoverage } from "@t3tools/provider-core/server/handoffBudget";
import * as DateTime from "effect/DateTime";
import * as Effect from "effect/Effect";
import * as Layer from "effect/Layer";
import * as TestClock from "effect/testing/TestClock";
import * as SqlitePersistence from "../persistence/Sqlite.ts";
import { CodexProviderCapabilitiesV2 } from "./Adapters/CodexAdapterV2.ts";
import * as EffectOutbox from "./EffectOutbox.ts";
import * as EventSink from "./EventSink.ts";
import * as Orchestrator from "./Orchestrator.ts";
import * as ProjectionStore from "./ProjectionStore.ts";
import * as ProviderAdapterRegistry from "./ProviderAdapterRegistry.ts";
import { encodeThreadHistoryCursor } from "./threadHistoryPaging.ts";
import * as ProviderReplayHarness from "./testkit/ProviderReplayHarness.ts";
import type * as ProviderAdapter from "@t3tools/provider-core/server/ProviderAdapter";

const instanceId = ProviderInstanceId.make("codex");
const modelSelection = { instanceId, model: "gpt-5.1-codex" };
const adapter = {
  instanceId,
  driver: ProviderDriverKind.make("codex"),
  getCapabilities: () => Effect.succeed(CodexProviderCapabilitiesV2),
  planSelectionTransition: () => Effect.succeed({ type: "apply_on_next_turn" as const }),
  openSession: () => Effect.die("A voice call record runs no provider"),
} as ProviderAdapter.ProviderAdapterV2["Service"];
const layerDatabase = SqlitePersistence.layerMemory;
const layerTest = Layer.mergeAll(
  layerDatabase,
  ProjectionStore.layer.pipe(Layer.provide(layerDatabase)),
  EffectOutbox.layer.pipe(Layer.provide(layerDatabase)),
  ProviderReplayHarness.layerWithRegistry(
    { name: "voice-call-record" },
    ProviderAdapterRegistry.layerFromAdapters([adapter]),
    { databaseLayer: layerDatabase, runEffectWorker: false },
  ),
);

const threadId = ThreadId.make("thread:voice-call-record");
const cardId = TurnItemId.make("turn-item:voice-call:voice-call-record");
const startedAt = DateTime.makeUnsafe("2026-11-21T10:00:00.000Z");
const endedAt = DateTime.makeUnsafe("2026-11-21T10:04:00.000Z");
const lines = [
  { speaker: "caller", text: "How is it going?", relay: false },
  {
    speaker: "voice",
    text: "The clamp thread finished and the tests pass.",
    relay: true,
    claim: {
      act: "done",
      evidence: [
        { sequence: 41, ref: { type: "message", messageId: MessageId.make("message:reply") } },
        { sequence: 40, ref: null },
      ],
    },
  },
  { speaker: "voice", text: "Want me to", relay: false, interrupted: true },
] as const;
const record = {
  type: "thread.voice-call.record",
  commandId: CommandId.make("voice:record_call:voice-call-record"),
  threadId,
  turnItemId: cardId,
  callId: "call-1",
  startedAt,
  endedAt,
  lines,
  truncated: false,
} as const;

it.effect("records a voice call once, for the owner, and no other read of the thread changes", () =>
  Effect.gen(function* () {
    const orchestrator = yield* Orchestrator.OrchestratorV2;
    yield* orchestrator.dispatch({
      type: "thread.create",
      commandId: CommandId.make("create-voice-call-record"),
      threadId,
      projectId: ProjectId.make("project:voice-call-record"),
      title: "Voice call record",
      modelSelection,
      runtimeMode: "full-access",
      interactionMode: "default",
      branch: null,
      worktreePath: null,
      createdBy: "user",
      creationSource: "web",
    });
    yield* orchestrator.dispatch({
      type: "message.dispatch",
      commandId: CommandId.make("message-voice-call-record"),
      threadId,
      messageId: MessageId.make("message:voice-call-record"),
      text: "Before the call",
      attachments: [],
      dispatchMode: { type: "defer_start" },
      createdBy: "user",
      creationSource: "web",
    });
    // Archived during the call: the transcript is still the caller's record of it.
    yield* orchestrator.dispatch({
      type: "thread.archive",
      commandId: CommandId.make("archive-voice-call-record"),
      threadId,
    });
    const projectionStore = yield* ProjectionStore.ProjectionStoreV2;
    // What a model, an agent or a guest is built from: every read of the thread's items.
    const look = Effect.gen(function* () {
      const items = (yield* projectionStore.getThreadRecords(threadId, ["turnItems", "messages"]))
        .turnItems;
      const runless = (yield* projectionStore.getThreadRecords(threadId, ["turnItems"], {
        turnItemRunIds: [null],
      })).turnItems;
      return {
        snapshot: yield* orchestrator.getThreadSnapshot(threadId),
        window: yield* orchestrator.getThreadSnapshotWindow(threadId, { rowLimit: 50 }),
        history: yield* orchestrator.getThreadHistoryPage(
          threadId,
          encodeThreadHistoryCursor({
            snapshotSequence: 0,
            sourceThreadId: threadId,
            sourceItemId: "absent",
            position: 100,
          }),
        ),
        nextOrdinal: yield* projectionStore.getNextTurnItemOrdinal(threadId),
        items,
        runless,
        handoff: handoffCoverage({
          threadId,
          coveredRunOrdinals: { from: 1, to: 1 },
          items: runless,
        }),
      };
    });
    const before = yield* look;
    yield* TestClock.adjust("1 minute");
    yield* orchestrator.dispatch(record);
    yield* orchestrator.dispatch(record);
    const again = yield* orchestrator
      .dispatch({ ...record, commandId: CommandId.make("voice:record_call:again") })
      .pipe(Effect.flip);
    assert.strictEqual(again._tag, "OrchestratorDispatchError");

    const calls = yield* orchestrator.getThreadVoiceCalls(threadId);
    assert.strictEqual(calls.length, 1, "one card, whatever the retries");
    const card = calls[0]!;
    assert.deepStrictEqual(
      {
        runId: card.runId,
        callId: card.callId,
        lineCount: card.lineCount,
        truncated: card.truncated,
      },
      { runId: null, callId: "call-1", lineCount: lines.length, truncated: false },
    );
    assert.deepStrictEqual(yield* orchestrator.getVoiceCallLines(threadId, cardId), lines);
    assert.deepStrictEqual([card.startedAt, card.completedAt], [startedAt, endedAt]);
    assert.strictEqual(card.ordinal, before.items.at(-1)?.ordinal, "it follows the last item");
    assert.notStrictEqual(before.snapshot.projection.thread.archivedAt, null);
    assert.deepStrictEqual(
      yield* look,
      before,
      "every other read, the handoff text and the thread's sequence and ordinals stay as they were",
    );

    // Two copies of one command at once: the orchestrator's command receipt answers the second.
    const second = {
      ...record,
      commandId: CommandId.make("voice:record_call:second"),
      turnItemId: TurnItemId.make("turn-item:voice-call:second"),
    };
    yield* Effect.all([orchestrator.dispatch(second), orchestrator.dispatch(second)], {
      concurrency: "unbounded",
    });
    assert.strictEqual((yield* orchestrator.getThreadVoiceCalls(threadId)).length, 2);
  }).pipe(Effect.provide(layerTest)),
);

it.effect(
  "a call made while a prompt waits in the queue follows what shows, so the prompt shows after it",
  () =>
    Effect.gen(function* () {
      const orchestrator = yield* Orchestrator.OrchestratorV2;
      const queuedThread = ThreadId.make("thread:voice-call-queued");
      yield* orchestrator.dispatch({
        type: "thread.create",
        commandId: CommandId.make("create-voice-call-queued"),
        threadId: queuedThread,
        projectId: ProjectId.make("project:voice-call-record"),
        title: "Voice call while queued",
        modelSelection,
        runtimeMode: "full-access",
        interactionMode: "default",
        branch: null,
        worktreePath: null,
        createdBy: "user",
        creationSource: "web",
      });
      for (const [name, dispatchMode] of [
        ["running", { type: "start_immediately" }],
        ["queued", { type: "queue_after_active" }],
      ] as const) {
        yield* orchestrator.dispatch({
          type: "message.dispatch",
          commandId: CommandId.make(`message-voice-call-queued-${name}`),
          threadId: queuedThread,
          messageId: MessageId.make(`message:voice-call-queued-${name}`),
          text: `A ${name} prompt`,
          attachments: [],
          dispatchMode,
          createdBy: "user",
          creationSource: "web",
        });
      }
      const before = yield* orchestrator.getThreadProjection(queuedThread);
      const queuedRun = before.runs.find((run) => run.status === "queued");
      const shownLast = before.turnItems.at(-1);
      assert.isDefined(queuedRun, "the second prompt waits in a queued run");
      assert.isDefined(shownLast);
      if (queuedRun === undefined || shownLast === undefined) return;
      // The queued run's input as the provider pipeline stores it: an item no reader shows yet.
      const eventSink = yield* EventSink.EventSinkV2;
      const now = yield* DateTime.now;
      yield* eventSink.write({
        events: [
          {
            id: EventId.make("event:voice-call-queued-input"),
            type: "turn-item.updated" as const,
            threadId: queuedThread,
            occurredAt: now,
            payload: {
              ...shownLast,
              id: TurnItemId.make("turn-item:voice-call-queued-input"),
              runId: queuedRun.id,
              ordinal: shownLast.ordinal + 1,
              type: "user_message",
              messageId: MessageId.make("message:voice-call-queued-input"),
              text: "A queued prompt",
              createdBy: "user",
              creationSource: "web",
              inputIntent: "queued_turn",
              attachments: [],
            } as OrchestrationV2TurnItem,
          },
        ],
      });
      yield* orchestrator.dispatch({
        ...record,
        commandId: CommandId.make("voice:record_call:queued"),
        threadId: queuedThread,
        turnItemId: TurnItemId.make("turn-item:voice-call:queued"),
      });
      const [call] = yield* orchestrator.getThreadVoiceCalls(queuedThread);
      assert.strictEqual(
        call?.ordinal,
        shownLast.ordinal,
        "it follows the last item shown, so the queued prompt shows after it once it runs",
      );
    }).pipe(Effect.provide(layerTest)),
);

it.effect(
  "a call on a thread with no item yet follows none, and calls after one item read back in the order made",
  () =>
    Effect.gen(function* () {
      const orchestrator = yield* Orchestrator.OrchestratorV2;
      const emptyThread = ThreadId.make("thread:voice-call-empty");
      yield* orchestrator.dispatch({
        type: "thread.create",
        commandId: CommandId.make("create-voice-call-empty"),
        threadId: emptyThread,
        projectId: ProjectId.make("project:voice-call-record"),
        title: "No items yet",
        modelSelection,
        runtimeMode: "full-access",
        interactionMode: "default",
        branch: null,
        worktreePath: null,
        createdBy: "user",
        creationSource: "web",
      });
      // The later call's id sorts first, so only the start time can put them in the order made.
      for (const [id, minute] of [
        ["turn-item:voice-call:z-first", "10"],
        ["turn-item:voice-call:a-second", "30"],
      ] as const) {
        yield* orchestrator.dispatch({
          ...record,
          commandId: CommandId.make(`voice:record_call:${id}`),
          threadId: emptyThread,
          turnItemId: TurnItemId.make(id),
          startedAt: DateTime.makeUnsafe(`2026-11-21T10:${minute}:00.000Z`),
          endedAt: DateTime.makeUnsafe(`2026-11-21T10:${minute}:30.000Z`),
        });
      }
      const calls = yield* orchestrator.getThreadVoiceCalls(emptyThread);
      assert.deepStrictEqual(
        calls.map((call) => [call.id, call.ordinal]),
        [
          ["turn-item:voice-call:z-first", 0],
          ["turn-item:voice-call:a-second", 0],
        ],
      );
    }).pipe(Effect.provide(layerTest)),
);
