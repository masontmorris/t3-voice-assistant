import * as NodeCrypto from "@effect/platform-node/NodeCrypto";
import * as NodeServices from "@effect/platform-node/NodeServices";
import {
  AuthSessionId,
  type CommandId,
  type ModelSelection,
  ProjectId,
  ProviderDriverKind,
  type ServerProvider,
  type ThreadId,
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

import * as CheckpointStore from "../checkpointing/CheckpointStore.ts";
import * as GitWorkflow from "../git/GitWorkflowService.ts";
import { CodexProviderCapabilitiesV2 } from "../orchestration-v2/Adapters/CodexAdapterV2.ts";
import * as CommandReceiptStore from "../orchestration-v2/CommandReceiptStore.ts";
import * as LegacyV1ThreadImporter from "../orchestration-v2/legacy/LegacyV1ThreadImporter.ts";
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
import { ThreadWorktreeRestore } from "../workspace/restoreMissingWorktree.ts";
import { layerNoTaskFolders } from "../orchestration-v2/ProviderTurnStartService.testkit.ts";
import { VOICE_OPERATIONS, type VoiceSeamConfig } from "./ceiling.ts";
import { voiceCommandId } from "./launch.ts";
import * as VoiceSeam from "./VoiceSeam.ts";

// The real orchestrator, ThreadLaunchService and ThreadManagementService, for the seam's writes.
// No provider turn runs: a launched run waits in preparation, which is all the receipt gates need.

export const PROJECT = ProjectId.make("project:voice-sandbox");
export const NO_MODEL = ProjectId.make("project:no-model");
export const OUTSIDE = ProjectId.make("project:outside");
export const REPLAY: ModelSelection = { instanceId: DEV_REPLAY_INSTANCE_ID, model: "showcase" };

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
  openSession: () => Effect.die("no provider turn runs in the write tests"),
} as ProviderAdapter.ProviderAdapterV2["Service"];

/** Every input the seam handed to ThreadLaunchService.launch. */
export class Launched extends Context.Service<
  Launched,
  Ref.Ref<ReadonlyArray<ThreadLaunch.ThreadLaunchInput>>
>()("t3/voiceSeam/testWriteLayer/Launched") {}

/** Each worktree restore a send ran, and a step a test runs inside the next ones. */
export class Restores extends Context.Service<
  Restores,
  {
    readonly threadIds: Ref.Ref<ReadonlyArray<ThreadId>>;
    readonly during: Ref.Ref<Effect.Effect<void>>;
  }
>()("t3/voiceSeam/testWriteLayer/Restores") {}

/** A step a test runs once, inside the next call the seam makes to interruptThread. */
export class BeforeInterrupt extends Context.Service<
  BeforeInterrupt,
  Ref.Ref<Effect.Effect<void>>
>()("t3/voiceSeam/testWriteLayer/BeforeInterrupt") {}

