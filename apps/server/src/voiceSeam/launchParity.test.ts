import * as NodeCrypto from "@effect/platform-node/NodeCrypto";
import * as NodeServices from "@effect/platform-node/NodeServices";
import { assert, it } from "@effect/vitest";
import {
  EnvironmentId,
  type ModelSelection,
  ProjectId,
  type Project as ProjectRecord,
  ProviderDriverKind,
  ProviderInstanceId,
  type RuntimeMode,
  type ServerProvider,
} from "@t3tools/contracts";
import * as Digest from "effect/crypto/Digest";
import * as Effect from "effect/Effect";
import * as Layer from "effect/Layer";
import * as Option from "effect/Option";
import * as Stream from "effect/Stream";

import * as ServerConfig from "../config.ts";
import * as McpInvocationContext from "../mcp/McpInvocationContext.ts";
import * as McpToolAccess from "../mcp/McpToolAccess.ts";
import * as ProjectHandlers from "../mcp/toolkits/project/handlers.ts";
import { ProjectToolkit } from "../mcp/toolkits/project/tools.ts";
import * as ThreadLaunch from "../orchestration-v2/ThreadLaunchService.ts";
import * as ThreadManagement from "../orchestration-v2/ThreadManagementService.ts";
import * as PeerForwarding from "../peer/PeerForwarding.ts";
import * as ManagedProjectFolders from "../project/ManagedProjectFolders.ts";
import * as Project from "../project/ProjectService.ts";
import * as DocumentText from "../provider/DocumentText.ts";
import { DEV_REPLAY_INSTANCE_ID } from "../provider/ProviderInstanceRegistryHydration.ts";
import * as ProviderRegistry from "../provider/ProviderRegistry.ts";
import * as ProviderRegistryMock from "../provider/testUtils/providerRegistryMock.ts";
import * as ServerSettings from "../serverSettings.ts";
import * as GitVcsDriver from "../vcs/GitVcsDriver.ts";
import * as VcsProcess from "../vcs/VcsProcess.ts";
import type { VoiceSeamConfig } from "./ceiling.ts";
import * as Launch from "./launch.ts";

// A voice launch must start as t3_thread_launch would for an MCP client whose access is the
// voice ceiling. Both run over the same project, settings and providers, and each hands its
// launch input to the same recorder.

const PROJECT = ProjectId.make("project:parity");
const REPLAY: ModelSelection = { instanceId: DEV_REPLAY_INSTANCE_ID, model: "showcase" };
const OTHER: ModelSelection = { instanceId: ProviderInstanceId.make("codex"), model: "gpt-5.4" };

const provider = (
  selection: ModelSelection,
  supportedRuntimeModes?: ReadonlyArray<RuntimeMode>,
): ServerProvider => ({
  instanceId: selection.instanceId,
  driver: ProviderDriverKind.make("acpRegistry"),
  displayName: "Replay (dev)",
  enabled: true,
  installed: true,
  status: "ready",
  version: null,
  auth: { status: "authenticated" },
  checkedAt: "2026-11-17T00:00:00Z",
  models: [{ slug: selection.model, name: "Showcase", isCustom: false, capabilities: null }],
  slashCommands: [],
  skills: [],
  ...(supportedRuntimeModes === undefined ? {} : { supportedRuntimeModes }),
});

const REPLAY_INSTANCE = {
  displayName: "Replay (dev)",
  driver: ProviderDriverKind.make("acpRegistry"),
  config: { source: "local" },
};

interface ParityCase {
  readonly name: string;
  readonly ceiling: RuntimeMode;
  readonly defaultRuntimeMode: RuntimeMode;
  readonly supportedRuntimeModes?: ReadonlyArray<RuntimeMode>;
  /** The project's own legacy default model field. */
  readonly projectModel: ModelSelection | null;
  /** Where Settings saves a project's model once project settings are folded. */
  readonly overrideModel?: ModelSelection;
  /** The voice-seam.json entry, which the MCP call passes as its workspaceStrategy. */
  readonly workspace?:
    | { readonly type: "root" }
    | { readonly type: "worktree"; readonly baseRef: string; readonly startFromOrigin?: boolean };
  readonly newWorktreesStartFromOrigin?: boolean;
  /** The runtime mode both must pick, so a shared regression cannot pass as parity. */
  readonly runtimeMode: RuntimeMode;
}

