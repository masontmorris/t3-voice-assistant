import {
  isRuntimeModeAbove,
  OrchestrationV2ThreadLaunchWorkspaceStrategy,
  PositiveInt,
  ProjectId,
  type ProviderInteractionMode,
  RuntimeMode,
  TrimmedNonEmptyString,
} from "@t3tools/contracts";
import * as Effect from "effect/Effect";
import * as FileSystem from "effect/FileSystem";
import * as Schema from "effect/Schema";

import { runtimeModeRank } from "../mcp/delegatedTaskModes.ts";
import {
  type DispatchModes,
  exceededDispatchModeLimit,
} from "../orchestration-v2/DispatchModeLimit.ts";

export const VOICE_OPERATIONS = [
  "list",
  "read",
  "watch",
  "launch",
  "send",
  "interrupt",
  "respond",
  "answer",
] as const;
export const VoiceOperation = Schema.Literals(VOICE_OPERATIONS);
export type VoiceOperation = typeof VoiceOperation.Type;

/** In the T3 home. The seam is off until it exists, and each new socket reads it again. */
export const VOICE_SEAM_CONFIG_FILE = "voice-seam.json";

export const VoiceSeamConfig = Schema.Struct({
  maxRuntimeMode: RuntimeMode,
  operations: Schema.Array(VoiceOperation),
  projects: Schema.Array(
    Schema.Struct({
      projectId: ProjectId,
      /** What Mason calls the project out loud. The project picker reads these. */
      spokenNames: Schema.Array(TrimmedNonEmptyString),
    }),
  ),
  /** How a launch in each project gets its workspace. A project with no entry is unit 5's call. */
  workspace: Schema.Record(ProjectId, OrchestrationV2ThreadLaunchWorkspaceStrategy).pipe(
    Schema.withDecodingDefaultKey(Effect.succeed({})),
  ),
  maxWatches: PositiveInt.pipe(Schema.withDecodingDefaultKey(Effect.succeed(16))),
})
  .check(
    Schema.makeFilter(
      (config) =>
        Object.keys(config.workspace).every((projectId) =>
          config.projects.some((project) => project.projectId === projectId),
        ),
      { expected: "a workspace entry only for a listed project" },
    ),
  )
  .annotate({ identifier: "VoiceSeamConfig" });
export type VoiceSeamConfig = typeof VoiceSeamConfig.Type;

const decodeConfig = Schema.decodeUnknownEffect(Schema.fromJsonString(VoiceSeamConfig));

export type VoiceSeamConfigRead =
  | { readonly kind: "off" }
  | { readonly kind: "invalid"; readonly cause: unknown }
  | { readonly kind: "on"; readonly config: VoiceSeamConfig };

export const readConfig = Effect.fn("VoiceSeam.readConfig")(function* (
  fs: FileSystem.FileSystem,
  file: string,
) {
  const text = yield* fs.readFileString(file).pipe(Effect.result);
  if (text._tag === "Failure") {
    return text.failure.reason._tag === "NotFound"
      ? ({ kind: "off" } as const)
      : ({ kind: "invalid", cause: text.failure } as const);
  }
  return yield* decodeConfig(text.success).pipe(
    Effect.match({
      onFailure: (cause): VoiceSeamConfigRead => ({ kind: "invalid", cause }),
      onSuccess: (config): VoiceSeamConfigRead => ({ kind: "on", config }),
    }),
  );
});

/** The one interaction mode the ceiling allows. A voice launch starts in it. */
export const CEILING_INTERACTION_MODE: ProviderInteractionMode = "default";

/** The ceiling as T3's per-command mode limit, which the orchestrator rechecks under its lock. */
export const dispatchModeLimit = (config: VoiceSeamConfig): DispatchModes => ({
  runtimeMode: config.maxRuntimeMode,
  interactionMode: CEILING_INTERACTION_MODE,
});

/** A thread whose modes are above the ceiling, so a voice command would run work in them. */
export const isAboveCeiling = (config: VoiceSeamConfig, modes: DispatchModes) =>
  exceededDispatchModeLimit(dispatchModeLimit(config), modes) !== undefined;

/**
 * Whether a message queued now still starts at or below the ceiling. T3 starts a queued run in the
 * thread's mode at that time and holds it only above its managed ceiling, so Mason can raise the
 * thread meanwhile. With no managed ceiling, any mode can start.
 */
export const queueHoldsCeiling = (
  config: VoiceSeamConfig,
  managedMaxRuntimeMode: RuntimeMode | undefined,
) => !isRuntimeModeAbove(managedMaxRuntimeMode ?? "full-access", config.maxRuntimeMode);

export const allowsOperation = (config: VoiceSeamConfig, operation: VoiceOperation) =>
  config.operations.includes(operation);

export const allowsProject = (config: VoiceSeamConfig, projectId: ProjectId) =>
  config.projects.some((project) => project.projectId === projectId);

/**
 * The mode a voice launch starts at: the project's default, lowered to the ceiling, then stepped
 * down to the broadest mode the provider offers. This is what `t3_thread_launch` picks for a
 * caller whose limit is the ceiling and who asks for no mode.
 */
export const clampRuntimeMode = (input: {
  readonly ceiling: RuntimeMode;
  readonly requested: RuntimeMode;
  readonly supported: ReadonlyArray<RuntimeMode> | undefined;
}): RuntimeMode => {
  const capped =
    runtimeModeRank(input.requested) > runtimeModeRank(input.ceiling)
      ? input.ceiling
      : input.requested;
  const { supported } = input;
  if (supported === undefined || supported.length === 0 || supported.includes(capped)) {
    return capped;
  }
  return (
    supported
      .filter((mode) => runtimeModeRank(mode) <= runtimeModeRank(capped))
      .toSorted((left, right) => runtimeModeRank(right) - runtimeModeRank(left))[0] ??
    "approval-required"
  );
};