const layerDatabase = Sqlite.layerMemory;
const layerOrchestrator = ProviderReplayHarness.layerWithRegistry(
  { name: "voice-writes" },
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
const layerRestores = Layer.effect(
  Restores,
  Effect.all({
    threadIds: Ref.make<ReadonlyArray<ThreadId>>([]),
    during: Ref.make<Effect.Effect<void>>(Effect.void),
  }),
);
const layerCountedRestore = Layer.effect(
  ThreadWorktreeRestore,
  Effect.gen(function* () {
    const restores = yield* Restores;
    return {
      restore: Effect.fn("test.ThreadWorktreeRestore.restore")(function* (thread) {
        yield* Ref.update(restores.threadIds, (ids) => [...ids, thread.id]);
        yield* Effect.flatten(Ref.get(restores.during));
      }),
      takeRecreated: Effect.fn("test.ThreadWorktreeRestore.takeRecreated")(function () {
        return Effect.undefined;
      }),
    };
  }),
);
const layerNoLegacyImports = Layer.succeed(
  LegacyV1ThreadImporter.LegacyV1ThreadImporter,
  LegacyV1ThreadImporter.LegacyV1ThreadImporter.of({
    pendingThreadCount: Effect.succeed(0),
    reconcileShells: Effect.succeed({ importedThreadCount: 0, importedMessageCount: 0 }),
    ensureTranscript: Effect.fn("test.LegacyV1ThreadImporter.ensureTranscript")(function () {
      return Effect.succeed({ importedThreadCount: 0, importedMessageCount: 0 });
    }),
    importPendingTranscripts: Effect.succeed({ importedThreadCount: 0, importedMessageCount: 0 }),
  }),
);
const layerThreads = ThreadManagement.layerWithLegacyImporter.pipe(
  Layer.provide(
    Layer.mergeAll(
      layerOrchestrator,
      layerSettings,
      layerReceipts,
      layerNoLegacyImports,
      layerCountedRestore,
    ),
  ),
);
const layerHookedThreads = Layer.effect(
  ThreadManagement.ThreadManagementService,
  Effect.gen(function* () {
    const real = yield* ThreadManagement.ThreadManagementService;
    const before = yield* BeforeInterrupt;
    return {
      ...real,
      interruptThread: (input) =>
        Effect.flatten(Ref.getAndSet(before, Effect.void)).pipe(
          Effect.andThen(real.interruptThread(input)),
        ),
    } satisfies ThreadManagement.ThreadManagementService["Service"];
  }),
).pipe(Layer.provide(layerThreads));
const layerProjects = Layer.mock(ProjectService.ProjectService)({
  getById: (id) => Effect.succeed(Option.fromNullishOr(PROJECTS.get(id))),
});
const layerExternal = Layer.mergeAll(
  layerNoTaskFolders,
  WorktreeSetupTracker.layer,
  Layer.mock(ProjectCloneTracker.ProjectCloneTracker)({ get: () => Effect.succeed(null) }),
  Layer.mock(TerminalManager.TerminalManager)({ close: () => Effect.void }),
  Layer.mock(CheckpointStore.CheckpointStore)({ isGitRepository: () => Effect.succeed(true) }),
  Layer.mock(GitWorkflow.GitWorkflowService)({ currentBranch: () => Effect.succeed(null) }),
  Layer.succeed(ProjectSetupScriptRunner.ProjectSetupScriptRunner, {
    runForThread: Effect.fn("test.ProjectSetupScriptRunner.runForThread")(function () {
      return Effect.succeed({ status: "no-script" as const });
    }),
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
export const layerTest = VoiceSeam.layer.pipe(
  Layer.provideMerge(layerCountedLaunches),
  Layer.provideMerge(
    Layer.mergeAll(
      layerHookedThreads,
      layerReceipts,
      layerExternal,
      OrchestrationEventStore.layer,
      ProjectStore.layer,
      NodeCrypto.layer,
    ),
  ),
  Layer.provideMerge(
    Layer.mergeAll(
      Layer.effect(Launched, Ref.make<ReadonlyArray<ThreadLaunch.ThreadLaunchInput>>([])),
      layerRestores,
      Layer.effect(BeforeInterrupt, Ref.make<Effect.Effect<void>>(Effect.void)),
    ),
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
export const caller = (config: Partial<VoiceSeamConfig> = {}): VoiceSeam.VoiceCaller => ({
  sessionId: AuthSessionId.make("session:voice"),
  namespace: "voice",
  config: { ...CONFIG, ...config },
});

export const commandIdOf = Effect.fn("test.commandIdOf")(function* (
  operation: "launch" | "send" | "interrupt",
  requestId: string,
) {
  return yield* voiceCommandId(yield* Digest.Digest, operation, requestId);
});

export const launchCount = Effect.gen(function* () {
  return (yield* Ref.get(yield* Launched)).length;
});

/** The threads whose worktree a send restored, in order. */
export const restored = Effect.gen(function* () {
  return yield* Ref.get((yield* Restores).threadIds);
});

export const refusalOf = <A>(effect: Effect.Effect<A, VoiceSeam.VoiceSeamRefusal>) =>
  effect.pipe(
    Effect.flip,
    Effect.map(({ code, reason, commandId, threadId }) => ({ code, reason, commandId, threadId })),
  );

export const seedReceipt = Effect.fn("test.seedReceipt")(function* (input: {
  readonly commandId: CommandId;
  readonly threadId: ThreadId;
  readonly commandType: CommandReceiptStore.CommandReceiptV2["commandType"];
  readonly status: "accepted" | "rejected";
  readonly error: string | null;
}) {
  const receipts = yield* CommandReceiptStore.CommandReceiptStoreV2;
  yield* receipts.upsert({ ...input, acceptedAt: yield* DateTime.now, resultSequence: 0 });
});
