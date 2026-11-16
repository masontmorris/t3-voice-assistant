# Why live replies parrot, and the one-line fix

The live assistant answers "What did I just ask you?" by repeating the question back. Three runs on the Desktop isolate
the cause, and the cause is not the persona. It is the machine text the server injects into the voice session, because
that text repeats the person's own words as the newest thing the model reads before it replies.

The fix is one rule: nothing the server injects may repeat the turn's words. The snapshot keeps its place, its role,
its prefix, its task list, its statuses and its results, and loses only the strings that quote the person.

## What was measured

Each run is one GPU lease under `quiet_run`, no priority, on loopback with synthetic audio, against the same harness,
the same five-turn and three-turn scripts, and persona `prompt.txt` sha256 `02700e254b8f` (measured). Scores come from
`score.py`, which checks parroting, recall, third-person narration and clarification asks in code.

| Run | Was in the voice session before the reply | Parrot turns | Narration | Clarification | Recall |
| --- | --- | --- | --- | --- | --- |
| run 5 | the snapshot item, after the tools turn, holding `final_transcript` | 3, 4, and the tail of 7 | none | 7 | 1 of 4 |
| run 6 (A) | the snapshot item moved before the turn's audio, holding the prior turn's state | 3, 4, 5 | 7 | none | 2 of 4 |
| run 7 (B) | nothing | none | none | 1, 2, 4, 7 | 3 of 4 |
| run 8 (C) | the snapshot in its run 5 place, with the person's words removed | none | none | none | 3 of 4 |

Run 6 is the one that names the mechanism. Its snapshot went in before the audio, so it could not carry the current
utterance, and the snapshot reply stopped echoing. The echo moved to the second reply of each turn, which follows the
task-update item, and that item carried a `description` equal to the utterance (measured: run 6 turns 3, 4 and 5, every
parrot in a reply whose newest item held the turn's words). Run 7 removed all machine text and no echo survived at any
turn, and no reply used a task at all. Run 8 dropped only the echoing strings and no echo survived while the task
results still reached the person (measured: turns 1 and 6 report the Denver forecast from the update item).

## What llama.cpp's realtime API accepts

There is no non-user role for a text item in the realtime conversation. `conversation.item.create` takes a message with
role `user`, or a function call output, and refuses anything else with `only user messages and function call outputs
are supported` (measured: `tools/server/server-realtime.cpp`, `on_item_create`, at commit `b4c0e00a4`, the build
`llama-server` reports as 12428). A function call output needs tools on the session and a response that called one, so
it is not available to a session that only speaks and listens (inferred from the same function). Session instructions
are fixed once the conversation starts (measured: the same file refuses an instruction change after the first audio or
message). The position and the wording of that one item are the only levers, which is why this fix is about wording.

## The change

`voice/prototype/run.py` gains a `snapshot_mode` and a filter:

- `reply` is the shipped behaviour and stays the default.
- `reply-no-utterance` keeps the item in the same place and drops every string that repeats the person's words, at any
  depth, from the snapshot and from the task update. `without_utterance` does the dropping, and `repeats` is the test
  it uses: a case-insensitive verbatim match on the turn's transcript.
- `before-audio` and `off` are the two ablation settings the runs above used, kept so the experiment can be rerun.

`voice/live/server.py` gains `--snapshot` with those four values and records the mode in each turn's `live` block, so a
record says how its reply was built.

## What it costs

A task whose description is the utterance loses that description, so a reply about a task that has not finished cannot
name the subject from the task list alone. Run 8 turn 2 answers "I don't have access to the weather data for Saturday"
and does not repeat Denver, though the model heard it earlier in the call (measured). A decider-written description that
paraphrases the request rather than copying it would keep the label and still stay clear of the person's words
(guess: the decider's paraphrase is already in the dispatch log for most turns, but no run has injected one).

## Where the evidence is

The runs, their scores, the per-turn audio and the harness copy are under `$VOICE_HOME/evidence/live/memory-20261116/`,
with `ablation/runA` to `ablation/runC` for runs 6 to 8 and `score.py` for the checks. `runs.md` there carries the one
difference and the expectation for every run.
