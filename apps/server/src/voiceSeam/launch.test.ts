import * as NodeCrypto from "@effect/platform-node/NodeCrypto";
import * as NodeServices from "@effect/platform-node/NodeServices";
import { assert, it } from "@effect/vitest";
import {
  AuthSessionId,
  CommandId,
  type ModelSelection,
  ProjectId,
  ProviderDriverKind,
  type ServerProvider,
  ThreadId,
} from "@t3tools/contracts";
import * as IdAllocator from "@t3tools/provider-core/server/IdAllocator";
import type * as ProviderAdapter from "@t3tools/provider-core/server/ProviderAdapter";
import * as Context from "effect/Context";
import * as Digest from "effect/crypto/Digest";
import * as DateTime from "effect/DateTime";
import * as Effect from "effect/Effect";
import * as Layer from "effect/Layer";
import * as Option from "effect/Option";
import * as Ref from "effect/Ref";
import * as SqlClient from "effect/sql/SqlClient";

import * as CheckpointStore from "../checkpointing/CheckpointStore.ts";
import * as GitWorkflow from "../git/GitWorkflowService.ts";
import { CodexProviderCapabilitiesV2 } from "../orchestration-v2/Adapters/CodexAdapterV2.ts";
import * as CommandReceiptStore from "../orchestration-v2/CommandReceiptStore.ts";
import * as ProjectStore from "../orchestration-v2/ProjectStore.ts";
import * as ProviderAdapterRegistry from "../orchestration-v2/ProviderAdapterRegistry.ts";
import * as ThreadLaunch from "../orchestration-v2/ThreadLaunchService.ts";
import * as ThreadManagement from "../orchestration-v2/ThreadManagementService.ts";
import * as ProviderReplayHarness from "../orchestration-v2/testkit/ProviderReplayHarness.ts";
import * as OrchestrationEventStore from "../persistence/OrchestrationEventStore.ts";
import * as Sqlite from "../persistence/Sqlite.ts";
import * as ManagedProjectFolders from "../project/ManagedProjectFolders.ts";
import * as ProjectCloneTracker from "../project/ProjectCloneTracker.ts";
import * as ProjectService from "../project/ProjectService.ts";
import * as ProjectSetupScriptRunner from "../project/ProjectSetupScriptRunner.ts";
import * as WorktreeSetupTracker from "../project/WorktreeSetupTracker.ts";
import { DEV_REPLAY_INSTANCE_ID } from "../provider/ProviderInstanceRegistryHydration.ts";
import * as ProviderRegistryMock from "../provider/testUtils/providerRegistryMock.ts";
import * as ServerSettings from "../serverSettings.ts";
import * as TerminalManager from "../terminal/Manager.ts";
import * as TextGeneration from "../textGeneration/TextGeneration.ts";
import { VOICE_OPERATIONS, type VoiceSeamConfig } from "./ceiling.ts";
import { voiceCommandId } from "./launch.ts";
import * as VoiceSeam from "./VoiceSeam.ts";

// The real orchestrator and the real ThreadLaunchService, with its launches counted. No provider
// turn runs: a launch's run waits in preparation, which is all the receipt gate needs.

const PROJECT = ProjectId.make("project:voice-sandbox");
const NO_MODEL = ProjectId.make("project:no-model");
const OUTSIDE = ProjectId.make("project:outside");
const REPLAY: ModelSelection = { instanceId: DEV_REPLAY_INSTANCE_ID, model: "showcase" };

const project = (id: ProjectId, defaultModelSelection: ModelSelection | null) => ({
  id,
  title: "Voice sandbox",
  workspaceRoot: `/projects/${id}`,
  repositoryIdentity: null,
  faviconPath: null,
  defaultModelSelection,
  defaultThreadEnvMode: null,
  scripts: [],
  pinnedAt: null,
  archivedAt: null,
  createdAt: "2026-11-17T00:00:00.000Z",
  updatedAt: "2026-11-17T00:00:00.000Z",
  deletedAt: null,
});
const PROJECTS = new Map([
  [PROJECT, project(PROJECT, REPLAY)],
  [NO_MODEL, project(NO_MODEL, null)],
  [OUTSIDE, project(OUTSIDE, REPLAY)],
]);

