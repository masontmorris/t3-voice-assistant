import {
  MessageId,
  type OrchestrationV2ProjectedTurnItem,
  RunId,
  RuntimeRequestId,
  ThreadId,
  TurnItemId,
} from "@t3tools/contracts";
import * as DateTime from "effect/DateTime";
import { expect, it } from "vite-plus/test";
import {
  resolveVoiceCallEvidence,
  shownVoiceCallLines,
  VOICE_CALL_SHOWN_LINES,
  type VoiceCallItem,
  voiceCallMinutes,
} from "./VoiceCallCard.logic";

const threadId = ThreadId.make("thread:call");
const at = (iso: string) => DateTime.makeUnsafe(iso);
const base = {
  threadId,
  runId: null,
  nodeId: null,
  providerThreadId: null,
  providerTurnId: null,
  nativeItemRef: null,
  parentItemId: null,
  ordinal: 3,
  status: "completed",
  title: null,
  updatedAt: at("2026-11-21T10:05:00.000Z"),
} as const;

const call = (lines: number): VoiceCallItem => ({
  ...base,
  id: TurnItemId.make("turn-item:voice-call:1"),
  startedAt: at("2026-11-21T10:00:00.000Z"),
  completedAt: at("2026-11-21T10:04:20.000Z"),
  type: "voice_call",
  callId: "call-1",
  truncated: false,
  lineCount: lines,
});
const linesOf = (count: number) =>
  Array.from({ length: count }, (_, n) => ({
    speaker: n % 2 === 0 ? ("caller" as const) : ("voice" as const),
    text: `line ${n}`,
    relay: false,
  }));

const row = (item: OrchestrationV2ProjectedTurnItem["item"]): OrchestrationV2ProjectedTurnItem => ({
  position: item.ordinal,
  visibility: "local",
  sourceThreadId: threadId,
  sourceItemId: item.id,
  item,
});

it("summarizes the call and bounds a long one until it is asked for in full", () => {
  expect(voiceCallMinutes(call(2))).toBe(4);
  const long = linesOf(VOICE_CALL_SHOWN_LINES + 10);
  expect(shownVoiceCallLines(call(long.length), long, false)).toHaveLength(VOICE_CALL_SHOWN_LINES);
  expect(shownVoiceCallLines(call(long.length), long, true)).toHaveLength(long.length);
});

it("points a checked line's evidence at the thread items it rests on, and never guesses one", () => {
  const reply = MessageId.make("message:reply");
  const request = RuntimeRequestId.make("request:approve");
  const items = [
    row({
      ...base,
      id: TurnItemId.make("turn-item:reply"),
      startedAt: null,
      completedAt: null,
      type: "assistant_message",
      messageId: reply,
      text: "The clamp helper is in.\nTests pass.",
      streaming: false,
    } as OrchestrationV2ProjectedTurnItem["item"]),
    row({
      ...base,
      id: TurnItemId.make("turn-item:approval"),
      startedAt: null,
      completedAt: null,
      title: "Run the tests?",
      type: "approval_request",
      requestId: request,
    } as OrchestrationV2ProjectedTurnItem["item"]),
  ];
  const runs = [{ id: RunId.make("run:1"), status: "completed" as const }];
  const resolve = (ref: Parameters<typeof resolveVoiceCallEvidence>[0]["ref"]) =>
    resolveVoiceCallEvidence({ sequence: 1, ref }, items, runs);
  expect(resolve({ type: "message", messageId: reply })).toEqual({
    kind: "item",
    itemType: "assistant_message",
    text: "The clamp helper is in.",
  });
  expect(resolve({ type: "request", requestId: request })).toEqual({
    kind: "item",
    itemType: "approval_request",
    text: "Run the tests?",
  });
  expect(resolve({ type: "run", runId: RunId.make("run:1") })).toEqual({
    kind: "run",
    status: "completed",
  });
  expect(resolve(null)).toEqual({ kind: "thread" });
  expect(resolve({ type: "message", messageId: MessageId.make("message:older") })).toEqual({
    kind: "missing",
  });
});
