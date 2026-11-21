import type {
  OrchestrationV2DomainEvent,
  OrchestrationV2ProjectedTurnItem,
  OrchestrationV2ThreadProjection,
  OrchestrationV2VoiceCall,
} from "@t3tools/contracts";
import * as DateTime from "effect/DateTime";
import { describe, expect, it } from "vite-plus/test";

import { applyOrchestrationV2ProjectionEvent } from "./orchestrationV2Projection.ts";
import { mergeOlderVoiceCallsIntoProjection } from "./threadHistoryMerge.ts";
import { withVoiceCallRows } from "./voiceCallRows.ts";

const row = (ordinal: number, threadId = "thread-1") =>
  ({
    position: ordinal,
    visibility: threadId === "thread-1" ? "local" : "inherited",
    sourceThreadId: threadId,
    sourceItemId: `item-${threadId}-${ordinal}`,
    item: { id: `item-${threadId}-${ordinal}`, threadId, ordinal, type: "assistant_message" },
  }) as unknown as OrchestrationV2ProjectedTurnItem;
const call = (id: string, ordinal: number, startedAt = "2026-11-21T10:00:00.000Z") =>
  ({
    id,
    threadId: "thread-1",
    ordinal,
    type: "voice_call",
    startedAt: DateTime.makeUnsafe(startedAt),
    updatedAt: DateTime.makeUnsafe(startedAt),
  }) as unknown as OrchestrationV2VoiceCall;
const ids = (rows: ReadonlyArray<{ readonly sourceItemId: string }>) =>
  rows.map((entry) => entry.sourceItemId);

describe("withVoiceCallRows", () => {
  it("puts each call after the last item of its own thread that it follows, never an inherited one", () => {
    const rows = [row(2), row(3, "source"), row(9)];
    expect(ids(withVoiceCallRows(rows, [call("call-a", 5)]))).toEqual([
      "item-thread-1-2",
      "call-a",
      "item-source-3",
      "item-thread-1-9",
    ]);
  });

  it("puts a call that follows no item first among the thread's own rows, even with none yet", () => {
    expect(ids(withVoiceCallRows([row(1, "source"), row(2)], [call("call-0", 0)]))).toEqual([
      "item-source-1",
      "call-0",
      "item-thread-1-2",
    ]);
    expect(ids(withVoiceCallRows([row(1, "source")], [call("call-0", 0)]))).toEqual([
      "item-source-1",
      "call-0",
    ]);
    expect(ids(withVoiceCallRows([], [call("call-0", 0)]))).toEqual(["call-0"]);
  });

  it("holds back a call whose item is not loaded until its page loads", () => {
    const rows = [row(4), row(6)];
    expect(withVoiceCallRows(rows, [call("call-old", 3)])).toBe(rows);
  });

  it("orders calls after one item by when they began, whatever order they arrived in", () => {
    const first = call("call-z", 5, "2026-11-21T10:00:00.000Z");
    const second = call("call-a", 5, "2026-11-21T10:30:00.000Z");
    const rows = [row(5)];
    const expected = ["item-thread-1-5", "call-z", "call-a"];
    expect(ids(withVoiceCallRows(rows, [second, first]))).toEqual(expected);
    expect(ids(withVoiceCallRows(rows, [first, second]))).toEqual(expected);
  });
});

describe("the owner's calls in the client's projection", () => {
  const projection = { thread: { id: "thread-1" } } as unknown as OrchestrationV2ThreadProjection;
  const recorded = (voiceCall: OrchestrationV2VoiceCall) =>
    ({
      id: `event-${voiceCall.id}`,
      type: "thread.voice-call.recorded",
      threadId: "thread-1",
      occurredAt: DateTime.makeUnsafe("2026-11-21T11:00:00.000Z"),
      payload: { call: voiceCall },
    }) as unknown as OrchestrationV2DomainEvent;

  it("keeps a recorded call once and moves nothing else", () => {
    const live = call("call-live", 5);
    const once = applyOrchestrationV2ProjectionEvent(projection, recorded(live));
    const twice = applyOrchestrationV2ProjectionEvent(once, recorded(live));
    expect(twice?.voiceCalls).toEqual([live]);
    expect({ ...twice, voiceCalls: undefined }).toEqual({ ...projection, voiceCalls: undefined });
  });

  it("adds an older page's calls before the ones it holds, and keeps a call it already holds", () => {
    const held = call("call-held", 5);
    const older = call("call-older", 1);
    const merged = mergeOlderVoiceCallsIntoProjection({ ...projection, voiceCalls: [held] }, [
      older,
      held,
    ]);
    expect(merged.voiceCalls).toEqual([older, held]);
  });
});
