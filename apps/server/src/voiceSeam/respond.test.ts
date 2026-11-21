import { assert, it } from "@effect/vitest";
import {
  type OrchestrationV2RuntimeRequest,
  type OrchestrationV2StoredEvent,
  type OrchestrationV2TurnItem,
  ThreadId,
} from "@t3tools/contracts";
import * as Digest from "effect/crypto/Digest";
import * as Effect from "effect/Effect";
import * as Option from "effect/Option";

import * as NodeCrypto from "@effect/platform-node/NodeCrypto";
import { voiceCommandId } from "./launch.ts";
import { make, type RespondSources } from "./respond.ts";
import { FIXTURES } from "./testFixtures.ts";

const THREAD = ThreadId.make("thread:respond");
const OTHER = ThreadId.make("thread:other");
const REQUEST_ID = "01J0000000000000000000000A";

const pendingRequestOf = (events: ReadonlyArray<OrchestrationV2StoredEvent>) =>
  events.flatMap((stored) =>
    stored.event.type === "runtime-request.updated" && stored.event.payload.status === "pending"
      ? [stored.event.payload]
      : [],
  )[0]!;

const promptOf = (events: ReadonlyArray<OrchestrationV2StoredEvent>) =>
  events.flatMap((stored) =>
    stored.event.type === "turn-item.updated" && stored.event.payload.type === "approval_request"
      ? [stored.event.payload]
      : [],
  );

const APPROVAL = pendingRequestOf(FIXTURES.approval);
const QUESTION = pendingRequestOf(FIXTURES.question);

interface Rig {
  readonly dispatched: Array<Record<string, unknown>>;
  readonly sources: RespondSources;
}

type Decision = OrchestrationV2RuntimeRequest["decision"];

/**
 * The seam's two services, scripted: the thread's pending requests, the ones T3 resolved (with their decision), and the
 * receipt for the voice's command. A dispatch resolves the request, and one that finds the command already recorded
 * answers `replayed` as the orchestrator does.
 */
const rig = (
  pending: ReadonlyArray<OrchestrationV2RuntimeRequest>,
  receipt: { readonly threadId: ThreadId; readonly status: "accepted" | "rejected" } | null = null,
  resolved: ReadonlyArray<OrchestrationV2RuntimeRequest> = [],
): Effect.Effect<Rig> =>
  Effect.gen(function* () {
    const digest = yield* Digest.Digest;
    const dispatched: Array<Record<string, unknown>> = [];
    const answered = [...resolved];
    let recorded = receipt !== null;
    const sources = {
      digest,
      threads: {
        getThreadRecords: (
          _thread: ThreadId,
          _kinds: unknown,
          filter: { runtimeRequestStatuses?: string[] },
        ) =>
          Effect.succeed({
            runtimeRequests: filter.runtimeRequestStatuses?.includes("resolved")
              ? answered
              : pending,
            turnItems: promptOf(FIXTURES.approval) as ReadonlyArray<OrchestrationV2TurnItem>,
          }),
        getThreadEventSequence: () => Effect.succeed(77),
        dispatch: (command: Record<string, unknown>) => {
          dispatched.push(command);
          const replayed = recorded;
          if (!recorded) {
            recorded = true;
            answered.push({
              ...pending.find((request) => request.id === command.requestId)!,
              status: "resolved",
              decision: command.decision as Decision,
            });
          }
          return Effect.succeed({ sequence: 78, replayed });
        },
      },
      receipts: {
        getByCommandId: () =>
          Effect.succeed(
            receipt === null
              ? Option.none()
              : Option.some({ ...receipt, error: receipt.status === "rejected" ? "no" : null }),
          ),
      },
    } as unknown as RespondSources;
    return { dispatched, sources };
  }).pipe(Effect.provide(NodeCrypto.layer));

const resolvedAs = (request: OrchestrationV2RuntimeRequest, decision: Decision) =>
  ({ ...request, status: "resolved", decision }) as OrchestrationV2RuntimeRequest;

const input = (decision: "approve" | "deny", pendingRequestId: string = APPROVAL.id) => ({
  requestId: REQUEST_ID,
  threadId: THREAD,
  pendingRequestId,
  decision,
});

const outcome = <A>(effect: Effect.Effect<A, { readonly code: string }>) =>
  effect.pipe(
    Effect.result,
    Effect.map((result) => (result._tag === "Failure" ? result.failure.code : "answered")),
  );

const T3_DECISION = { approve: "accept", deny: "decline" } as const;

it.effect.each(["approve", "deny"] as const)(
  "%s answers the pending approval with T3's decision",
  (decision) =>
    Effect.gen(function* () {
      const { dispatched, sources } = yield* rig([APPROVAL]);
      const answered = yield* make(sources).respond(input(decision));
      assert.deepEqual(answered, {
        threadId: THREAD,
        pendingRequestId: APPROVAL.id,
        decision,
        sequence: 78,
        replayed: false,
      });
      assert.lengthOf(dispatched, 1);
      assert.deepInclude(dispatched[0]!, {
        type: "runtime-request.respond",
        threadId: THREAD,
        requestId: APPROVAL.id,
        decision: T3_DECISION[decision],
      });
      const digest = yield* Digest.Digest;
      assert.strictEqual(
        dispatched[0]!.commandId,
        yield* voiceCommandId(digest, "respond", REQUEST_ID),
        "the voice's request id fixes the command",
      );
    }).pipe(Effect.provide(NodeCrypto.layer)),
);

