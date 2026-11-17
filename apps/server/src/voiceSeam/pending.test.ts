import { assert, it } from "@effect/vitest";
import {
  EventId,
  MessageId,
  type OrchestrationV2DomainEvent,
  type OrchestrationV2StoredEvent,
} from "@t3tools/contracts";
import * as DateTime from "effect/DateTime";
import * as Effect from "effect/Effect";
import * as Layer from "effect/Layer";

import * as EventSink from "../orchestration-v2/EventSink.ts";
import * as EventStore from "../orchestration-v2/EventStore.ts";
import { OrchestratorProjectionError } from "../orchestration-v2/Orchestrator.ts";
import * as ProjectionStore from "../orchestration-v2/ProjectionStore.ts";
import * as Sqlite from "../persistence/Sqlite.ts";
import { readNeed, type NeedSignal, type PendingReadSource } from "./pending.ts";
import { FIXTURES } from "./testFixtures.ts";

const layer = EventSink.layer.pipe(
  Layer.provideMerge(Layer.merge(EventStore.layer, ProjectionStore.layer)),
  Layer.provideMerge(Sqlite.layerMemory),
);

/** A message Mason writes in the thread one second after its first user message. */
const userWritesLater = (fixture: ReadonlyArray<OrchestrationV2StoredEvent>) => {
  const message = fixture.flatMap((stored) =>
    stored.event.type === "message.updated" && stored.event.payload.role === "user"
      ? [stored.event]
      : [],
  )[0]!;
  const later = DateTime.add(message.payload.updatedAt, { seconds: 1 });
  return {
    sequence: 0,
    commandId: null,
    event: {
      ...message,
      id: EventId.make("user-writes-later"),
      payload: {
        ...message.payload,
        id: MessageId.make("user-writes-later"),
        text: "Never mind the link, run them here.",
        createdBy: "user",
        createdAt: later,
        updatedAt: later,
      },
    } satisfies OrchestrationV2DomainEvent,
  };
};

/** The link-card fixture with a message Mason writes after the card and before he answers it. */
const linkCardMovedPast = (() => {
  const answer = FIXTURES.linkCard.findIndex(
    (stored) =>
      stored.event.type === "turn-item.updated" &&
      stored.event.payload.type === "link_request" &&
      stored.event.payload.status !== "waiting",
  );
  return [
    ...FIXTURES.linkCard.slice(0, answer),
    userWritesLater(FIXTURES.linkCard),
    ...FIXTURES.linkCard.slice(answer),
  ];
})();

const cardSignal = (event: OrchestrationV2DomainEvent): NeedSignal | null => {
  if (event.type !== "turn-item.updated") return null;
  const item = event.payload;
  if (item.type === "link_request") {
    return {
      type: "pending",
      threadId: event.threadId,
      requestId: item.id,
      kind: "in_app",
      source: "link_request",
      sequence: 0,
    };
  }
  if (item.type === "secret_request") {
    return {
      type: "secret_card",
      threadId: event.threadId,
      requestId: item.id,
      runId: item.runId,
      sequence: 0,
    };
  }
  return null;
};

it.effect.each([
  { name: "linkCard", events: FIXTURES.linkCard, states: [true, false] },
  { name: "lateSecretCard", events: FIXTURES.lateSecretCard, states: [false] },
  {
    name: "linkCard moved past by a user message",
    events: linkCardMovedPast,
    states: [true, false],
  },
])(
  "the pending read's waitsOnUser agrees with the shell's userOnly pick at every step of $name",
  ({ events, states }) =>
    Effect.gen(function* () {
      const sink = yield* EventSink.EventSinkV2;
      const projections = yield* ProjectionStore.ProjectionStoreV2;
      const threads: PendingReadSource = {
        getThreadRecords: (id, fields, filter) =>
          projections
            .getThreadRecords(id, fields, filter)
            .pipe(
              Effect.mapError((cause) => new OrchestratorProjectionError({ threadId: id, cause })),
            ),
      };
      const threadId = events[0]!.event.threadId;
      let card: NeedSignal | null = null;
      const compared: Array<boolean> = [];
      for (const stored of events) {
        const [written] = yield* sink.write({ events: [stored.event] });
        card ??= cardSignal(stored.event);
        if (card === null) continue;
        const pending = yield* projections.getThreadRecords(threadId, ["runtimeRequests"], {
          runtimeRequestStatuses: ["pending"],
        });
        // Another need hides the card from the shell's pick, so only the card's own states compare.
        if (pending.runtimeRequests.length > 0) continue;
        const shell = yield* projections.getThreadShell(threadId);
        const picked =
          shell?.pendingRuntimeRequest?.userOnly === true &&
          shell.pendingRuntimeRequest.id === card.requestId;
        const waits = (yield* readNeed(threads, card)) !== null;
        assert.strictEqual(waits, picked, `at stored ${stored.event.type} ${written!.sequence}`);
        if (compared.at(-1) !== waits) compared.push(waits);
      }
      assert.deepStrictEqual(compared, states);
    }).pipe(Effect.provide(layer)),
);
