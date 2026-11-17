import React from "react";
import { AbsoluteFill } from "remotion";
import { Archive, clock, LoopState, Task } from "./archive";
import { C, flash, grotesk, mono, rise, STATUS } from "./theme";

const W = 1920;
const YES_NO = ["side_effect", "finished"];
const SOURCE: Record<string, string> = { orchestrator: "orch", lfm: "lfm", service: "svc" };

const clamp = (lines: number): React.CSSProperties => ({
  display: "-webkit-box",
  WebkitLineClamp: lines,
  WebkitBoxOrient: "vertical",
  overflow: "hidden",
});

const Lamp: React.FC<{ on: boolean; color: string; size?: number }> = ({ on, color, size = 22 }) => (
  <div style={{
    width: size, height: size, borderRadius: size, background: on ? color : "#2c2c2c",
    boxShadow: on ? `0 0 ${size}px ${color}` : "none", flexShrink: 0,
  }} />
);

const Module: React.FC<{
  x: number; y: number; w: number; h: number; label: string; lamp?: { on: boolean; color: string };
  glow?: number; glowColor?: string; children: React.ReactNode;
}> = ({ x, y, w, h, label, lamp, glow = 0, glowColor = C.yellow, children }) => (
  <div style={{
    position: "absolute", left: x, top: y, width: w, height: h, background: C.module, borderRadius: 14,
    boxShadow: `0 10px 0 ${C.stageShade}, inset 0 0 0 ${3 * glow}px ${glowColor}`, padding: "22px 28px",
    boxSizing: "border-box", color: C.ink, overflow: "hidden",
  }}>
    <div style={{ display: "flex", alignItems: "center", gap: 14, marginBottom: 16 }}>
      {lamp ? <Lamp {...lamp} /> : null}
      <div style={{ fontFamily: mono, fontWeight: 600, fontSize: 24, letterSpacing: 4, color: C.dim }}>{label}</div>
    </div>
    {children}
  </div>
);

const Waveform: React.FC<{ archive: Archive; t: number; width: number; height: number }> = ({ archive, t, width, height }) => {
  const { fps, microphone, assistant } = archive.waveform;
  const span = 8;
  const bars = 160;
  const step = span / bars;
  const start = t - span * 0.75;
  const mid = height / 2;
  const rects: React.ReactNode[] = [];
  for (let i = 0; i < bars; i++) {
    const at = start + i * step;
    const k = Math.floor(at * fps);
    if (k < 0 || k >= microphone.length) continue;
    const past = at <= t;
    const m = Math.min(1, microphone[k] * 5) * (mid - 4);
    const a = Math.min(1, assistant[k] * 5) * (mid - 4);
    const x = (i * width) / bars;
    const bw = width / bars - 3;
    rects.push(
      <rect key={`m${i}`} x={x} y={mid - m - 2} width={bw} height={m + 2} fill={C.yellow} opacity={past ? 1 : 0.25} />,
      <rect key={`a${i}`} x={x} y={mid + 2} width={bw} height={a + 1} fill={C.teal} opacity={past ? 1 : 0.25} />,
    );
  }
  return (
    <svg width={width} height={height} style={{ display: "block" }}>
      <rect x={0} y={mid - 1} width={width} height={2} fill={C.line} />
      {rects}
      <rect x={width * 0.75 - 2} y={0} width={4} height={height} fill={C.red} />
    </svg>
  );
};

const Meter: React.FC<{ score: number; threshold: number; passed: boolean; age: number }> = ({ score, threshold, passed, age }) => {
  const w = 190;
  return (
    <div style={{ width: w, flexShrink: 0 }}>
      <div style={{ position: "relative", height: 16, background: "#2c2c2c", borderRadius: 3 }}>
        <div style={{ position: "absolute", left: 0, top: 0, bottom: 0, width: w * score * rise(age, 0.4),
          background: passed ? C.teal : C.yellow, borderRadius: 3 }} />
        <div style={{ position: "absolute", left: w * threshold - 2, top: -6, width: 4, height: 28, background: C.ink }} />
      </div>
      <div style={{ fontFamily: mono, fontSize: 24, color: passed ? C.teal : C.yellow, marginTop: 6 }}>
        {score.toFixed(2)} {passed ? "≥" : "<"} {threshold.toFixed(2)}
      </div>
    </div>
  );
};

