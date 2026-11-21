import {
  MessageId,
  type OrchestrationV2TurnItem,
  type OrchestrationV2VoiceCallEvidence,
  type OrchestrationV2VoiceCallLine,
  RunId,
  RuntimeRequestId,
  TurnItemId,
  VOICE_CALL_LIMITS,
} from "@t3tools/contracts";
import * as Digest from "effect/crypto/Digest";
import * as DateTime from "effect/DateTime";
import * as Effect from "effect/Effect";
import * as Option from "effect/Option";
import * as Schema from "effect/Schema";
import * as Stream from "effect/Stream";

import type * as CommandReceiptStore from "../orchestration-v2/CommandReceiptStore.ts";
import type * as ThreadManagementService from "../orchestration-v2/ThreadManagementService.ts";
import type * as OrchestrationEventStore from "../persistence/OrchestrationEventStore.ts";
import { voiceCommandId } from "./launch.ts";
import type { RequestInput, VoiceRecordResult } from "./protocol.ts";
import { VoiceSeamRefusal } from "./refusal.ts";

export interface RecordSources {
  readonly digest: Digest.Digest["Service"];
  readonly threads: Pick<
    ThreadManagementService.ThreadManagementService["Service"],
    "dispatch" | "getThreadRecords" | "getThreadEventSequence"
  >;
  readonly receipts: Pick<CommandReceiptStore.CommandReceiptStoreV2["Service"], "getByCommandId">;
  readonly eventStore: Pick<
    OrchestrationEventStore.OrchestrationEventStore["Service"],
    "readAgentEvents"
  >;
}

type Given = RequestInput<"record_call">["lines"][number];
type Bounded = { readonly lines: ReadonlyArray<Given>; readonly truncated: boolean };

/**
 * The call within VOICE_CALL_LIMITS: lines past the line count or the total are left out, a long line is cut, and a
 * claim keeps its first few sequences. What was left out is marked, never refused.
 */
export function boundCall(lines: ReadonlyArray<Given>): Bounded {
  const kept: Array<Given> = [];
  let total = 0;
  let truncated = false;
  for (const line of lines) {
    if (kept.length === VOICE_CALL_LIMITS.lines || total >= VOICE_CALL_LIMITS.totalChars) {
      truncated = true;
      break;
    }
    const room = Math.min(VOICE_CALL_LIMITS.lineChars, VOICE_CALL_LIMITS.totalChars - total);
    const text = line.text.length > room ? line.text.slice(0, room) : line.text;
    const sequences = line.sequences?.slice(0, VOICE_CALL_LIMITS.evidence);
    truncated ||= text !== line.text || sequences?.length !== line.sequences?.length;
    total += text.length;
    kept.push({ ...line, text, ...(sequences === undefined ? {} : { sequences }) });
  }
  return { lines: kept, truncated };
}

/** What a stored event of the thread is about, for the transcript to point at. */
const refOf = (event: { readonly type: string; readonly payload?: unknown }) => {
  const id = (event.payload as { readonly id?: string } | undefined)?.id;
  if (id === undefined) return null;
  switch (event.type) {
    case "message.updated":
      return { type: "message", messageId: MessageId.make(id) } as const;
    case "runtime-request.updated":
      return { type: "request", requestId: RuntimeRequestId.make(id) } as const;
    case "turn-item.updated":
      return { type: "turn_item", turnItemId: TurnItemId.make(id) } as const;
    case "run.updated":
      return { type: "run", runId: RunId.make(id) } as const;
    default:
      return null;
  }
};

const isCard = (
  item: OrchestrationV2TurnItem,
  id: TurnItemId,
): item is Extract<OrchestrationV2TurnItem, { readonly type: "voice_call" }> =>
  item.type === "voice_call" && item.id === id;

/**
 * Records a call's transcript on a visible thread, once per request id. The command is the request id's alone, so the
 * same id sent to another thread finds the first thread's receipt and is refused. A receipt does not hold the lines,
 * so the card's id is a digest of the request as received, before any bound: a replay finds its card only when every
 * field and every line, the cut tail too, is what was first sent, and any other copy of the id is refused.
 */
const encodeReceived = Schema.encodeEffect(Schema.fromJsonString(Schema.Unknown));

