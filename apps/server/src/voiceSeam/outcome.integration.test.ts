import { assert, it } from "@effect/vitest";
import type { OrchestrationV2StoredEvent } from "@t3tools/contracts";
import * as Effect from "effect/Effect";

import { materializeReplayTranscriptWorkspace } from "@t3tools/provider-testing/replayTranscript";
import { ClaudeOrchestratorReplayHarness } from "../orchestration-v2/Adapters/ClaudeAdapterV2.testkit.ts";
import { CodexOrchestratorReplayHarness } from "../orchestration-v2/Adapters/CodexAdapterV2.testkit.ts";
import {
  findReplayFixture,
  runReplayFixture,
} from "../orchestration-v2/testkit/ProviderReplayFixtures.ts";
import { distillAll, FIXTURES, runStatuses } from "./testFixtures.ts";

const variant = (name: string, driver: string) => {
  const fixture = findReplayFixture(name)!;
  const provider = fixture.providers.find((candidate) => candidate.driver === driver)!;
  // Each recording keeps fewer steps than the shared fixture, so its output checks do not apply.
  return { fixture, provider: { ...provider, assertOutput: () => undefined } };
};

/** The watched thread's events: the first thread the recording created. */
const rootThread = (stored: ReadonlyArray<OrchestrationV2StoredEvent>) => {
  const threadId = stored.find((event) => event.event.type === "thread.created")!.event.threadId;
  return stored.filter((event) => event.event.threadId === threadId);
};

const codexWorkspace = (
  transcript: Parameters<typeof materializeReplayTranscriptWorkspace>[0],
  workspace: string,
) => Effect.succeed(materializeReplayTranscriptWorkspace(transcript, workspace));

/** The Claude result_is_error replay through its first turn, which fails; the follow-up turn is cut. */
const recordFailedRun = Effect.gen(function* () {
  const { fixture, provider } = variant("claude_result_is_error", "claudeAgent");
  const result = yield* runReplayFixture({
    fixtureName: fixture.name,
    buildInput: fixture.buildInput,
    driver: provider,
    harness: ClaudeOrchestratorReplayHarness,
  });
  const events = rootThread(result.storedEvents);
  const failed = events.find(
    (stored) => stored.event.type === "run.updated" && stored.event.payload.status === "failed",
  )!;
  return events.filter((stored) => stored.sequence <= failed.sequence);
});

/** The Codex thread_rollback replay through its rollback, which rolls back the second turn. */
const recordRolledBack = Effect.gen(function* () {
  const { fixture, provider } = variant("thread_rollback", "codex");
  const steps = fixture.buildInput().steps;
  const result = yield* runReplayFixture({
    fixtureName: fixture.name,
    buildInput: () => ({
      ...fixture.buildInput(),
      steps: [
        ...steps.slice(0, steps.findIndex((step) => step.type === "rollback") + 1),
        { type: "await_run_status", targetRunIndex: 2, status: "rolled_back" },
      ],
    }),
    driver: provider,
    harness: CodexOrchestratorReplayHarness,
    materializeWorkspace: codexWorkspace,
  });
  return rootThread(result.storedEvents);
});

/** A Claude root turn that ends while its background subagent works, then wakes on its report. */
const recordSubagentAfterRoot = Effect.gen(function* () {
  const { fixture, provider } = variant("claude_background_subagent_after_root", "claudeAgent");
  const result = yield* runReplayFixture({
    fixtureName: fixture.name,
    buildInput: fixture.buildInput,
    driver: provider,
    harness: ClaudeOrchestratorReplayHarness,
  });
  return rootThread(result.storedEvents);
});

/** A follow-up queued behind a running turn and cancelled before it starts. */
const recordQueuedCancelled = Effect.gen(function* () {
  const { fixture, provider } = variant("queued_cancelled_while_active", "codex");
  const result = yield* runReplayFixture({
    fixtureName: fixture.name,
    buildInput: fixture.buildInput,
    driver: provider,
    harness: CodexOrchestratorReplayHarness,
    materializeWorkspace: codexWorkspace,
  });
  return rootThread(result.storedEvents);
});

it.effect.each([
  { name: "failedRun", record: recordFailedRun },
  { name: "rolledBack", record: recordRolledBack },
  { name: "subagentAfterRoot", record: recordSubagentAfterRoot },
  { name: "queuedCancelled", record: recordQueuedCancelled },
] as const)("a fresh $name recording still matches its committed fixture", ({ name, record }) =>
  Effect.gen(function* () {
    const recorded = yield* record;
    assert.deepStrictEqual(distillAll(recorded), distillAll(FIXTURES[name]));
    assert.deepStrictEqual(runStatuses(recorded), runStatuses(FIXTURES[name]));
  }),
);
