import { Audio } from "@remotion/media";
import React from "react";
import { AbsoluteFill, CalculateMetadataFunction, staticFile, useCurrentFrame, useVideoConfig } from "remotion";
import { Archive, loadArchive, stateAt } from "./archive";
import { Stage } from "./Stage";

export type LoopReplayProps = { archive: string; data?: Archive };

export const FPS = 30;

export const loopReplayMetadata: CalculateMetadataFunction<LoopReplayProps> = async ({ props }) => {
  const data = await loadArchive(props.archive);
  return { durationInFrames: Math.ceil(data.duration * FPS), props: { ...props, data } };
};

const Frame: React.FC<{ data: Archive }> = ({ data }) => {
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();
  return <Stage archive={data} s={stateAt(data, frame / fps)} />;
};

export const LoopReplay: React.FC<LoopReplayProps> = ({ data }) => {
  if (!data) return null;
  return (
    <AbsoluteFill>
      <Audio src={staticFile(data.audio)} />
      <Frame data={data} />
    </AbsoluteFill>
  );
};
