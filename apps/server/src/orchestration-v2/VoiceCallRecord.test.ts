import { assert, it } from "@effect/vitest";
import {
  CommandId,
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
import * as Orchestrator from "./Orchestrator.ts";
import * as ProjectionStore from "./ProjectionStore.ts";
import * as ProviderAdapterRegistry from "./ProviderAdapterRegistry.ts";
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
      { runId: card.runId, callId: card.callId, lines: card.lines, truncated: card.truncated },
      { runId: null, callId: "call-1", lines, truncated: false },
    );
    assert.deepStrictEqual([card.startedAt, card.completedAt], [startedAt, endedAt]);
    assert.strictEqual(card.ordinal, before.nextOrdinal - 1, "it follows the last item");
    assert.notStrictEqual(before.snapshot.projection.thread.archivedAt, null);
    assert.deepStrictEqual(
      yield* look,
      before,
      "every other read, the handoff text and the thread's sequence and ordinals stay as they were",
    );
  }).pipe(Effect.provide(layerTest)),
);