const replayProvider: ServerProvider = {
  instanceId: DEV_REPLAY_INSTANCE_ID,
  driver: ProviderDriverKind.make("acpRegistry"),
  displayName: "Replay (dev)",
  enabled: true,
  installed: true,
  status: "ready",
  version: null,
  auth: { status: "authenticated" },
  checkedAt: "2026-11-17T00:00:00Z",
  models: [{ slug: "showcase", name: "Showcase", isCustom: false, capabilities: null }],
  slashCommands: [],
  skills: [],
};

const replayAdapter = {
  instanceId: DEV_REPLAY_INSTANCE_ID,
  driver: ProviderDriverKind.make("acpRegistry"),
  getCapabilities: () => Effect.succeed(CodexProviderCapabilitiesV2),
  planSelectionTransition: () => Effect.succeed({ type: "apply_on_next_turn" as const }),
  openSession: () => Effect.die("no provider turn runs in launch tests"),
} as ProviderAdapter.ProviderAdapterV2["Service"];

/** Every input the seam handed to ThreadLaunchService.launch. */
class Launched extends Context.Service<
  Launched,
  Ref.Ref<ReadonlyArray<ThreadLaunch.ThreadLaunchInput>>
>()("t3/voiceSeam/launch.test/Launched") {}

const layerDatabase = Sqlite.layerMemory;
const layerOrchestrator = ProviderReplayHarness.layerWithRegistry(
  { name: "voice-launch" },
  ProviderAdapterRegistry.layerFromAdapters([replayAdapter]),
  { databaseLayer: layerDatabase, runEffectWorker: false },
);
const layerSettings = ServerSettings.layerTest({
  // As the dev server seeds it. An instance missing here reads as disabled.
  providerInstances: {
    [DEV_REPLAY_INSTANCE_ID]: {
      displayName: "Replay (dev)",
      driver: ProviderDriverKind.make("acpRegistry"),
      config: { source: "local" },
    },
  },
  // The "request" a voice launch inherits. The ceiling must lower it.
  defaultRuntimeMode: "full-access",
});
const layerReceipts = CommandReceiptStore.layer.pipe(Layer.provide(layerDatabase));
const layerThreads = ThreadManagement.layer.pipe(
  Layer.provide(Layer.mergeAll(layerOrchestrator, layerSettings, layerReceipts)),
);
const layerProjects = Layer.mock(ProjectService.ProjectService)({
  getById: (id) => Effect.succeed(Option.fromNullishOr(PROJECTS.get(id))),
});
const layerExternal = Layer.mergeAll(
  WorktreeSetupTracker.layer,
  Layer.mock(ProjectCloneTracker.ProjectCloneTracker)({ get: () => Effect.succeed(null) }),
  Layer.mock(TerminalManager.TerminalManager)({ close: () => Effect.void }),
  Layer.mock(CheckpointStore.CheckpointStore)({ isGitRepository: () => Effect.succeed(true) }),
  Layer.mock(GitWorkflow.GitWorkflowService)({ currentBranch: () => Effect.succeed(null) }),
  Layer.succeed(ProjectSetupScriptRunner.ProjectSetupScriptRunner, {
    runForThread: () => Effect.succeed({ status: "no-script" as const }),
  }),
  Layer.mock(TextGeneration.TextGeneration)({}),
  Layer.mock(ManagedProjectFolders.ManagedProjectFolders)({
    namedProjectsRoot: "/projects",
    isScratchProject: () => Effect.succeed(false),
    folderForThread: () => Effect.succeedNone,
  }),
  layerProjects,
  layerSettings,
  ProviderRegistryMock.layer([replayProvider]),
);
const layerCountedLaunches = Layer.effect(
  ThreadLaunch.ThreadLaunchService,
  Effect.gen(function* () {
    const real = yield* ThreadLaunch.ThreadLaunchService;
    const launched = yield* Launched;
    return {
      ...real,
      launch: (input) =>
        Ref.update(launched, (inputs) => [...inputs, input]).pipe(
          Effect.andThen(real.launch(input)),
        ),
    } satisfies ThreadLaunch.ThreadLaunchService["Service"];
  }),
).pipe(
  Layer.provide(
    ThreadLaunch.layer.pipe(
      Layer.provide(
        Layer.mergeAll(
          layerExternal,
          layerThreads,
          layerReceipts,
          layerOrchestrator,
          IdAllocator.layer,
        ),
      ),
    ),
  ),
);
const layerTest = VoiceSeam.layer.pipe(
  Layer.provideMerge(layerCountedLaunches),
  Layer.provideMerge(
    Layer.mergeAll(
      layerThreads,
      layerReceipts,
      layerExternal,
      OrchestrationEventStore.layer,
      ProjectStore.layer,
      NodeCrypto.layer,
    ),
  ),
  Layer.provideMerge(
    Layer.effect(Launched, Ref.make<ReadonlyArray<ThreadLaunch.ThreadLaunchInput>>([])),
  ),
  Layer.provideMerge(Layer.merge(layerDatabase, NodeServices.layer)),
);

