import {
  OrchestrationV2ThreadLaunchWorkspaceStrategy,
  PositiveInt,
  ProjectId,
  RuntimeMode,
  TrimmedNonEmptyString,
} from "@t3tools/contracts";
import * as Effect from "effect/Effect";
import * as FileSystem from "effect/FileSystem";
import * as Schema from "effect/Schema";

export const VOICE_OPERATIONS = ["list", "read", "watch", "launch", "send", "interrupt"] as const;
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

export const allowsOperation = (config: VoiceSeamConfig, operation: VoiceOperation) =>
  config.operations.includes(operation);

export const allowsProject = (config: VoiceSeamConfig, projectId: ProjectId) =>
  config.projects.some((project) => project.projectId === projectId);
