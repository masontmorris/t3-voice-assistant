import {
  CommandId,
  EnvironmentId,
  EventId,
  NonNegativeInt,
  type OrchestrationV2DomainEvent,
  OrchestrationV2DomainEventJson,
  type OrchestrationV2StoredEvent,
  ProjectId,
  type RunId,
  ThreadId,
} from "@t3tools/contracts";
import * as DateTime from "effect/DateTime";
import * as Schema from "effect/Schema";

import { distill } from "./events.ts";

import approval from "./fixtures/approval.json" with { type: "json" };
import failedRun from "./fixtures/failed-run.json" with { type: "json" };
import interrupted from "./fixtures/interrupted.json" with { type: "json" };
import lateSecretCard from "./fixtures/late-secret-card.json" with { type: "json" };
import linkCard from "./fixtures/link-card.json" with { type: "json" };
import messageQuestion from "./fixtures/message-question.json" with { type: "json" };
import plainReply from "./fixtures/plain-reply.json" with { type: "json" };
import queuedCancelled from "./fixtures/queued-cancelled.json" with { type: "json" };
import question from "./fixtures/question.json" with { type: "json" };
import rolledBack from "./fixtures/rolled-back.json" with { type: "json" };
import subagentAfterRoot from "./fixtures/subagent-after-root.json" with { type: "json" };

const decodeFixture = Schema.decodeUnknownSync(
  Schema.Struct({
    scenario: Schema.String,
    events: Schema.Array(
      Schema.Struct({
        sequence: NonNegativeInt,
        commandId: Schema.NullOr(CommandId),
        event: OrchestrationV2DomainEventJson,
      }),
    ),
  }),
);

const load = (raw: unknown): ReadonlyArray<OrchestrationV2StoredEvent> => decodeFixture(raw).events;

/**
 * Stored events, one thread each, in stored order. The first four come from Replay (dev) turns;
 * `linkCard`, `lateSecretCard` and `messageQuestion` from needs.integration.test.ts, which drives the orchestrator.
 */
export const FIXTURES = {
  plainReply: load(plainReply),
  approval: load(approval),
  question: load(question),
  interrupted: load(interrupted),
  linkCard: load(linkCard),
  lateSecretCard: load(lateSecretCard),
  messageQuestion: load(messageQuestion),
  failedRun: load(failedRun),
  rolledBack: load(rolledBack),
  subagentAfterRoot: load(subagentAfterRoot),
  queuedCancelled: load(queuedCancelled),
} as const;

export type FixtureName = keyof typeof FIXTURES;

export const describeEvent = (stored: OrchestrationV2StoredEvent): string | null => {
  const distilled = distill(stored);
  if (distilled === null) return null;
  const signal = distilled.signal;
  const settle = distilled.settleCheck ? " +settle" : "";
  if (signal === null) return `${stored.event.type} => settle`;
  switch (signal.type) {
    case "run":
      return `${stored.event.type} => run ${signal.status}${settle}`;
    case "reply":
      return `${stored.event.type} => reply ${JSON.stringify(signal.text)}${settle}`;
    case "pending":
      return `${stored.event.type} => pending ${signal.kind} ${signal.source}${settle}`;
    case "resolved":
      return `${stored.event.type} => resolved ${signal.status}${settle}`;
    case "secret_card":
      return `${stored.event.type} => secret card of run ${signal.runId === null ? "none" : "known"}${settle}`;
    case "gone":
      return `${stored.event.type} => gone${settle}`;
  }
};

/** Each run's statuses in stored order, by ordinal. */
export const runStatuses = (events: ReadonlyArray<OrchestrationV2StoredEvent>) =>
  events.flatMap((stored) =>
    stored.event.type === "run.updated"
      ? [`${stored.event.payload.ordinal} ${stored.event.payload.status}`]
      : [],
  );

export const distillAll = (events: ReadonlyArray<OrchestrationV2StoredEvent>) =>
  events.flatMap((stored) => describeEvent(stored) ?? []);

const requests = (events: ReadonlyArray<OrchestrationV2StoredEvent>) =>
  events.flatMap((stored) =>
    stored.event.type === "runtime-request.updated"
      ? [{ sequence: stored.sequence, request: stored.event.payload }]
      : [],
  );

const turnItems = (events: ReadonlyArray<OrchestrationV2StoredEvent>) =>
  events.flatMap((stored) =>
    stored.event.type === "turn-item.updated"
      ? [{ sequence: stored.sequence, item: stored.event.payload }]
      : [],
  );

