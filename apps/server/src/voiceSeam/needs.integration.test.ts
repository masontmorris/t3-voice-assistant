import { assert, it } from "@effect/vitest";
import {
  CommandId,
  EnvironmentId,
  ProviderReplayTranscript,
  TurnItemId,
  type OrchestrationV2StoredEvent,
} from "@t3tools/contracts";
import * as Effect from "effect/Effect";
import * as Schema from "effect/Schema";
import * as Stream from "effect/Stream";

import * as IdAllocator from "@t3tools/provider-core/server/IdAllocator";
import { materializeReplayTranscriptWorkspace } from "@t3tools/provider-testing/replayTranscript";
import { checkpointWorkspace } from "@t3tools/provider-testing/replayWorkspace";
import { AcpRegistryOrchestratorReplayHarness } from "../orchestration-v2/Adapters/AcpRegistryAdapterV2.testkit.ts";
import { CodexOrchestratorReplayHarness } from "../orchestration-v2/Adapters/CodexAdapterV2.testkit.ts";
import * as Orchestrator from "../orchestration-v2/Orchestrator.ts";
import { provideDeterministicTestRuntime } from "../orchestration-v2/testkit/DeterministicRuntime.ts";
import {
  materializeFixtureInput,
  MULTI_TURN_FIRST_PROMPT,
  MULTI_TURN_SECOND_PROMPT,
  TOOL_CALL_WRITE_PROMPT,
} from "../orchestration-v2/testkit/fixtures/shared.ts";
import {
  findReplayFixture,
  readReplayFixtureTranscript,
  runReplayFixture,
} from "../orchestration-v2/testkit/ProviderReplayFixtures.ts";
import { layerProviderReplay } from "../orchestration-v2/testkit/ProviderReplayHarness.ts";
import { materializeReplayTranscriptRuntimeInstructions } from "../orchestration-v2/testkit/ReplayRuntimeInstructions.ts";
import { distillAll, FIXTURES, needLandings, promptOrder } from "./testFixtures.ts";

const decodeTranscript = Schema.decodeUnknownSync(ProviderReplayTranscript);

const QUESTION = "Which fixture turn should run next?";
const QUESTION_ITEM_ID = "msg_06900ecb02a780a6016ab5bdbcd76887d0ba52ca8fee9da2e1";

/** Turns the first turn's final answer into a Codex async question, answered by the second turn. */
const asyncQuestionTranscript = (recorded: ProviderReplayTranscript) =>
  decodeTranscript(
    JSON.parse(
      JSON.stringify({
        ...recorded,
        // Codex sends an async question only as a completed item, so the recorded stream of that item goes.
        entries: recorded.entries.filter(
          (entry) =>
            !(
              "label" in entry &&
              (entry.label === "item/started" || entry.label === "item/agentMessage/delta") &&
              JSON.stringify(entry).includes(QUESTION_ITEM_ID)
            ),
        ),
      })
        .replace(
          '"text":"first fixture turn complete","phase":"final_answer","memoryCitation":null,"delivery":null,"questions":null',
          `"text":${JSON.stringify(QUESTION)},"phase":"final_answer","memoryCitation":null,"delivery":"async","questions":[{"title":${JSON.stringify(QUESTION)},"options":["second"]}]`,
        )
        .replace(
          `"input":[{"text":${JSON.stringify(MULTI_TURN_SECOND_PROMPT)}`,
          `"input":[{"text":${JSON.stringify(`${QUESTION}\n${MULTI_TURN_SECOND_PROMPT}`)}`,
        ),
    ),
  );

/** A Codex async question, which T3 stores with a `message` response capability, answered after its run. */
const recordMessageQuestion = Effect.gen(function* () {
  const fixture = findReplayFixture("multi_turn")!;
  const driver = fixture.providers.find((provider) => provider.driver === "codex")!;
  const result = yield* runReplayFixture({
    fixtureName: fixture.name,
    buildInput: () => ({
      ...fixture.buildInput(),
      steps: [
        { type: "message", text: MULTI_TURN_FIRST_PROMPT },
        { type: "await_run_status", targetRunIndex: 1, status: "completed" },
        { type: "answer_next_user_input_request", answers: { "0": MULTI_TURN_SECOND_PROMPT } },
      ],
    }),
    driver: { ...driver, assertOutput: () => undefined },
    transformTranscript: asyncQuestionTranscript,
    harness: CodexOrchestratorReplayHarness,
    materializeWorkspace: (transcript, workspace) =>
      Effect.succeed(materializeReplayTranscriptWorkspace(transcript, workspace)),
  });
  return result.storedEvents;
});

