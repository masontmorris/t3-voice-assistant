# Loop visualization

`voice/research/viz/` is a Remotion project that renders a loop archive as video. It has two compositions. `LoopReplay` plays a whole run in real time with its audio. `Blooper` cuts a run into a short edit from a shot list. The archive drives every element of the frame. Nothing in the frame is written by hand except an edit's captions.

## Render a run

```bash
cd voice/research/viz
npm i
node scripts/stage.mjs "$VOICE_HOME/archives/<run>" <name>
npx remotion render LoopReplay "$VOICE_HOME/viz/renders/<name>.mp4" --props='{"archive":"<name>"}'
```

`stage.mjs` copies the archive's manifest, timeline, waveform and stems into `public/archives/<name>/`, which git ignores. The composition reads the manifest's duration, so the video is as long as the run.

## What the frame shows

The look follows a teenage engineering field-system brief: a warm grey stage, black instrument modules, and signal red, yellow and teal lamps. The brief and its eight references are under `$VOICE_HOME/viz-refs/loop-viz/` (`brief.md`, `picked/`), outside git.

| Module | Draws | From events |
| --- | --- | --- |
| Header | Run name, turn number and the replay clock | `speech_start` |
| MIC · TRANSCRIPT | An 8 s scrolling waveform (microphone above the line, assistant below) and the current turn's transcript, with a cursor while it is partial. Flashes red on a barge-in. | `waveform.json`, `transcript_partial`, `transcript_final`, `barge_in` |
| DECIDER | The latest decision point: revision, latency, and each segment's kind, readiness, side effect and weakest confidence. A tag shows a split or a merge. | `decision_point`, `task_split`, `task_merged` |
| TASKS | Up to four task cards. Each drops in when created, has a status lamp, and shows its gate meter (score against the threshold tick) and its result when done. | `task_created`, `task_status`, `gate_verdict` |
| LFM · VOICE | What LFM saw at the hand-off, which tools it called, the reply with words lit as they play, a CUT OFF tag, and tool results. | `handoff`, `lfm_action`, `reply_start`, `reply_end`, `tool_call` |
| Overlays | A gate verdict slams its score full-frame for 1.1 s. The hand-off wipes the final transcript across the frame. | `gate_verdict`, `handoff` |

The stage is a pure function of archive time. `stateAt(archive, t)` in `src/archive.ts` folds the events up to `t`, and `src/Stage.tsx` draws that state. Both compositions call the same pair, so an edit shows exactly what the replay shows at that moment.

## Make an edit

An edit is a JSON shot list in `public/edits/<name>.json`, rendered with `npx remotion render Blooper out.mp4 --props='{"edit":"<name>"}'`. There are three kinds of shot:

- `play` plays archive time `from` to `to` with its audio, with an optional `caption` and `zoom`.
- `freeze` holds archive time `at` for `hold` seconds, silent, with an optional `caption`, `stamp` and `zoom`.
- `card` is a title card with `title` and `sub`.

A `zoom` names a module (`mic`, `lfm`, `decider` or `tasks`) and a scale. A caption types out like a device's error message. A stamp slams a rubber-stamp word over the frame.

Edits of real runs quote real speech, so they live under `$VOICE_HOME/viz/edits/` and are copied in to render. Git ignores every edit except the synthetic `example.json`.

## Verification

- `npx tsc --noEmit` passes.
- The synthetic `LoopReplay` render is 25.7 s at 1920x1080, H.264 with AAC (measured with `ffprobe`). Its soundtrack sits 0.1 ms from `conversation.wav` at 2, 8, 14 and 21.5 s, by cross-correlation of the decoded MP4 audio (measured).
- Frames are drawn at 30 fps, so an event shows up to 33 ms after its timestamp. That is longer than the archive's 20 ms alignment. A lag that size is unlikely to be visible (guess).
- Five renders exist under `$VOICE_HOME/viz/renders/`, outside git (measured with `ffprobe`): the synthetic replay (25.7 s), the synthetic edit (31.5 s), the Desktop loopback replay (43.1 s), Mason's demo replay (91.7 s) and an edit of the demo (72.1 s).