const CASES: ReadonlyArray<ParityCase> = [
  {
    name: "a project default above the ceiling starts at the ceiling",
    ceiling: "auto-accept-edits",
    defaultRuntimeMode: "full-access",
    projectModel: REPLAY,
    runtimeMode: "auto-accept-edits",
  },
  {
    name: "a project default below the ceiling stays",
    ceiling: "auto-accept-edits",
    defaultRuntimeMode: "approval-required",
    projectModel: REPLAY,
    runtimeMode: "approval-required",
  },
  {
    name: "a provider without the ceiling's mode steps down",
    ceiling: "auto",
    defaultRuntimeMode: "full-access",
    supportedRuntimeModes: ["approval-required", "auto-accept-edits", "full-access"],
    projectModel: REPLAY,
    runtimeMode: "auto-accept-edits",
  },
  {
    name: "the folded project setting's model wins over the legacy field",
    ceiling: "auto-accept-edits",
    defaultRuntimeMode: "auto-accept-edits",
    projectModel: OTHER,
    overrideModel: REPLAY,
    runtimeMode: "auto-accept-edits",
  },
  {
    name: "a configured worktree takes startFromOrigin from the new-worktree setting",
    ceiling: "auto-accept-edits",
    defaultRuntimeMode: "auto-accept-edits",
    projectModel: REPLAY,
    workspace: { type: "worktree", baseRef: "main" },
    newWorktreesStartFromOrigin: true,
    runtimeMode: "auto-accept-edits",
  },
  {
    name: "a configured worktree keeps its own startFromOrigin",
    ceiling: "auto-accept-edits",
    defaultRuntimeMode: "auto-accept-edits",
    projectModel: REPLAY,
    workspace: { type: "worktree", baseRef: "main", startFromOrigin: false },
    newWorktreesStartFromOrigin: true,
    runtimeMode: "auto-accept-edits",
  },
];

const dependencies = (input: ParityCase, launched: Array<ThreadLaunch.ThreadLaunchInput>) =>
  Layer.mergeAll(
    ProviderRegistryMock.layer([provider(REPLAY, input.supportedRuntimeModes), provider(OTHER)]),
    ServerSettings.layerTest({
      // As the dev server seeds it. An instance missing here reads as disabled.
      providerInstances: { [DEV_REPLAY_INSTANCE_ID]: REPLAY_INSTANCE },
      defaultRuntimeMode: input.defaultRuntimeMode,
      ...(input.newWorktreesStartFromOrigin === undefined
        ? {}
        : { newWorktreesStartFromOrigin: input.newWorktreesStartFromOrigin }),
      ...(input.overrideModel === undefined
        ? {}
        : {
            projectSettingsFolded: true,
            projectSettingsOverrides: { [PROJECT]: { defaultModelSelection: input.overrideModel } },
          }),
    }),
    Layer.succeed(McpInvocationContext.McpInvocationContext, {
      environmentId: EnvironmentId.make("environment"),
      requestNamespace: "client:parity",
      thread: undefined,
      client: {
        sessionId: "session-parity",
        label: "Parity client",
        access: input.ceiling,
        hostedAppUrl: undefined,
      },
      issuedAt: 0,
      capabilities: new Set(["orchestration" as const]),
    }),
    Layer.mock(ThreadManagement.ThreadManagementService)({
      getThreadShell: () => Effect.succeed(null),
    }),
    Layer.mock(ThreadLaunch.ThreadLaunchService)({
      checkWorktreeBase: () => Effect.void,
      launch: (launch) => {
        launched.push(launch);
        return Effect.succeed({
          threadId: launch.threadId,
          projection: {
            thread: {
              id: launch.threadId,
              projectId: launch.projectId,
              title: launch.title,
              modelSelection: launch.modelSelection,
              runtimeMode: launch.runtimeMode,
            },
            runs: [],
          },
          resumed: false,
          sequence: 1,
        } as unknown as ThreadLaunch.ThreadLaunchResult);
      },
    }),
    Layer.mock(Project.ProjectService)({
      getById: (id) =>
        Effect.succeed(
          id === PROJECT
            ? Option.some({
                id,
                workspaceRoot: "/projects/parity",
                defaultModelSelection: input.projectModel,
              } as unknown as ProjectRecord)
            : Option.none(),
        ),
    }),
    Layer.mock(PeerForwarding.PeerForwarding)({}),
    Layer.mock(ManagedProjectFolders.ManagedProjectFolders)({
      namedProjectsRoot: "/projects",
      isScratchProject: () => Effect.succeed(false),
    }),
    DocumentText.layer.pipe(Layer.provideMerge(NodeServices.layer)),
  ).pipe(
    Layer.provideMerge(GitVcsDriver.layer),
    Layer.provideMerge(VcsProcess.layer),
    Layer.provideMerge(
      ServerConfig.layerTest(process.cwd(), { prefix: "t3-voice-launch-parity-" }).pipe(
        Layer.provide(NodeServices.layer),
      ),
    ),
    Layer.provideMerge(Layer.mergeAll(NodeServices.layer, NodeCrypto.layer)),
  );

