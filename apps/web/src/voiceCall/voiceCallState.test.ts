import { EnvironmentId, ThreadId } from "@t3tools/contracts";
import * as Option from "effect/Option";
import { expect, it } from "vite-plus/test";
import { decodeVoiceServerMessage } from "./voiceCallProtocol";
import {
  applyVoiceCallEvent,
  failVoiceCall,
  newVoiceCall,
  type VoiceCall,
  type VoiceCallEvent,
} from "./voiceCallState";

const start = newVoiceCall(EnvironmentId.make("env"), ThreadId.make("thread"));

function play(frames: ReadonlyArray<string | VoiceCallEvent>) {
  let call: VoiceCall = start;
  const phases: string[] = [];
  for (const frame of frames) {
    const event =
      typeof frame === "string" ? Option.getOrNull(decodeVoiceServerMessage(frame)) : frame;
    if (event === null) continue;
    call = applyVoiceCallEvent(call, event);
    phases.push(call.phase);
  }
  return { call, phases };
}

it("follows a turn from the wire: listening, thinking, speaking until playback drains", () => {
  const { call, phases } = play([
    '{"type":"ready","mode":"vad","silence_ms":700}',
    '{"type":"turn_start","turn":1,"carried_audio_s":0}',
    '{"type":"turn_end","turn":1,"trailing_ms":700,"forced":false}',
    '{"type":"heard","turn":1,"text":" how is it going? "}',
    '{"type":"reply_start","turn":1,"rate":24000}',
    '{"type":"say","turn":1,"text":"It is still"}',
    '{"type":"say","turn":1,"text":" running the tests."}',
    '{"type":"reply_end","turn":1}',
    '{"type":"turn_done","turn":1,"transcript":"how is it going?","replies":[]}',
    { type: "drained" },
  ]);
  expect(phases).toEqual([
    "listening",
    "listening",
    "thinking",
    "thinking",
    "speaking",
    "speaking",
    "speaking",
    "speaking",
    "speaking",
    "listening",
  ]);
  expect(call.captions.map(({ speaker, text, relay }) => [speaker, text, relay])).toEqual([
    ["caller", "how is it going?", false],
    ["voice", "It is still running the tests.", false],
  ]);
  expect(call.phrases).toBe(2);
});

it("keeps a relayed thread line apart from the free reply around it", () => {
  const { call } = play([
    '{"type":"say","turn":2,"text":"Sure."}',
    '{"type":"say","turn":2,"text":"The clamp thread finished.","relay":true}',
    '{"type":"say","turn":3,"text":"Anything else?"}',
  ]);
  expect(call.captions.map(({ text, relay }) => [text, relay])).toEqual([
    ["Sure.", false],
    ["The clamp thread finished.", true],
    ["Anything else?", false],
  ]);
});

it("ignores frames it does not know and freezes once the call has ended", () => {
  const { call } = play(['{"type":"pong","t":1}', "not json", '{"type":"say","turn":1}']);
  expect(call).toEqual(start);
  const ended = failVoiceCall(start, "wrong-secret");
  expect(applyVoiceCallEvent(ended, { type: "ready" })).toBe(ended);
});
