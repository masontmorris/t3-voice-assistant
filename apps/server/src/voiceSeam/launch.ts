import {
  CommandId,
  MessageId,
  type ModelSelection,
  type ProjectId,
  type ProviderInteractionMode,
  type RuntimeMode,
  type ServerProvider,
  ThreadId,
} from "@t3tools/contracts";
import { resolveProjectSettings } from "@t3tools/shared/projectSettings";
import { resolveNewThreadModelSelection } from "@t3tools/shared/usageLimits";
import * as Digest from "effect/crypto/Digest";
import * as DateTime from "effect/DateTime";
import * as Effect from "effect/Effect";
import * as Option from "effect/Option";
import * as Schema from "effect/Schema";

import type * as CommandReceiptStore from "../orchestration-v2/CommandReceiptStore.ts";
import { OrchestratorRuntimeModeAboveCeilingError } from "../orchestration-v2/Orchestrator.ts";
import type * as ThreadLaunch from "../orchestration-v2/ThreadLaunchService.ts";
import type * as ThreadManagementService from "../orchestration-v2/ThreadManagementService.ts";
import type * as ProjectService from "../project/ProjectService.ts";
import type * as ProviderRegistry from "../provider/ProviderRegistry.ts";
import type * as ServerSettings from "../serverSettings.ts";
import { CEILING_INTERACTION_MODE, clampRuntimeMode, type VoiceSeamConfig } from "./ceiling.ts";
import type { RequestInput, VoiceLaunchResult } from "./protocol.ts";
import { VoiceSeamRefusal } from "./refusal.ts";

/** What a voice launch starts with: what `t3_thread_launch` picks for a caller at the ceiling. */
interface LaunchPolicy {
  readonly modelSelection: ModelSelection;
  /** The model's display name. */
  readonly model: string;
  readonly runtimeMode: RuntimeMode;
  readonly interactionMode: ProviderInteractionMode;
  readonly workspaceStrategy: ThreadLaunch.ThreadLaunchWorkspaceStrategy;
}

/** As `t3 thread` titles a thread from its message. */
const TITLE_MAX_CHARS = 80;

/**
 * The command a write derives from its request id alone, so a retry names the same command and
 * thread. It is the MCP launch id's shape, `mcp:launch:<sha256 hex>`, with a voice prefix. A write that answers
 * one pending request passes it as `scope`: a receipt does not hold the request, so the command is minted for it,
 * and a retry that names another request has no receipt to replay.
 */
export const voiceCommandId = (
  digest: Digest.Digest["Service"],
  operation: "launch" | "send" | "interrupt" | "respond" | "answer",
  requestId: string,
  scope?: string,
) =>
  digest
    .digestHex(
      Digest.sha256,
      new TextEncoder().encode(
        JSON.stringify(
          scope === undefined
            ? ["voice", operation, requestId]
            : ["voice", operation, requestId, scope],
        ),
      ),
    )
    .pipe(Effect.map((hex) => CommandId.make(`voice:${operation}:${hex}`)));

const initialMessageCommandId = (commandId: CommandId) =>
  CommandId.make(`${commandId}:initial-message`);

type ReceiptGate =
  /** No receipt: this request has not launched. */
  | { readonly kind: "launch" }
  /** T3 refused the launch. No thread exists. */
  | { readonly kind: "refused"; readonly reason: string | null }
  /** The thread exists, and T3 refused its first message. */
  | { readonly kind: "message_refused"; readonly reason: string | null }
  /** The launch was accepted: answer from the thread as it is now. */
  | { readonly kind: "replay" };

/** The receipt table of docs/voice/seam-design.md, unit 5. A message receipt counts only after an accepted launch. */
const receiptGate = (
  launch: Option.Option<CommandReceiptStore.CommandReceiptV2>,
  message: Option.Option<CommandReceiptStore.CommandReceiptV2>,
): ReceiptGate => {
  if (Option.isNone(launch)) return { kind: "launch" };
  if (launch.value.status === "rejected") return { kind: "refused", reason: launch.value.error };
  if (Option.isSome(message) && message.value.status === "rejected") {
    return { kind: "message_refused", reason: message.value.error };
  }
  return { kind: "replay" };
};

const titleOf = (instruction: string) => {
  const firstLine = instruction.trim().split("\n")[0]?.trim() ?? "";
  return firstLine.length <= TITLE_MAX_CHARS
    ? firstLine
    : `${firstLine.slice(0, TITLE_MAX_CHARS - 1).trimEnd()}…`;
};