const policyOf = (launch: ThreadLaunch.ThreadLaunchInput | undefined) => ({
  modelSelection: launch?.modelSelection,
  runtimeMode: launch?.runtimeMode,
  interactionMode: launch?.interactionMode,
  workspaceStrategy: launch?.workspaceStrategy,
});

it.effect.each(CASES)("launches as t3_thread_launch does: $name", (input) =>
  Effect.gen(function* () {
    const viaMcp: Array<ThreadLaunch.ThreadLaunchInput> = [];
    const mcpDependencies = dependencies(input, viaMcp);
    const toolkit = yield* ProjectToolkit.pipe(
      Effect.provide(
        McpToolAccess.HandlersLayer.layer(ProjectHandlers.layer).pipe(
          Layer.provide(mcpDependencies),
        ),
      ),
    );
    yield* toolkit
      .handle("t3_thread_launch", {
        projectId: PROJECT,
        title: "Fix the flaky test",
        message: "Fix the flaky test",
        ...(input.workspace === undefined ? {} : { workspaceStrategy: input.workspace }),
      })
      .pipe(Stream.unwrap, Stream.runCollect, Effect.provide(mcpDependencies));

    const viaSeam: Array<ThreadLaunch.ThreadLaunchInput> = [];
    const config: VoiceSeamConfig = {
      maxRuntimeMode: input.ceiling,
      operations: ["launch"],
      projects: [{ projectId: PROJECT, spokenNames: [] }],
      workspace: input.workspace === undefined ? {} : { [PROJECT]: input.workspace },
      maxWatches: 16,
    };
    const launched = yield* Effect.gen(function* () {
      const launchThread = Launch.make({
        digest: yield* Digest.Digest,
        threads: yield* ThreadManagement.ThreadManagementService,
        launches: yield* ThreadLaunch.ThreadLaunchService,
        receipts: {
          getByCommandId: () => Effect.succeedNone,
          getProjectByCommandId: () => Effect.succeedNone,
          insertIfAbsent: () => Effect.die("the seam never writes receipts"),
          upsert: () => Effect.die("the seam never writes receipts"),
        },
        projects: yield* Project.ProjectService,
        settings: yield* ServerSettings.ServerSettingsService,
        providers: yield* ProviderRegistry.ProviderRegistry,
      });
      return yield* launchThread(config, {
        requestId: "01JD0000000000000000000000",
        projectId: PROJECT,
        instruction: "Fix the flaky test",
      });
    }).pipe(Effect.provide(dependencies(input, viaSeam)));

    assert.lengthOf(viaMcp, 1);
    assert.lengthOf(viaSeam, 1);
    assert.deepStrictEqual(policyOf(viaSeam[0]), policyOf(viaMcp[0]));
    assert.strictEqual(viaSeam[0]?.runtimeMode, input.runtimeMode);
    assert.strictEqual(launched.runtimeMode, input.runtimeMode);
  }),
);