/**
 * A link card asked while an approval holds the run, then answered after the run completes.
 * The card is the `link_request.record` command that PeerLinkRequests sends.
 */
const recordLinkCard = Effect.gen(function* () {
  const fixture = findReplayFixture("tool_call_read_only_on_request")!;
  const variant = fixture.providers.find((provider) => provider.driver === "acpRegistry")!;
  const transcript = yield* AcpRegistryOrchestratorReplayHarness.decodeTranscript(
    materializeReplayTranscriptRuntimeInstructions(
      yield* readReplayFixtureTranscript(variant.transcriptFile),
      { driver: variant.driver, model: variant.modelSelection.model },
    ),
  );
  const materialized = yield* materializeFixtureInput({
    scenario: "voice_link_card",
    fixtureInput: { steps: [{ type: "message", text: TOOL_CALL_WRITE_PROMPT }] },
    driver: variant.driver,
    modelSelection: variant.modelSelection,
  }).pipe(Effect.provide(IdAllocator.layer));
  const threadId = materialized.projectionThreadIds[0]!;
  const scenario = {
    ...materialized,
    name: "voice_link_card",
    transcript,
    runtimePolicyOverride: {
      ...variant.runtimePolicyOverride,
      cwd: yield* checkpointWorkspace("voice_link_card", fixture.buildInput().workspaceFiles),
    },
  };
  return yield* Effect.gen(function* () {
    const orchestrator = yield* Orchestrator.OrchestratorV2;
    const first = (matches: (stored: OrchestrationV2StoredEvent) => boolean) =>
      orchestrator.streamStoredEventsFrom({ threadId }).pipe(
        Stream.filter(matches),
        Stream.take(1),
        Stream.runCollect,
        Effect.map((found) => Array.from(found)[0]!),
      );

    for (const command of materialized.commands) yield* orchestrator.dispatch(command);
    const approval = yield* first(
      ({ event }) => event.type === "runtime-request.updated" && event.payload.status === "pending",
    );
    if (approval.event.type !== "runtime-request.updated") return yield* Effect.die("no approval");
    const run = (yield* orchestrator.getThreadProjection(threadId)).runs.at(-1)!;
    const card = {
      type: "link_request.record",
      threadId,
      runId: run.id,
      nodeId: run.rootNodeId!,
      turnItemId: TurnItemId.make("voice-link-card"),
      reason: "Link the build machine so the agent can run the tests there.",
    } as const;
    yield* orchestrator.dispatch({
      ...card,
      commandId: CommandId.make("voice-link-card-ask"),
      linkStatus: "pending",
    });
    yield* orchestrator.dispatch({
      type: "runtime-request.respond",
      commandId: CommandId.make("voice-link-card-approve"),
      threadId,
      requestId: approval.event.payload.id,
      decision: "accept",
    });
    yield* first(
      ({ event }) =>
        event.type === "run.updated" &&
        event.payload.id === run.id &&
        event.payload.status === "completed",
    );
    yield* orchestrator.dispatch({
      ...card,
      commandId: CommandId.make("voice-link-card-answer"),
      linkStatus: "linked",
      linkedEnvironmentId: EnvironmentId.make("environment-build-machine"),
    });
    const answered = yield* first(
      ({ event }) =>
        event.type === "turn-item.updated" &&
        event.payload.type === "link_request" &&
        event.payload.linkStatus === "linked",
    );
    return Array.from(
      yield* orchestrator.streamStoredEventsFrom({ threadId }).pipe(
        Stream.takeUntil((stored) => stored.sequence >= answered.sequence),
        Stream.runCollect,
      ),
    );
  }).pipe(
    Effect.provide(layerProviderReplay(scenario, AcpRegistryOrchestratorReplayHarness)),
    Effect.scoped,
  );
}).pipe(provideDeterministicTestRuntime);

it.effect.each([
  { name: "linkCard", record: recordLinkCard },
  { name: "messageQuestion", record: recordMessageQuestion },
] as const)("a fresh $name recording still matches its committed fixture", ({ name, record }) =>
  Effect.gen(function* () {
    const recorded = yield* record;
    assert.deepStrictEqual(distillAll(recorded), distillAll(FIXTURES[name]));
    assert.deepStrictEqual(needLandings(recorded), needLandings(FIXTURES[name]));
    assert.strictEqual(promptOrder(recorded), promptOrder(FIXTURES[name]));
  }),
);
