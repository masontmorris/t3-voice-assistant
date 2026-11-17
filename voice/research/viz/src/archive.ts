import { staticFile } from "remotion";

type Base = { t: number; seq: number; turn: number; src: "server" | "client" | "derived" };

export type Decision = {
  sid: string;
  kind: string;
  readiness: string;
  side_effect?: boolean;
  confidence?: Record<string, number>;
};

export type LoopEvent = Base &
  (
    | { type: "speech_start" }
    | { type: "speech_end" }
    | { type: "barge_in" }
    | { type: "turn_end"; trailing_ms: number; forced: boolean }
    | { type: "transcript_partial" | "transcript_final"; revision: number; text: string }
    | { type: "decision_point"; revision: number; is_final: boolean; segments: string[]; decisions: Decision[]; latency_ms?: number }
    | { type: "task_split" | "task_merged"; before: string[]; after: string[] }
    | { type: "gate_verdict"; task_id: string; score: number; threshold: number; passed: boolean; weakest: string }
    | { type: "task_created"; task_id: string; text: string | null; sources: string[] }
    | { type: "task_status"; task_id: string; status: string; reason?: string; result_summary?: string }
    | { type: "handoff"; final_transcript: string | null; unreviewed_speech: string | null; segments: string[] }
    | { type: "lfm_action"; calls: { name: string; arguments: unknown }[]; text: string }
    | { type: "tool_call"; name: string; arguments: unknown; result: unknown }
    | { type: "reply_start"; reply: string; reply_id: string; text: string }
    | { type: "reply_end"; reply: string; reply_id: string; played_s: number; interrupted: boolean; cut?: string | null }
    | { type: "backlog_reprompt"; task_ids: string[] }
  );

type Of<K extends LoopEvent["type"]> = LoopEvent & { type: K };

export type Archive = {
  name: string;
  run: string;
  duration: number;
  events: LoopEvent[];
  waveform: { fps: number; microphone: number[]; assistant: number[] };
  audio: string;
};

export const loadArchive = async (name: string): Promise<Archive> => {
  const base = `archives/${name}`;
  const [manifest, timeline, waveform] = await Promise.all([
    fetch(staticFile(`${base}/manifest.json`)).then((r) => r.json()),
    fetch(staticFile(`${base}/timeline.jsonl`)).then((r) => r.text()),
    fetch(staticFile(`${base}/waveform.json`)).then((r) => r.json()),
  ]);
  if (manifest.schema !== "voice.loop.archive/1") {
    throw new Error(`${name}: schema ${manifest.schema} is not voice.loop.archive/1`);
  }
  const conversation = manifest.stems.find((s: { role: string }) => s.role === "conversation");
  return {
    name,
    run: manifest.run,
    duration: manifest.duration_s,
    events: timeline.split("\n").filter(Boolean).map((line) => JSON.parse(line)),
    waveform,
    audio: `${base}/${conversation.file}`,
  };
};

export type Task = {
  id: string;
  turn: number;
  text: string;
  sources: string[];
  createdAt: number;
  status: string;
  statusAt: number;
  reason?: string;
  result?: string;
  gate?: Of<"gate_verdict">;
};

export type Reply = { start: Of<"reply_start">; end?: Of<"reply_end">; progress: number; live: boolean };

export type LoopState = {
  t: number;
  turn: number;
  userSpeaking: boolean;
  transcript: { text: string; final: boolean; at: number } | null;
  decision: Of<"decision_point"> | null;
  reshape: Of<"task_split" | "task_merged"> | null;
  tasks: Task[];
  gate: Of<"gate_verdict"> | null;
  handoff: Of<"handoff"> | null;
  action: Of<"lfm_action"> | null;
  tools: Of<"tool_call">[];
  reply: Reply | null;
  barge: Of<"barge_in"> | null;
};

/** Everything the stage draws at archive time t: a fold over the events that have happened by then. */
export const stateAt = (archive: Archive, t: number): LoopState => {
  const s: LoopState = {
    t, turn: 0, userSpeaking: false, transcript: null, decision: null, reshape: null, tasks: [], gate: null,
    handoff: null, action: null, tools: [], reply: null, barge: null,
  };
  const tasks = new Map<string, Task>();
  const ends = new Map<string, Of<"reply_end">>();
  for (const e of archive.events) {
    if (e.type === "reply_end") ends.set(e.reply_id, e);
  }
  for (const e of archive.events) {
    if (e.t > t) break;
    switch (e.type) {
      case "speech_start":
        s.turn = e.turn;
        s.userSpeaking = true;
        s.transcript = null;
        s.handoff = null;
        s.action = null;
        break;
      case "speech_end":
        if (e.turn === s.turn) s.userSpeaking = false;
        break;
      case "transcript_partial":
      case "transcript_final":
        if (e.turn === s.turn) s.transcript = { text: e.text, final: e.type === "transcript_final", at: e.t };
        break;
      case "decision_point":
        s.decision = e;
        break;
      case "task_split":
      case "task_merged":
        s.reshape = e;
        break;
      case "task_created":
        tasks.set(e.task_id, {
          id: e.task_id, turn: e.turn, text: e.text ?? "", sources: e.sources, createdAt: e.t, status: "proposed",
          statusAt: e.t,
        });
        break;
      case "task_status": {
        const task = tasks.get(e.task_id);
        if (task) Object.assign(task, { status: e.status, statusAt: e.t, reason: e.reason ?? task.reason,
          result: e.result_summary ?? task.result });
        break;
      }
      case "gate_verdict": {
        const task = tasks.get(e.task_id);
        if (task) task.gate = e;
        s.gate = e;
        break;
      }
      case "handoff":
        s.handoff = e;
        break;
      case "lfm_action":
        s.action = e;
        break;
      case "tool_call":
        s.tools = [...s.tools, e].slice(-3);
        break;
      case "reply_start": {
        const end = ends.get(e.reply_id);
        const length = end ? end.played_s : 0;
        const live = !end || t < end.t;
        s.reply = { start: e, end, live, progress: length > 0 ? Math.min(1, (t - e.t) / length) : 1 };
        break;
      }
      case "barge_in":
        s.barge = e;
        break;
    }
  }
  s.tasks = [...tasks.values()].sort((a, b) => a.createdAt - b.createdAt).slice(-6);
  return s;
};

export const clock = (t: number) => {
  const m = Math.floor(t / 60);
  const sec = t - m * 60;
  return `${String(m).padStart(2, "0")}:${sec.toFixed(2).padStart(5, "0")}`;
};