const TaskCard: React.FC<{ task: Task; t: number; index: number }> = ({ task, t, index }) => {
  const enter = rise(t - task.createdAt, 0.35);
  const status = STATUS[task.status] ?? { color: C.dim, label: task.status.toUpperCase() };
  const blink = flash(t - task.statusAt, 0.5);
  return (
    <div style={{
      position: "absolute", left: 0, right: 0, top: index * 104, height: 92, background: C.panel, borderRadius: 8,
      display: "flex", alignItems: "center", gap: 18, padding: "0 18px", boxSizing: "border-box",
      opacity: enter, translate: `0px ${(1 - enter) * -60}px`,
      boxShadow: `inset 0 0 0 ${3 * blink}px ${status.color}`,
    }}>
      <Lamp on color={status.color} size={26} />
      <div style={{ flex: 1, minWidth: 0 }}>
        <div style={{ display: "flex", gap: 12, fontFamily: mono, fontSize: 24 }}>
          <span style={{ color: status.color, fontWeight: 600 }}>{status.label}</span>
          <span style={{ color: C.dim, whiteSpace: "nowrap" }}>{task.id} · {task.sources.map((x) => SOURCE[x] ?? x).join("+")}</span>
        </div>
        <div style={{ fontFamily: grotesk, fontSize: 28, ...clamp(1) }}>
          {task.result && task.status === "completed" ? `→ ${task.result}` : task.text}
        </div>
      </div>
      {task.gate ? <Meter {...task.gate} age={t - task.gate.t} /> : null}
    </div>
  );
};

const Transcript: React.FC<{ s: LoopState }> = ({ s }) => {
  if (!s.transcript) {
    return <div style={{ fontFamily: grotesk, fontSize: 50, color: C.dim }}>{s.userSpeaking ? "…" : "listening"}</div>;
  }
  return (
    <div style={{ fontFamily: grotesk, fontWeight: 500, fontSize: 52, lineHeight: 1.12, ...clamp(3),
      color: s.transcript.final ? C.ink : "#cfcbc0" }}>
      {s.transcript.text}
      {!s.transcript.final ? <span style={{ background: C.yellow, marginLeft: 6 }}>&nbsp;</span> : null}
    </div>
  );
};

const ReplyText: React.FC<{ s: LoopState }> = ({ s }) => {
  const reply = s.reply;
  if (!reply) return <div style={{ fontFamily: grotesk, fontSize: 44, color: C.dim }}>waiting for the hand-off</div>;
  const words = reply.start.text.split(" ");
  const shown = reply.end?.interrupted && !reply.live
    ? Math.round(words.length * reply.progress)
    : Math.ceil(words.length * reply.progress);
  return (
    <div style={{ fontFamily: grotesk, fontWeight: 500, fontSize: 44, lineHeight: 1.15, ...clamp(4) }}>
      {words.map((word, i) => (
        <span key={i} style={{ color: i < shown ? C.ink : "#4a4a4a" }}>{word} </span>
      ))}
    </div>
  );
};