export const make = (sources: RecordSources) => {
  const { threads, receipts, eventStore } = sources;
  const unavailable = Effect.mapError(VoiceSeamRefusal.unavailable);

  const record = Effect.fn("VoiceSeam.recordCall")(function* (input: RequestInput<"record_call">) {
    const commandId = yield* voiceCommandId(sources.digest, "record", input.requestId);
    const received = yield* encodeReceived([
      input.requestId,
      input.threadId,
      input.callId,
      DateTime.formatIso(input.startedAt),
      DateTime.formatIso(input.endedAt),
      input.lines,
    ]).pipe(unavailable);
    const payload = yield* sources.digest.digestHex(
      Digest.sha256,
      new TextEncoder().encode(received),
    );
    const turnItemId = TurnItemId.make(`turn-item:voice-call:${payload}`);
    const bounded = boundCall(input.lines);

    const recorded = Effect.fn("VoiceSeam.recordCallRecorded")(function* (
      sequence: number,
      replayed: boolean,
    ) {
      const { turnItems } = yield* threads
        .getThreadRecords(input.threadId, ["turnItems"], {
          turnItemTypes: ["voice_call"],
          turnItemRunIds: [null],
        })
        .pipe(unavailable);
      const card = turnItems.find((item) => isCard(item, turnItemId));
      if (card === undefined) {
        return yield* VoiceSeamRefusal.invalidRequest(
          "That request id already recorded something else.",
        );
      }
      return {
        threadId: input.threadId,
        turnItemId,
        lines: card.lines.length,
        truncated: card.truncated,
        sequence,
        replayed,
      } satisfies VoiceRecordResult;
    });
    const replay = Effect.fn("VoiceSeam.recordCallReplay")(function* () {
      const sequence = yield* threads.getThreadEventSequence(input.threadId).pipe(unavailable);
      return yield* recorded(sequence, true);
    });
    const fromReceipt = (receipt: CommandReceiptStore.CommandReceiptV2) => {
      if (receipt.threadId !== input.threadId) {
        return Effect.fail(
          VoiceSeamRefusal.invalidRequest("That request already went to another thread."),
        );
      }
      if (receipt.status === "rejected") {
        return Effect.fail(VoiceSeamRefusal.refused({ reason: receipt.error, commandId }));
      }
      return replay();
    };

    const receipt = yield* receipts.getByCommandId(commandId).pipe(unavailable);
    if (Option.isSome(receipt)) return yield* fromReceipt(receipt.value);

    const evidenceAt = (sequence: number) =>
      eventStore
        .readAgentEvents({
          threadId: input.threadId,
          afterSequence: sequence - 1,
          throughSequence: sequence,
          limit: 1,
          skipUnknownEventTypes: true,
        })
        .pipe(
          Stream.runHead,
          Effect.map((stored): OrchestrationV2VoiceCallEvidence => ({
            sequence,
            // A read's `as_of` sequence, or one the log no longer holds, cites the thread, not an item of it.
            ref:
              Option.isSome(stored) && stored.value.sequence === sequence
                ? refOf(stored.value.event)
                : null,
          })),
          unavailable,
        );
    const lines = yield* Effect.forEach(bounded.lines, (line) =>
      line.act === undefined
        ? Effect.succeed<OrchestrationV2VoiceCallLine>({
            speaker: line.speaker,
            text: line.text,
            relay: line.relay,
            ...(line.interrupted === true ? { interrupted: true } : {}),
          })
        : Effect.map(
            Effect.forEach(line.sequences ?? [], evidenceAt),
            (evidence): OrchestrationV2VoiceCallLine => ({
              speaker: line.speaker,
              text: line.text,
              relay: line.relay,
              ...(line.interrupted === true ? { interrupted: true } : {}),
              claim: { act: line.act!, evidence },
            }),
          ),
    );
    const dispatched = yield* threads
      .dispatch({
        type: "thread.voice-call.record",
        commandId,
        threadId: input.threadId,
        turnItemId,
        callId: input.callId,
        startedAt: input.startedAt,
        endedAt: input.endedAt,
        lines,
        truncated: bounded.truncated,
      })
      .pipe(Effect.result);
    if (dispatched._tag === "Failure") {
      // A record that failed after T3 recorded its command answers as its retry would.
      const after = yield* receipts.getByCommandId(commandId).pipe(unavailable);
      if (Option.isNone(after)) return yield* VoiceSeamRefusal.unavailable(dispatched.failure);
      return yield* fromReceipt(after.value);
    }
    const { sequence, replayed } = dispatched.success;
    if (replayed === true) return yield* replay();
    return yield* recorded(sequence, false);
  });

  return { record };
};