const CONFIG: VoiceSeamConfig = {
  maxRuntimeMode: "auto-accept-edits",
  operations: [...VOICE_OPERATIONS],
  projects: [PROJECT, NO_MODEL].map((projectId) => ({ projectId, spokenNames: [] })),
  workspace: {},
  maxWatches: 16,
};
const caller = (config: Partial<VoiceSeamConfig> = {}): VoiceSeam.VoiceCaller => ({
  sessionId: AuthSessionId.make("session:voice"),
  namespace: "voice",
  config: { ...CONFIG, ...config },
});

const REQUEST = "01JD5Z7Q8M3N4P5R6S7T8V9W0X";
const launchInput = (requestId = REQUEST, projectId = PROJECT) => ({
  requestId,
  projectId,
  instruction: "Fix the flaky ASR test",
});

const commandIdOf = Effect.fn("test.commandIdOf")(function* (requestId: string) {
  return yield* voiceCommandId(yield* Digest.Digest, "launch", requestId);
});

const launchCount = Effect.gen(function* () {
  return (yield* Ref.get(yield* Launched)).length;
});

const refusalOf = <A>(effect: Effect.Effect<A, VoiceSeam.VoiceSeamRefusal>) =>
  effect.pipe(
    Effect.flip,
    Effect.map(({ code, reason, commandId, threadId }) => ({ code, reason, commandId, threadId })),
  );

const seedReceipt = Effect.fn("test.seedReceipt")(function* (input: {
  readonly commandId: CommandId;
  readonly threadId: ThreadId;
  readonly status: "accepted" | "rejected";
  readonly error: string | null;
}) {
  const receipts = yield* CommandReceiptStore.CommandReceiptStoreV2;
  yield* receipts.upsert({
    ...input,
    commandType: "thread.create",
    acceptedAt: yield* DateTime.now,
    resultSequence: 0,
  });
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
    const commandId = yield* commandIdOf(requestId);
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
    const rejectedId = yield* commandIdOf(rejected);
    yield* seedReceipt({
      commandId: rejectedId,
      threadId: ThreadId.make(rejectedId),
      status: "rejected",
      error: "Project is being cloned.",
    });
    const silent = "01JD5Z7Q8M3N4P5R6S7T8V9W3C";
    const silentId = yield* commandIdOf(silent);
    yield* seedReceipt({
      commandId: silentId,
      threadId: ThreadId.make(silentId),
      status: "rejected",
      error: null,
    });

    // Launch accepted, its first message rejected: the thread exists.
    const messageRefused = "01JD5Z7Q8M3N4P5R6S7T8V9W4D";
    const started = yield* seam.launch(caller(), launchInput(messageRefused));
    const messageRefusedId = yield* commandIdOf(messageRefused);
    const initialMessageId = CommandId.make(`${messageRefusedId}:initial-message`);
    yield* seedReceipt({
      commandId: initialMessageId,
      threadId: started.threadId,
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
    const commandId = yield* commandIdOf(requestId);
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