it.effect("answers nothing for a request that is no longer pending", () =>
  Effect.gen(function* () {
    const { dispatched, sources } = yield* rig([]);
    assert.strictEqual(yield* outcome(make(sources).respond(input("approve"))), "invalid_request");
    assert.lengthOf(dispatched, 0);
  }).pipe(Effect.provide(NodeCrypto.layer)),
);

it.effect("answers nothing for a question: only approvals can be answered by voice", () =>
  Effect.gen(function* () {
    const { dispatched, sources } = yield* rig([QUESTION]);
    assert.strictEqual(
      yield* outcome(make(sources).respond(input("approve", QUESTION.id))),
      "invalid_request",
    );
    assert.lengthOf(dispatched, 0);
  }).pipe(Effect.provide(NodeCrypto.layer)),
);

it.effect("answers nothing for an approval that can no longer be resumed", () =>
  Effect.gen(function* () {
    const stuck = {
      ...APPROVAL,
      responseCapability: { type: "not_resumable", reason: "session ended" },
    } as OrchestrationV2RuntimeRequest;
    const { dispatched, sources } = yield* rig([stuck]);
    assert.strictEqual(yield* outcome(make(sources).respond(input("approve"))), "invalid_request");
    assert.lengthOf(dispatched, 0);
  }).pipe(Effect.provide(NodeCrypto.layer)),
);

it.effect("a request id that already answered replays its receipt and never answers again", () =>
  Effect.gen(function* () {
    const { dispatched, sources } = yield* rig([], { threadId: THREAD, status: "accepted" }, [
      resolvedAs(APPROVAL, "accept"),
    ]);
    const answered = yield* make(sources).respond(input("approve"));
    assert.deepInclude(answered, { sequence: 77, replayed: true });
    assert.lengthOf(dispatched, 0);
  }).pipe(Effect.provide(NodeCrypto.layer)),
);

it.effect(
  "a replay with another request or another decision is refused, not reported as accepted",
  () =>
    Effect.gen(function* () {
      const first = [resolvedAs(APPROVAL, "accept")];
      const receipt = { threadId: THREAD, status: "accepted" } as const;
      const decided = yield* rig([], receipt, first);
      assert.strictEqual(
        yield* outcome(make(decided.sources).respond(input("deny"))),
        "invalid_request",
      );
      const other = yield* rig([QUESTION], receipt, first);
      assert.strictEqual(
        yield* outcome(make(other.sources).respond(input("approve", QUESTION.id))),
        "invalid_request",
      );
      assert.lengthOf(
        [...decided.dispatched, ...other.dispatched],
        0,
        "no second command was sent",
      );
    }).pipe(Effect.provide(NodeCrypto.layer)),
);

it.effect(
  "two concurrent copies of one request id answer once, and a copy with another decision is refused",
  () =>
    Effect.gen(function* () {
      const same = yield* rig([APPROVAL]);
      const both = yield* Effect.all(
        [
          outcome(make(same.sources).respond(input("approve"))),
          outcome(make(same.sources).respond(input("approve"))),
        ],
        { concurrency: 2 },
      );
      assert.deepEqual(both, ["answered", "answered"]);
      const results = yield* Effect.all(
        [
          make(same.sources).respond(input("approve")),
          make(same.sources).respond(input("approve")),
        ],
        { concurrency: 2 },
      );
      assert.deepEqual(results.map((answered) => answered.replayed).sort(), [true, true]);
      const split = yield* rig([APPROVAL]);
      const mixed = yield* Effect.all(
        [
          outcome(make(split.sources).respond(input("approve"))),
          outcome(make(split.sources).respond(input("deny"))),
        ],
        { concurrency: 2 },
      );
      assert.deepEqual([...mixed].sort(), ["answered", "invalid_request"]);
      assert.strictEqual(
        split.dispatched.filter((command) => command.decision === "accept").length +
          split.dispatched.filter((command) => command.decision === "decline").length,
        2,
        "both reached the orchestrator, which recorded only the first",
      );
    }).pipe(Effect.provide(NodeCrypto.layer)),
);

it.effect("refuses a receipt that belongs to another thread or that T3 rejected", () =>
  Effect.gen(function* () {
    const elsewhere = yield* rig([APPROVAL], { threadId: OTHER, status: "accepted" });
    assert.strictEqual(
      yield* outcome(make(elsewhere.sources).respond(input("approve"))),
      "invalid_request",
    );
    const rejected = yield* rig([APPROVAL], { threadId: THREAD, status: "rejected" });
    assert.strictEqual(yield* outcome(make(rejected.sources).respond(input("deny"))), "refused");
    assert.lengthOf([...elsewhere.dispatched, ...rejected.dispatched], 0);
  }).pipe(Effect.provide(NodeCrypto.layer)),
);

it.effect("an unreadable thread is unavailable, and nothing is answered", () =>
  Effect.gen(function* () {
    const { dispatched, sources } = yield* rig([APPROVAL]);
    const broken = {
      ...sources,
      threads: {
        ...sources.threads,
        getThreadRecords: () => Effect.fail({ _tag: "TestDatabaseError" as const }),
      },
    } as unknown as RespondSources;
    assert.strictEqual(yield* outcome(make(broken).respond(input("approve"))), "unavailable");
    assert.lengthOf(dispatched, 0);
  }).pipe(Effect.provide(NodeCrypto.layer)),
);