const Decider: React.FC<{ s: LoopState }> = ({ s }) => {
  const d = s.decision;
  if (!d) return <div style={{ fontFamily: mono, fontSize: 28, color: C.dim }}>no decision yet</div>;
  return (
    <div style={{ background: "#0b0b0b", borderRadius: 8, padding: "14px 18px", height: 236, boxSizing: "border-box" }}>
      <div style={{ fontFamily: mono, fontSize: 24, color: C.red, display: "flex", justifyContent: "space-between" }}>
        <span>REV {String(d.revision).padStart(2, "0")}{d.is_final ? " FINAL" : ""}</span>
        <span>T{d.turn} · {d.latency_ms ?? "?"} MS</span>
      </div>
      {d.decisions.slice(0, 3).map((dec, i) => {
        const conf = Object.entries(dec.confidence ?? {}).map(([k, v]) => (YES_NO.includes(k) ? Math.abs(2 * v - 1) : v));
        const low = conf.length ? Math.min(...conf) : 0;
        return (
          <div key={dec.sid} style={{ marginTop: 12, opacity: rise(s.t - d.t - i * 0.06, 0.2) }}>
            <div style={{ display: "flex", gap: 14, fontFamily: mono, fontSize: 26, color: C.ink }}>
              <span style={{ color: C.yellow }}>{dec.kind.toUpperCase()}</span>
              <span>{dec.readiness.toUpperCase()}</span>
              {dec.side_effect ? <span style={{ color: C.red }}>SIDE EFFECT</span> : null}
              <span style={{ marginLeft: "auto", color: C.dim }}>{low.toFixed(2)}</span>
            </div>
            <div style={{ fontFamily: grotesk, fontSize: 24, color: C.dim, ...clamp(1) }}>{d.segments[i] ?? ""}</div>
            <div style={{ height: 6, background: "#2c2c2c", marginTop: 4 }}>
              <div style={{ height: 6, width: `${low * 100}%`, background: C.yellow }} />
            </div>
          </div>
        );
      })}
    </div>
  );
};

const GateSlam: React.FC<{ s: LoopState }> = ({ s }) => {
  const g = s.gate;
  if (!g) return null;
  const age = s.t - g.t;
  if (age < 0 || age > 1.1) return null;
  const color = g.passed ? C.teal : C.yellow;
  const out = rise(age - 0.85, 0.25);
  return (
    <AbsoluteFill style={{ background: color, justifyContent: "center", alignItems: "center",
      scale: `1 ${1 - out}`, opacity: rise(age, 0.06) }}>
      <div style={{ fontFamily: grotesk, fontWeight: 700, fontSize: 460, lineHeight: 0.9, color: C.module,
        scale: String(1.18 - 0.18 * rise(age, 0.18)) }}>{g.score.toFixed(2)}</div>
      <div style={{ fontFamily: mono, fontWeight: 600, fontSize: 56, color: C.module, letterSpacing: 6 }}>
        {g.passed ? "≥" : "<"} {g.threshold.toFixed(2)} · {g.passed ? "RUN" : "HOLD"} · {g.task_id}
      </div>
    </AbsoluteFill>
  );
};

const HandoffWipe: React.FC<{ s: LoopState }> = ({ s }) => {
  const h = s.handoff;
  if (!h) return null;
  const age = s.t - h.t;
  if (age < 0 || age > 0.9) return null;
  const x = -W + 2 * W * rise(age, 0.9);
  return (
    <div style={{ position: "absolute", left: x, top: 470, width: W, height: 140, background: C.yellow,
      display: "flex", alignItems: "center", padding: "0 60px", boxSizing: "border-box",
      fontFamily: grotesk, fontWeight: 700, fontSize: 72, color: C.module, whiteSpace: "nowrap", overflow: "hidden" }}>
      HAND-OFF → {h.final_transcript}
    </div>
  );
};

