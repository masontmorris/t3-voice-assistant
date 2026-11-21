import type {
  OrchestrationV2ProjectedTurnItem,
  OrchestrationV2VoiceCall,
} from "@t3tools/contracts";
import { describe, expect, it } from "vite-plus/test";

import { withVoiceCallRows } from "./voiceCallRows.ts";

const row = (ordinal: number, threadId = "thread-1") =>
  ({
    position: ordinal,
    visibility: threadId === "thread-1" ? "local" : "inherited",
    sourceThreadId: threadId,
    sourceItemId: `item-${threadId}-${ordinal}`,
    item: { id: `item-${threadId}-${ordinal}`, threadId, ordinal, type: "assistant_message" },
  }) as unknown as OrchestrationV2ProjectedTurnItem;
const call = (id: string, ordinal: number) =>
  ({
    id,
    threadId: "thread-1",
    ordinal,
    type: "voice_call",
  }) as unknown as OrchestrationV2VoiceCall;
const ids = (rows: ReadonlyArray<{ readonly sourceItemId: string }>) =>
  rows.map((entry) => entry.sourceItemId);

describe("withVoiceCallRows", () => {
  it("puts each call after the last of its thread's items it follows", () => {
    const rows = [row(0, "source"), row(2), row(5), row(9)];
    expect(ids(withVoiceCallRows(rows, [call("call-a", 5), call("call-b", 7)]))).toEqual([
      "item-source-0",
      "item-thread-1-2",
      "item-thread-1-5",
      "call-a",
      "call-b",
      "item-thread-1-9",
    ]);
  });

  it("holds back a call older than every loaded item until its page loads", () => {
    const rows = [row(4), row(6)];
    expect(withVoiceCallRows(rows, [call("call-old", 3)])).toBe(rows);
  });
});
