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

- `reply-no-utterance` keeps the item in the same place and removes the person's words from the snapshot and from the
  task update. It is the default for live mode.
- `reply` is the earlier behaviour, with everything in the item. It stays the default for the prototype's own runs, so
  their records compare with earlier ones.
- `before-audio` and `off` are the two ablation settings the runs above used, kept so the experiment can be rerun.
  `off` sends no machine text under every `--eou` and `--inject`.

`without_utterance` removes the person's words in two ways:

- By field. `final_transcript`, `unreviewed_speech`, `segment_text`, `instruction`, `description`, every element of
  `clarification_needed`, and the `text` of each entry in the decider's `read` list go, whatever they hold.
- By content. Any other string, in a dict or a list, goes when it holds the transcript or the transcript holds it.
  Both sides are normalised first: case, curly quotes, contractions ("what's" and "what is" match) and spacing. A
  single word is not compared, so a status such as `running` stays when the person happened to say "running".

`voice/live/server.py` gains `--snapshot` with those four values and records the mode in each turn's `live` block, so a
record says how its reply was built.

## What it costs

The task list loses its subject. A task's `description` is the decision model's copy of the request, so it goes, and a
reply about a task that has not finished cannot name the subject from the item. Two replies in run 8 show it (measured).
Turn 1 answered "I can't provide the weather in Denver tomorrow as I don't have access to real-time weather data" while
the lookup ran, a false refusal. Turn 7, "That's not what I meant.", answered "I'm running the task to get the weather
in Denver tomorrow", a task that turn did not have. Turn 2 not naming Denver is not a cost of the filter: its
description was "And what about Saturday?", which never named Denver (measured, run 8 records). Both measured costs
come from the item losing its subject.
Giving the task back a subject that is not the person's words is the paraphrase work in PR 28's follow-up, not this
filter.

## Where the evidence is

The runs, their scores, the per-turn audio and the harness copy are under `$VOICE_HOME/evidence/live/memory-20261116/`,
with `ablation/runA` to `ablation/runC` for runs 6 to 8 and `score.py` for the checks. `runs.md` there carries the one
difference and the expectation for every run.
