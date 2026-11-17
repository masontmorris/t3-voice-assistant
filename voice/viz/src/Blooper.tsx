import { Audio } from "@remotion/media";
import React from "react";
import {
  AbsoluteFill, CalculateMetadataFunction, Sequence, staticFile, useCurrentFrame, useVideoConfig,
} from "remotion";
import { Archive, loadArchive, stateAt } from "./archive";
import { FPS } from "./LoopReplay";
import { Stage } from "./Stage";
import { C, grotesk, mono, rise } from "./theme";

/** A region of the stage to push in on, by its center in stage pixels. */
const REGIONS = {
  mic: { x: 590, y: 345 },
  lfm: { x: 590, y: 805 },
  decider: { x: 1510, y: 295 },
  tasks: { x: 1510, y: 755 },
} as const;

type Zoom = { region: keyof typeof REGIONS; scale: number };

export type Shot =
  | { kind: "play"; from: number; to: number; caption?: string; zoom?: Zoom }
  | { kind: "freeze"; at: number; hold: number; caption?: string; stamp?: string; zoom?: Zoom }
  | { kind: "card"; hold: number; title: string; sub?: string };

export type Edit = { archive: string; shots: Shot[] };

export type BlooperProps = { edit: string; data?: { archive: Archive; edit: Edit } };

const length = (shot: Shot) => (shot.kind === "play" ? shot.to - shot.from : shot.hold);

export const blooperMetadata: CalculateMetadataFunction<BlooperProps> = async ({ props }) => {
  const edit: Edit = await fetch(staticFile(`edits/${props.edit}.json`)).then((r) => r.json());
  const archive = await loadArchive(edit.archive);
  const frames = edit.shots.reduce((sum, shot) => sum + Math.round(length(shot) * FPS), 0);
  return { durationInFrames: Math.max(1, frames), props: { ...props, data: { archive, edit } } };
};

const Zoomed: React.FC<{ zoom?: Zoom; age: number; children: React.ReactNode }> = ({ zoom, age, children }) => {
  if (!zoom) return <>{children}</>;
  const { x, y } = REGIONS[zoom.region];
  const p = rise(age, 0.45);
  return (
    <AbsoluteFill style={{ transformOrigin: `${x}px ${y}px`, scale: String(1 + (zoom.scale - 1) * p),
      translate: `${(960 - x) * p}px ${(540 - y) * p}px` }}>
      {children}
    </AbsoluteFill>
  );
};

const Caption: React.FC<{ text: string; age: number }> = ({ text, age }) => {
  const shown = Math.floor(text.length * rise(age - 0.15, Math.min(1.2, text.length * 0.03)));
  return (
    <div style={{ position: "absolute", left: 60, right: 60, bottom: 40, background: C.module, borderRadius: 10,
      padding: "20px 30px", fontFamily: mono, fontWeight: 600, fontSize: 44, color: C.red, letterSpacing: 1,
      boxShadow: `0 10px 0 ${C.stageShade}`, opacity: rise(age, 0.12) }}>
      {text.slice(0, shown)}
      <span style={{ background: C.red, opacity: Math.floor(age * 3) % 2 ? 1 : 0 }}>&nbsp;</span>
    </div>
  );
};

const Stamp: React.FC<{ text: string; age: number }> = ({ text, age }) => {
  const k = rise(age - 0.35, 0.16);
  if (k <= 0) return null;
  return (
    <AbsoluteFill style={{ justifyContent: "center", alignItems: "center" }}>
      <div style={{ fontFamily: grotesk, fontWeight: 700, fontSize: 150, color: C.red, border: `14px solid ${C.red}`,
        padding: "6px 50px", borderRadius: 20, rotate: "-8deg", scale: String(2.2 - 1.2 * k), opacity: k,
        background: "rgba(196,195,190,0.82)", whiteSpace: "nowrap" }}>{text}</div>
    </AbsoluteFill>
  );
};

const ShotView: React.FC<{ shot: Shot; archive: Archive }> = ({ shot, archive }) => {
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();
  const age = frame / fps;
  if (shot.kind === "card") {
    return (
      <AbsoluteFill style={{ background: C.module, justifyContent: "center", padding: 140 }}>
        <div style={{ fontFamily: grotesk, fontWeight: 700, fontSize: 130, lineHeight: 1, color: C.ink,
          translate: `0px ${(1 - rise(age, 0.3)) * 60}px`, opacity: rise(age, 0.3) }}>{shot.title}</div>
        {shot.sub ? (
          <div style={{ fontFamily: mono, fontSize: 44, color: C.red, marginTop: 40, opacity: rise(age - 0.4, 0.3) }}>
            {shot.sub}
          </div>
        ) : null}
      </AbsoluteFill>
    );
  }
  const t = shot.kind === "play" ? shot.from + age : shot.at;
  return (
    <AbsoluteFill>
      <Zoomed zoom={shot.zoom} age={age}><Stage archive={archive} s={stateAt(archive, t)} /></Zoomed>
      {shot.kind === "freeze" ? (
        <AbsoluteFill style={{ background: C.module, opacity: 0.18 * rise(age, 0.1) }} />
      ) : null}
      {shot.kind === "freeze" && shot.stamp ? <Stamp text={shot.stamp} age={age} /> : null}
      {shot.caption ? <Caption text={shot.caption} age={age} /> : null}
    </AbsoluteFill>
  );
};

export const Blooper: React.FC<BlooperProps> = ({ data }) => {
  const { fps } = useVideoConfig();
  if (!data) return null;
  let at = 0;
  return (
    <AbsoluteFill style={{ background: C.stage }}>
      {data.edit.shots.map((shot, i) => {
        const from = at;
        const frames = Math.round(length(shot) * fps);
        at += frames;
        return (
          <Sequence key={i} from={from} durationInFrames={frames} name={`${shot.kind} ${i + 1}`}>
            {shot.kind === "play" ? (
              <Audio src={staticFile(data.archive.audio)} trimBefore={Math.round(shot.from * fps)}
                trimAfter={Math.round(shot.to * fps)} />
            ) : null}
            <ShotView shot={shot} archive={data.archive} />
          </Sequence>
        );
      })}
    </AbsoluteFill>
  );
};