/** Where the turn item that carries a request's prompt lands against the request itself. */
export const promptOrder = (events: ReadonlyArray<OrchestrationV2StoredEvent>) => {
  const pending = requests(events).find(({ request }) => request.status === "pending");
  const item = turnItems(events).find(
    ({ item }) =>
      (item.type === "approval_request" || item.type === "user_input_request") &&
      item.requestId === pending?.request.id,
  );
  if (pending === undefined || item === undefined) return "missing";
  return pending.sequence < item.sequence ? "request first" : "turn item first";
};

const TERMINAL = new Set(["completed", "failed", "interrupted", "cancelled", "rolled_back"]);

/** Where each need starts and ends against the terminal run.updated of the run that asked it. */
export const needLandings = (events: ReadonlyArray<OrchestrationV2StoredEvent>) => {
  const terminalOf = (runId: RunId | null) =>
    events.find(
      (stored) =>
        stored.event.type === "run.updated" &&
        stored.event.payload.id === runId &&
        TERMINAL.has(stored.event.payload.status),
    )?.sequence;
  const against = (sequence: number | undefined, terminal: number | undefined) =>
    sequence === undefined
      ? "never"
      : terminal === undefined
        ? "with no terminal run"
        : sequence < terminal
          ? "before the run ends"
          : "after the run ends";
  const items = turnItems(events);
  const requestNeeds = [...new Set(requests(events).map(({ request }) => request.id))].map((id) => {
    const updates = requests(events).filter(({ request }) => request.id === id);
    const asked = updates.find(({ request }) => request.status === "pending");
    const ended = updates.find(({ request }) => request.status !== "pending");
    const prompt = items.find(
      ({ item }) =>
        (item.type === "approval_request" || item.type === "user_input_request") &&
        item.requestId === id,
    );
    const terminal = terminalOf(prompt?.item.runId ?? null);
    const capability = asked?.request.responseCapability.type;
    return `${asked?.request.kind} (${capability}) asked ${against(asked?.sequence, terminal)}, ${ended?.request.status ?? "open"} ${against(ended?.sequence, terminal)}`;
  });
  const cardNeeds = [
    ...new Set(
      items.flatMap(({ item }) =>
        item.type === "link_request" || item.type === "secret_request" ? [item.id] : [],
      ),
    ),
  ].map((id) => {
    const updates = items.filter(({ item }) => item.id === id);
    const asked = updates.find(({ item }) => item.status === "waiting");
    const ended = updates.find(({ item }) => item.status !== "waiting");
    const terminal = terminalOf(asked?.item.runId ?? null);
    return `${asked?.item.type} asked ${against(asked?.sequence, terminal)}, ${ended?.item.status ?? "open"} ${against(ended?.sequence, terminal)}`;
  });
  return [...requestNeeds, ...cardNeeds];
};

export const createdOf = (events: ReadonlyArray<OrchestrationV2StoredEvent>) =>
  events.flatMap((stored) => (stored.event.type === "thread.created" ? [stored.event] : []))[0]!;

const CREATED = createdOf(FIXTURES.plainReply);
const START = DateTime.makeUnsafe("2026-11-01T00:00:00.000Z");

export const SEED_PROJECT = ProjectId.make("project:voice");

/** A thread with no runs, created `minutes` after START, so a larger number is newer. */
export const thread = (input: {
  readonly id: string;
  readonly minutes: number;
  readonly projectId?: ProjectId;
  readonly settled?: boolean;
  readonly lineage?: Partial<(typeof CREATED)["payload"]["lineage"]>;
  readonly delegated?: boolean;
}): OrchestrationV2DomainEvent => {
  const id = ThreadId.make(input.id);
  const at = DateTime.add(START, { minutes: input.minutes });
  return {
    ...CREATED,
    id: EventId.make(`created:${input.id}`),
    threadId: id,
    occurredAt: at,
    payload: {
      ...CREATED.payload,
      id,
      projectId: input.projectId ?? SEED_PROJECT,
      title: `thread ${input.id}`,
      lineage: {
        parentThreadId: null,
        relationshipToParent: null,
        rootThreadId: id,
        ...input.lineage,
      },
      ...(input.delegated === true
        ? {
            delegatedFrom: {
              environmentId: EnvironmentId.make("environment:elsewhere"),
              threadId: ThreadId.make("elsewhere"),
              title: "elsewhere",
            },
          }
        : {}),
      createdAt: at,
      updatedAt: at,
      settledOverride: input.settled === true ? "settled" : null,
      settledAt: input.settled === true ? at : null,
    },
  };
};
