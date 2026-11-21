import { assert, it } from "@effect/vitest";
import * as NodeCrypto from "@effect/platform-node/NodeCrypto";
import {
  EventId,
  MessageId,
  type OrchestrationV2StoredEvent,
  type OrchestrationV2VoiceCall,
  type OrchestrationV2VoiceCallLine,
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
 * The seam's services, scripted: the thread's cards, a receipt per command naming its thread, and the stored events
 * the evidence points at. A dispatch records the card as the orchestrator would; a command it already holds answers
 * `replayed` without recording anything.
 */
const rig = () =>
  Effect.gen(function* () {
    const digest = yield* Digest.Digest;
    const dispatched: Array<Record<string, unknown>> = [];
    const cards: Array<OrchestrationV2VoiceCall> = [];
    const sent = new Map<string, ReadonlyArray<OrchestrationV2VoiceCallLine>>();
    const receipts = new Map<string, ThreadId>();
    const sources = {
      digest,
      threads: {
        getThreadVoiceCalls: (threadId: ThreadId) =>
          Effect.succeed(cards.filter((card) => card.threadId === threadId)),
        getThreadEventSequence: () => Effect.succeed(77),
        dispatch: (command: Record<string, unknown> & { commandId: string; threadId: ThreadId }) =>
          Effect.suspend(() => {
            dispatched.push(command);
            if (receipts.has(command.commandId)) {
              return Effect.succeed({ sequence: 77, replayed: true });
            }
            receipts.set(command.commandId, command.threadId);
            const lines = command.lines as ReadonlyArray<OrchestrationV2VoiceCallLine>;
            sent.set(String(command.turnItemId), lines);
            cards.push({
              ...command,
              id: command.turnItemId,
              type: "voice_call",
              lineCount: lines.length,
            } as never);
            return Effect.succeed({ sequence: 42 });
          }),
      },
      receipts: {
        getByCommandId: (commandId: string) =>
          Effect.sync(() => {
            const threadId = receipts.get(commandId);
            return threadId === undefined
              ? Option.none()
              : Option.some({ threadId, status: "accepted" });
          }),
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
    return { dispatched, cards, sent, record: make(sources).record };
  });

it.effect(
  "records the call once, with each checked line's evidence resolved, and a retry adds nothing",
  () =>
    Effect.gen(function* () {
      const { dispatched, cards, sent, record } = yield* rig();
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
      if (card === undefined) return;
      const recorded = sent.get(card.id) ?? [];
      assert.deepStrictEqual(recorded[1]?.claim, {
        act: "done",
        evidence: [
          { sequence: 41, ref: { type: "message", messageId: reply } },
          { sequence: 40, ref: null },
        ],
      });
      assert.strictEqual(recorded[2]?.interrupted, true);
      assert.strictEqual(recorded[0]?.claim, undefined, "a free line claims nothing");

      const again = yield* record(input(LINES));
      assert.deepStrictEqual([again.replayed, again.sequence], [true, 77]);
      assert.strictEqual(dispatched.length, 1, "the retry is answered from the receipt");
    }).pipe(Effect.provide(NodeCrypto.layer)),
);

it.effect("binds a request id to the whole request as received, and to its one thread", () =>
  Effect.gen(function* () {
    const { cards, record } = yield* rig();
    const first = input(LINES);
    yield* record(first);
    const later = DateTime.makeUnsafe("2026-11-21T10:09:00.000Z");
    const changes = {
      "other lines": { ...first, lines: LINES.slice(0, 2) },
      "a changed start": { ...first, startedAt: later },
      "a changed end": { ...first, endedAt: later },
      "another call id": { ...first, callId: "call-2" },
    };
    for (const [change, retry] of Object.entries(changes)) {
      const refused = yield* record(retry).pipe(Effect.flip);
      assert.strictEqual(refused.code, "invalid_request", change);
    }
    const elsewhere = yield* record({ ...first, threadId: OTHER }).pipe(Effect.flip);
    assert.strictEqual(elsewhere.code, "invalid_request", "the same id on another thread");
    assert.strictEqual(cards.length, 1, "no second card, on either thread");
    assert.strictEqual(
      (yield* record(first)).replayed,
      true,
      "the exact same request still replays",
    );
  }).pipe(Effect.provide(NodeCrypto.layer)),
);

it.effect("binds the tail cut past the bounds too, though it is never recorded", () =>
  Effect.gen(function* () {
    const { cards, record } = yield* rig();
    const many = (tail: string) =>
      Array.from({ length: VOICE_CALL_LIMITS.lines + 1 }, (_, n) => ({
        speaker: "caller" as const,
        text: n === VOICE_CALL_LIMITS.lines ? tail : "hi",
        relay: false,
      }));
    const first = yield* record(input(many("first tail")));
    assert.strictEqual(first.truncated, true);
    const refused = yield* record(input(many("another tail"))).pipe(Effect.flip);
    assert.strictEqual(refused.code, "invalid_request");
    assert.strictEqual(cards.length, 1);
  }).pipe(Effect.provide(NodeCrypto.layer)),
);

it.effect(
  "on a synchronous rig, answers copies of one request with one card and refuses another under its id; under real concurrency the orchestrator's command receipts hold this",
  () =>
    Effect.gen(function* () {
      const { cards, record } = yield* rig();
      const copies = yield* Effect.all([record(input(LINES)), record(input(LINES))], {
        concurrency: "unbounded",
      });
      assert.deepStrictEqual(
        copies.map((copy) => copy.turnItemId).filter((id, n, all) => all.indexOf(id) === n).length,
        1,
      );
      assert.strictEqual(cards.length, 1);
      const fresh = yield* rig();
      const [kept, other] = yield* Effect.all(
        [
          Effect.result(fresh.record(input(LINES))),
          Effect.result(fresh.record(input(LINES.slice(1)))),
        ],
        { concurrency: "unbounded" },
      );
      assert.strictEqual(kept._tag, "Success");
      assert.strictEqual(other._tag === "Failure" ? other.failure.code : null, "invalid_request");
      assert.strictEqual(fresh.cards.length, 1);
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