export const Stage: React.FC<{ archive: Archive; s: LoopState }> = ({ archive, s }) => {
  const replying = !!s.reply && s.reply.live;
  const barge = s.barge ? flash(s.t - s.barge.t, 0.8) : 0;
  const decided = s.decision ? flash(s.t - s.decision.t, 0.5) : 0;
  const cut = s.reply && !s.reply.live && s.reply.end?.interrupted;
  return (
    <AbsoluteFill style={{ background: C.stage }}>
      <div style={{ position: "absolute", left: 60, top: 36, right: 60, height: 70, display: "flex",
        alignItems: "center", justifyContent: "space-between" }}>
        <div style={{ display: "flex", alignItems: "baseline", gap: 22 }}>
          <div style={{ fontFamily: grotesk, fontWeight: 700, fontSize: 44, color: C.module }}>voice loop</div>
          <div style={{ fontFamily: mono, fontSize: 26, color: "#5d5c58" }}>{archive.name}</div>
        </div>
        <div style={{ display: "flex", gap: 18, alignItems: "center" }}>
          <div style={{ fontFamily: mono, fontWeight: 600, fontSize: 30, color: C.module }}>TURN {String(s.turn).padStart(2, "0")}</div>
          <div style={{ fontFamily: mono, fontWeight: 600, fontSize: 40, background: C.module, color: C.red,
            padding: "4px 18px", borderRadius: 8 }}>{clock(s.t)}</div>
        </div>
      </div>

      <Module x={60} y={130} w={1060} h={430} label="MIC · TRANSCRIPT" lamp={{ on: s.userSpeaking, color: C.red }}
        glow={barge} glowColor={C.red}>
        <Waveform archive={archive} t={s.t} width={1004} height={140} />
        <div style={{ marginTop: 18 }}><Transcript s={s} /></div>
        {barge > 0 ? (
          <div style={{ position: "absolute", right: 28, top: 18, fontFamily: mono, fontWeight: 600, fontSize: 30,
            background: C.red, color: C.module, padding: "2px 14px", opacity: barge }}>BARGE-IN</div>
        ) : null}
      </Module>

      <Module x={60} y={590} w={1060} h={430} label="LFM · VOICE" lamp={{ on: replying, color: C.teal }}
        glow={s.handoff ? flash(s.t - s.handoff.t, 0.7) : 0}>
        <div style={{ fontFamily: mono, fontSize: 24, color: C.dim, ...clamp(1) }}>
          {s.handoff ? `SAW: "${s.handoff.final_transcript ?? ""}"` : "SAW: nothing yet"}
          {s.action && s.action.calls.length
            ? `  ·  DID: ${s.action.calls.map((c) => c.name).join(", ")}`
            : s.action ? "  ·  DID: just talked" : ""}
        </div>
        <div style={{ marginTop: 18, height: 220 }}><ReplyText s={s} /></div>
        {cut ? (
          <div style={{ position: "absolute", right: 28, top: 18, fontFamily: mono, fontWeight: 600, fontSize: 26,
            background: C.red, color: C.module, padding: "2px 14px" }}>CUT OFF</div>
        ) : null}
        <div style={{ position: "absolute", left: 28, right: 28, bottom: 22, display: "flex", gap: 12 }}>
          {s.tools.map((tool) => (
            <div key={tool.seq} style={{ flex: 1, minWidth: 0, background: C.panel, borderRadius: 6, padding: "8px 12px",
              fontFamily: mono, fontSize: 24, color: C.ink, opacity: rise(s.t - tool.t, 0.3),
              boxShadow: `inset 0 0 0 ${3 * flash(s.t - tool.t, 0.6)}px ${C.orange}`, ...clamp(1) }}>
              <span style={{ color: C.orange }}>{tool.name}</span> {summarize(tool.result)}
            </div>
          ))}
        </div>
      </Module>

      <Module x={1160} y={130} w={700} h={330} label="DECIDER" lamp={{ on: decided > 0.05, color: C.yellow }} glow={decided}>
        <Decider s={s} />
        {s.reshape && s.t - s.reshape.t < 1.5 ? (
          <div style={{ position: "absolute", right: 28, top: 18, fontFamily: mono, fontWeight: 600, fontSize: 24,
            background: C.yellow, color: C.module, padding: "2px 12px" }}>
            {s.reshape.type === "task_split" ? "SPLIT" : "MERGE"} {s.reshape.before.length}→{s.reshape.after.length}
          </div>
        ) : null}
      </Module>

      <Module x={1160} y={490} w={700} h={530} label="TASKS">
        <div style={{ position: "relative", height: 440 }}>
          {s.tasks.slice(-4).map((task, i) => <TaskCard key={task.id} task={task} t={s.t} index={i} />)}
          {s.tasks.length === 0 ? <div style={{ fontFamily: mono, fontSize: 26, color: C.dim }}>empty rack</div> : null}
        </div>
      </Module>

      <HandoffWipe s={s} />
      <GateSlam s={s} />
    </AbsoluteFill>
  );
};

const summarize = (result: unknown): string => {
  if (result && typeof result === "object") {
    const r = result as Record<string, unknown>;
    for (const key of ["summary", "result_summary", "text", "task_id"]) {
      if (typeof r[key] === "string") return r[key] as string;
    }
    return r.ok === true ? "ok" : JSON.stringify(result).slice(0, 60);
  }
  return String(result ?? "");
};
