import { assert, it } from "@effect/vitest";
import * as NodeCrypto from "@effect/platform-node/NodeCrypto";
import {
  EventId,
  MessageId,
  type OrchestrationV2StoredEvent,
  type OrchestrationV2TurnItem,
  ThreadId,
  VOICE_CALL_LIMITS,
} from "@t3tools/contracts";
import * as Digest from "effect/crypto/Digest";
import * as DateTime from "effect/DateTime";
import * as Effect from "effect/Effect";
import * as Option from "effect/Option";
import * as Stream from "effect/Stream";

import { boundCall, make, type RecordSources } from "./record.ts";
import type { RequestInput } from "./protocol.ts";

const THREAD = ThreadId.make("thread:record");
const OTHER = ThreadId.make("thread:other");
const REQUEST_ID = "01J0000000000000000000000B";
const at = DateTime.makeUnsafe("2026-11-21T10:00:00.000Z");
const reply = MessageId.make("message:reply");

const STORED = [
  {
    sequence: 41,
    event: {
      id: EventId.make("event:41"),
      type: "message.updated",
      threadId: THREAD,
      occurredAt: at,
      payload: { id: reply },
    },
  },
] as unknown as ReadonlyArray<OrchestrationV2StoredEvent>;

const input = (lines: RequestInput<"record_call">["lines"]): RequestInput<"record_call"> => ({
  requestId: REQUEST_ID,
  threadId: THREAD,
  callId: "call-1",
  startedAt: at,
  endedAt: at,
  lines,
});

const LINES: RequestInput<"record_call">["lines"] = [
  { speaker: "caller", text: "How is it going?", relay: false },
  { speaker: "voice", text: "It finished.", relay: true, act: "done", sequences: [41, 40] },
  { speaker: "voice", text: "Want me", relay: false, interrupted: true },
];

/**
 * The seam's services, scripted: the thread's cards, the receipt for the voice's command, and the stored events the
 * evidence points at. A dispatch records the card as the orchestrator would, and a second one answers `replayed`.
 */
const rig = (receipt: { readonly threadId: ThreadId } | null = null) =>
  Effect.gen(function* () {
    const digest = yield* Digest.Digest;
    const dispatched: Array<Record<string, unknown>> = [];
    const cards: Array<OrchestrationV2TurnItem> = [];
    let recorded = receipt;
    const sources = {
      digest,
      threads: {
        getThreadRecords: () => Effect.succeed({ turnItems: cards }),
        getThreadEventSequence: () => Effect.succeed(77),
        dispatch: (command: Record<string, unknown>) => {
          dispatched.push(command);
          if (recorded === null) {
            recorded = { threadId: THREAD };
            cards.push({ ...command, id: command.turnItemId, type: "voice_call" } as never);
            return Effect.succeed({ sequence: 42 });
          }
          return Effect.succeed({ sequence: 77, replayed: true });
        },
      },
      receipts: {
        getByCommandId: () =>
          Effect.succeed(
            recorded === null
              ? Option.none()
              : Option.some({ threadId: recorded.threadId, status: "accepted" }),
          ),
      },
      eventStore: {
        readAgentEvents: (filter?: {
          readonly afterSequence?: number;
          readonly throughSequence?: number;
        }) =>
          Stream.fromIterable(
            STORED.filter(
              (stored) =>
                stored.sequence > (filter?.afterSequence ?? -1) &&
                stored.sequence <= (filter?.throughSequence ?? Infinity),
            ),
          ),
      },
    } as unknown as RecordSources;
    return { dispatched, cards, record: make(sources).record };
  });

it.effect(
  "records the call once, with each checked line's evidence resolved, and a retry adds nothing",
  () =>
    Effect.gen(function* () {
      const { dispatched, cards, record } = yield* rig();
      const first = yield* record(input(LINES));
      assert.deepStrictEqual(
        {
          replayed: first.replayed,
          sequence: first.sequence,
          lines: first.lines,
          truncated: first.truncated,
        },
        { replayed: false, sequence: 42, lines: 3, truncated: false },
      );
      const card = cards[0];
      assert.strictEqual(card?.type, "voice_call");
      if (card?.type !== "voice_call") return;
      assert.deepStrictEqual(card.lines[1]?.claim, {
        act: "done",
        evidence: [
          { sequence: 41, ref: { type: "message", messageId: reply } },
          { sequence: 40, ref: null },
        ],
      });
      assert.strictEqual(card.lines[2]?.interrupted, true);
      assert.strictEqual(card.lines[0]?.claim, undefined, "a free line claims nothing");

      const again = yield* record(input(LINES));
      assert.deepStrictEqual([again.replayed, again.sequence], [true, 77]);
      assert.strictEqual(dispatched.length, 1, "the retry is answered from the receipt");
    }).pipe(Effect.provide(NodeCrypto.layer)),
);

it.effect("refuses a request id that recorded other lines, or went to another thread", () =>
  Effect.gen(function* () {
    const { record } = yield* rig();
    yield* record(input(LINES));
    const changed = yield* record(input(LINES.slice(0, 2))).pipe(Effect.flip);
    assert.strictEqual(changed.code, "invalid_request");
    const elsewhere = yield* rig({ threadId: OTHER });
    const moved = yield* elsewhere.record(input(LINES)).pipe(Effect.flip);
    assert.strictEqual(moved.code, "invalid_request");
  }).pipe(Effect.provide(NodeCrypto.layer)),
);

it("records a call past the bounds cut and marked, never refused whole", () => {
  const long = "x".repeat(VOICE_CALL_LIMITS.lineChars + 10);
  const many = Array.from({ length: VOICE_CALL_LIMITS.lines + 5 }, () => ({
    speaker: "caller" as const,
    text: "hi",
    relay: false,
  }));
  const cut = boundCall([{ speaker: "voice", text: long, relay: false }, ...many]);
  assert.strictEqual(cut.truncated, true);
  assert.strictEqual(cut.lines.length, VOICE_CALL_LIMITS.lines);
  assert.strictEqual(cut.lines[0]?.text.length, VOICE_CALL_LIMITS.lineChars);
  const sequences = Array.from({ length: VOICE_CALL_LIMITS.evidence + 3 }, (_, n) => n);
  const claim = boundCall([{ speaker: "voice", text: "ok", relay: true, act: "done", sequences }]);
  assert.deepStrictEqual(
    [claim.truncated, claim.lines[0]?.sequences?.length],
    [true, VOICE_CALL_LIMITS.evidence],
  );
  const total = boundCall(
    Array.from({ length: 40 }, () => ({
      speaker: "caller" as const,
      text: "y".repeat(1_999),
      relay: false,
    })),
  );
  assert.strictEqual(total.truncated, true);
  assert.ok(
    total.lines.reduce((sum, line) => sum + line.text.length, 0) <= VOICE_CALL_LIMITS.totalChars,
  );
  assert.deepStrictEqual(boundCall(LINES), { lines: LINES, truncated: false });
});
