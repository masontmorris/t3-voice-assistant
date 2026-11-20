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

/** The seam's two services, scripted: the thread's pending requests, and the receipt for the voice's command. */
const rig = (
  pending: ReadonlyArray<OrchestrationV2RuntimeRequest>,
  receipt: { readonly threadId: ThreadId; readonly status: "accepted" | "rejected" } | null = null,
): Effect.Effect<Rig> =>
  Effect.gen(function* () {
    const digest = yield* Digest.Digest;
    const dispatched: Array<Record<string, unknown>> = [];
    const sources = {
      digest,
      threads: {
        getThreadRecords: () =>
          Effect.succeed({
            runtimeRequests: pending,
            turnItems: promptOf(FIXTURES.approval) as ReadonlyArray<OrchestrationV2TurnItem>,
          }),
        getThreadEventSequence: () => Effect.succeed(77),
        dispatch: (command: Record<string, unknown>) => {
          dispatched.push(command);
          return Effect.succeed({ sequence: 78, replayed: false });
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
    const { dispatched, sources } = yield* rig([APPROVAL], {
      threadId: THREAD,
      status: "accepted",
    });
    const answered = yield* make(sources).respond(input("approve"));
    assert.deepInclude(answered, { sequence: 77, replayed: true });
    assert.lengthOf(dispatched, 0);
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