const displayName = (providers: ReadonlyArray<ServerProvider>, selection: ModelSelection) =>
  providers
    .find((provider) => provider.instanceId === selection.instanceId)
    ?.models.find((model) => model.slug === selection.model)?.name ?? selection.model;

const isAboveCeiling = Schema.is(OrchestratorRuntimeModeAboveCeilingError);

export interface LaunchSources {
  readonly digest: Digest.Digest["Service"];
  readonly threads: ThreadManagementService.ThreadManagementService["Service"];
  readonly launches: ThreadLaunch.ThreadLaunchService["Service"];
  readonly receipts: CommandReceiptStore.CommandReceiptStoreV2["Service"];
  readonly projects: ProjectService.ProjectService["Service"];
  readonly settings: ServerSettings.ServerSettingsService["Service"];
  readonly providers: ProviderRegistry.ProviderRegistry["Service"];
}

/** Launches for a caller whose operation and project the service already checked. */
export const make = (sources: LaunchSources) => {
  const { threads, launches, receipts } = sources;

  /**
   * The composition `t3_thread_launch` runs for a caller outside a thread: the project's effective
   * defaults, the configured workspace, and the ceiling. The parity test holds the two together.
   */
  const resolveLaunchPolicy = Effect.fn("VoiceSeam.resolveLaunchPolicy")(function* (
    config: VoiceSeamConfig,
    projectId: ProjectId,
  ) {
    const project = yield* sources.projects
      .getById(projectId)
      .pipe(Effect.mapError(VoiceSeamRefusal.unavailable));
    if (Option.isNone(project)) {
      return yield* VoiceSeamRefusal.notFound("I can't find that project.");
    }
    const settings = yield* sources.settings.getSettings.pipe(
      Effect.mapError(VoiceSeamRefusal.unavailable),
    );
    const defaults = resolveProjectSettings(settings, projectId, project.value).settings;
    const configured = config.workspace[projectId] ?? { type: "root" };
    // An omitted startFromOrigin follows the new-worktree setting, as t3_thread_launch does.
    const workspaceStrategy: ThreadLaunch.ThreadLaunchWorkspaceStrategy =
      configured.type === "worktree" && configured.startFromOrigin === undefined
        ? { ...configured, startFromOrigin: defaults.newWorktreesStartFromOrigin }
        : configured;
    if (workspaceStrategy.type === "worktree") {
      yield* launches.checkWorktreeBase({ projectId, workspaceStrategy }).pipe(
        Effect.mapError(
          (cause) =>
            new VoiceSeamRefusal({
              code: "invalid_request",
              message: "T3 can't make a worktree for that project.",
              cause,
            }),
        ),
      );
    }
    const providers = yield* sources.providers.getProviders;
    const modelSelection = resolveNewThreadModelSelection({
      defaultSelection: defaults.defaultModelSelection,
      fallbackSelection: defaults.fallbackModelSelection,
      providers,
      settings: defaults,
      projectId,
      now: DateTime.toEpochMillis(yield* DateTime.now),
    }).selection;
    if (modelSelection === null) return yield* VoiceSeamRefusal.noDefaultModel();
    return {
      modelSelection,
      model: displayName(providers, modelSelection),
      runtimeMode: clampRuntimeMode({
        ceiling: config.maxRuntimeMode,
        requested: defaults.defaultRuntimeMode,
        supported: providers.find((provider) => provider.instanceId === modelSelection.instanceId)
          ?.supportedRuntimeModes,
      }),
      interactionMode: CEILING_INTERACTION_MODE,
      workspaceStrategy,
    } satisfies LaunchPolicy;
  });

  const readGate = Effect.fn("VoiceSeam.readLaunchGate")(function* (commandId: CommandId) {
    const launch = yield* receipts.getByCommandId(commandId);
    const message = Option.isSome(launch)
      ? yield* receipts.getByCommandId(initialMessageCommandId(commandId))
      : Option.none();
    return receiptGate(launch, message);
  }, Effect.mapError(VoiceSeamRefusal.unavailable));

  /** An accepted launch, answered from the thread as it is now. It never calls `launch`. */
  const replay = Effect.fn("VoiceSeam.replayLaunch")(function* (
    input: RequestInput<"launch">,
    ids: { readonly threadId: ThreadId; readonly messageId: MessageId },
  ) {
    const unavailable = Effect.mapError(VoiceSeamRefusal.unavailable);
    // Read first, so anything the thread commits after it is in a watch from it.
    const sequence = yield* threads.getThreadEventSequence(ids.threadId).pipe(unavailable);
    const shell = yield* threads.getThreadShell(ids.threadId).pipe(unavailable);
    if (shell === null || shell.deletedAt !== null) return yield* VoiceSeamRefusal.gone();
    if (shell.projectId !== input.projectId) {
      return yield* VoiceSeamRefusal.invalidRequest(
        "That request already started a thread in another project.",
      );
    }
    const { messages } = yield* threads
      .getThreadRecords(ids.threadId, ["messages"], { messageIds: [ids.messageId] })
      .pipe(unavailable);
    const providers = yield* sources.providers.getProviders;
    return {
      threadId: ids.threadId,
      runId: messages.find((message) => message.id === ids.messageId)?.runId ?? null,
      runtimeMode: shell.runtimeMode,
      model: displayName(providers, shell.modelSelection),
      sequence,
      replayed: true,
    } satisfies VoiceLaunchResult;
  });

  const answer = (
    gate: Exclude<ReceiptGate, { readonly kind: "launch" }>,
    input: RequestInput<"launch">,
    ids: {
      readonly commandId: CommandId;
      readonly threadId: ThreadId;
      readonly messageId: MessageId;
    },
  ): Effect.Effect<VoiceLaunchResult, VoiceSeamRefusal> => {
    switch (gate.kind) {
      case "refused":
        return Effect.fail(
          VoiceSeamRefusal.refused({ reason: gate.reason, commandId: ids.commandId }),
        );
      case "message_refused":
        return Effect.fail(
          VoiceSeamRefusal.refused({
            reason: gate.reason,
            commandId: initialMessageCommandId(ids.commandId),
            threadId: ids.threadId,
          }),
        );
      case "replay":
        return replay(input, ids);
    }
  };

  return Effect.fn("VoiceSeam.launchThread")(function* (
    config: VoiceSeamConfig,
    input: RequestInput<"launch">,
  ) {
    const commandId = yield* voiceCommandId(sources.digest, "launch", input.requestId);
    const ids = {
      commandId,
      threadId: ThreadId.make(commandId),
      messageId: MessageId.make(commandId),
    };
    // A request with a receipt never launches again: a launch replay inside ThreadLaunchService
    // reads the command's stored events, which the event log prunes.
    const gate = yield* readGate(commandId);
    if (gate.kind !== "launch") return yield* answer(gate, input, ids);
    const policy = yield* resolveLaunchPolicy(config, input.projectId);
    const launched = yield* launches
      .launch({
        commandId,
        threadId: ids.threadId,
        projectId: input.projectId,
        title: titleOf(input.instruction),
        // A spoken instruction makes a long title, and the voice says a thread's title back.
        generateTitle: true,
        modelSelection: policy.modelSelection,
        runtimeMode: policy.runtimeMode,
        interactionMode: policy.interactionMode,
        workspaceStrategy: policy.workspaceStrategy,
        initialMessage: { messageId: ids.messageId, text: input.instruction, attachments: [] },
        createdBy: "user",
        creationSource: "server",
      })
      .pipe(Effect.result);
    if (launched._tag === "Failure") {
      const error = launched.failure;
      if (error._tag === "ThreadManagementProviderInstanceDisabledError") {
        return yield* VoiceSeamRefusal.noDefaultModel();
      }
      // T3's managed ceiling leaves no receipt, so a retry runs the same check again.
      if (isAboveCeiling(error.cause)) return yield* VoiceSeamRefusal.aboveCeiling(error);
      // A launch that failed after T3 recorded its command answers as its retry would.
      const after = yield* readGate(commandId);
      if (after.kind === "launch") return yield* VoiceSeamRefusal.unavailable(error);
      return yield* answer(after, input, ids);
    }
    const { projection, resumed } = launched.success;
    const sequence =
      resumed || launched.success.sequence === undefined
        ? yield* threads
            .getThreadEventSequence(ids.threadId)
            .pipe(Effect.mapError(VoiceSeamRefusal.unavailable))
        : launched.success.sequence;
    return {
      threadId: launched.success.threadId,
      runId: projection.runs.find((run) => run.userMessageId === ids.messageId)?.id ?? null,
      runtimeMode: projection.thread.runtimeMode,
      model: policy.model,
      sequence,
      replayed: resumed,
    } satisfies VoiceLaunchResult;
  });
};
