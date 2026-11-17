import { loadFont as loadGrotesk } from "@remotion/google-fonts/SpaceGrotesk";
import { loadFont as loadMono } from "@remotion/google-fonts/IBMPlexMono";
import { Easing, interpolate } from "remotion";

export const grotesk = loadGrotesk("normal", { weights: ["500", "700"], subsets: ["latin"] }).fontFamily;
export const mono = loadMono("normal", { weights: ["400", "600"], subsets: ["latin"] }).fontFamily;

export const C = {
  stage: "#c4c3be",
  stageShade: "#b3b2ad",
  module: "#151515",
  panel: "#222222",
  line: "#3a3a3a",
  ink: "#f2f0ea",
  dim: "#8d8b85",
  red: "#ec3b24",
  yellow: "#f6c315",
  teal: "#0f8f80",
  orange: "#ff6b1a",
  lcd: "#c9e86b",
};

export const STATUS: Record<string, { color: string; label: string }> = {
  proposed: { color: C.dim, label: "PROPOSED" },
  ready: { color: C.yellow, label: "READY" },
  needs_clarification: { color: C.yellow, label: "ASK" },
  held: { color: C.yellow, label: "HELD" },
  dispatched: { color: C.orange, label: "SENT" },
  running: { color: C.orange, label: "RUNNING" },
  completed: { color: C.teal, label: "DONE" },
  superseded: { color: C.dim, label: "REPLACED" },
  withdrawn: { color: C.dim, label: "DROPPED" },
  cancel_requested: { color: C.red, label: "CANCEL" },
};

const snap = Easing.bezier(0.2, 0.9, 0.1, 1);

/** 0 to 1 over `duration` seconds after an event `age` seconds ago. Hardware-key easing: fast in, hard stop. */
export const rise = (age: number, duration = 0.25) =>
  interpolate(age, [0, duration], [0, 1], { extrapolateLeft: "clamp", extrapolateRight: "clamp", easing: snap });

/** 1 right after an event, falling to 0 over `duration` seconds. */
export const flash = (age: number, duration = 0.6) =>
  age < 0 ? 0 : interpolate(age, [0, duration], [1, 0], { extrapolateRight: "clamp" });
