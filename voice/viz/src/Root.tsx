import React from "react";
import { Composition } from "remotion";
import { Blooper, blooperMetadata } from "./Blooper";
import { FPS, LoopReplay, loopReplayMetadata } from "./LoopReplay";

export const RemotionRoot: React.FC = () => (
  <>
    <Composition
      id="LoopReplay"
      component={LoopReplay}
      durationInFrames={FPS * 10}
      fps={FPS}
      width={1920}
      height={1080}
      defaultProps={{ archive: "example" }}
      calculateMetadata={loopReplayMetadata}
    />
    <Composition
      id="Blooper"
      component={Blooper}
      durationInFrames={FPS * 10}
      fps={FPS}
      width={1920}
      height={1080}
      defaultProps={{ edit: "example" }}
      calculateMetadata={blooperMetadata}
    />
  </>
);
