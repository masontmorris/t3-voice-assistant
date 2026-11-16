# Wrong facts in the voice reply, and the check that stops them

Written 2026-11-16. Every number below is measured from records under `$VOICE_HOME/evidence/live/` except where it says inferred or guess.

The voice assistant invented a day. With the parroting fix in (#27) in place, run C's demo call turned the person's "That's not what I meant." into "I'm running the task to get the weather in Denver for Sunday." The person never said Sunday. This note traces the word, counts how often facts like it appear in every live record on disk, and describes the code check that now refuses such a reply.

## The failure, as recorded

`$VOICE_HOME/evidence/live/memory-20261116/ablation/demo-runC/server/turn_003.json`. The person's three turns were "What's the weather in Denver tomorrow?", "And what about Saturday?" and "That's not what I meant."

| Field | Value |
| --- | --- |
| `tasks[0].segment_text` and `tasks[0].instruction` | `"That's not what I meant."` |
| `tasks[0].history[0].segment_text` | `"That's not what I meant."` |
| `task_updates[0].description` | `"That's not what I meant."` |
| the injected snapshot item (`events.voice_sent[106]`) | `{"type": "utterance_snapshot", ..., "tasks": [{"task_id": "task_1", "status": "running"}], "clarification_needed": null}` |
| first `response.output_text.delta` | `" Sunday"`, at `_t` 2.6718 |
| `response.output_text.done` | `"I'm running the task to get the weather in Denver for Sunday."`, at `_t` 2.8997 |

The word is in the record 14 times and in none of it before `_t` 2.6718. Turns 1 and 2 contain no "Sunday".

## Which layer wrote it

The voice model did, in its own reply. Measured, from the same record:

- The segmenter wrote the person's own words. So did the task service's `description` and the task update's.
- The parroting fix drops every string that repeats the person's words, so the snapshot that reached the voice session held `{"task_id": "task_1", "status": "running"}` and nothing else. This is the fix doing its job: the description was the person's words.
- The voice model then had a running task with no subject and its own history that ended with "...for Saturday" followed by "That's not what I meant." Its first text delta was " Sunday": a guess at the correction the person had not given.
- Nothing after the model rewrote a value. The update reply repeated what the model had just said, which is why the invented day reached both replies.

The persona file (`$VOICE_HOME/live/prompt.txt`, `sha256:02700e254b8f`) already said "Use exactly the numbers, times, days and names in the note. If they corrected themselves, use only the corrected value." The model broke that rule. So a prompt rule is not enough, and this fix is code (inferred from one measured failure and one measured prompt violation; the persona had the rule on every recorded run).

Why the correction mattered: on the 8-turn run C the same words came after a turn that said "tomorrow", and the reply stayed on "tomorrow" (`ablation/runC/server/turn_007.json`) because "tomorrow" was in the call. On the demo, the day the person had said was in an earlier turn and the model had no note to read it from.

## How often, across every record on disk

`$VOICE_HOME/evidence/live/scan_facts.py` reads every `server/turn_*.json` record and asks whether each day, date, time, number and place in a reply appears in the person's own words from this turn or an earlier one, or in a machine item the voice session received before that reply.

Measured before this fix: 13 runs, 81 turns, 2 ungrounded facts. Both are turn 3 of `demo-runC`, one in each of its two replies, and both are the same word.

| Run | Turn | Person | Reply | Ungrounded |
| --- | --- | --- | --- | --- |
| `ablation/demo-runC` | 3 (snapshot) | "That's not what I meant." | "I'm running the task to get the weather in Denver for Sunday." | weekday Sunday |
| `ablation/demo-runC` | 3 (update) | "That's not what I meant." | "The task to get the weather in Denver for Sunday has been completed, ..." | weekday Sunday |

No description, number, date, time or place was ungrounded in any of those turns (measured). One turn in 81 is one case, not a pattern in the demos on disk (measured). The mechanism is not specific to demos: any turn whose note loses the subject, followed by a person who says only that they meant something else, reaches the same state (inferred).

The scan's blind spots are the check's blind spots, listed under [What the check cannot see](#what-the-check-cannot-see). It also reads only text, so it says nothing about what the audio said beyond the recorded transcripts.

## The check

`voice/live/facts.py` reads five kinds of fact from a string: weekday names, month plus day, clock times (digits, am/pm, or a spelled hour after "at"), numbers (digits), and place names that follow a locative preposition. Sources read spelled-out numbers too, so "at seven" grounds "7 pm", and read lower-case place names, so an ASR partial "in denver" grounds "Denver".

`ungrounded(reply, sources)` returns the facts in the reply no source states. The sources are the person's words from this call and the newest machine note. The assistant's own earlier replies are deliberately not a source: a wrong value it said once must not become allowed the next turn.

What the live turn does with a failing reply (`voice/prototype/run.py`, `answer`):

1. It is not spoken. The turn's timeline records `fact_check` with the reply text and the ungrounded values.
2. The turn sends one machine item, `[fact check, machine-generated, not spoken by the user]`, saying the last reply named a value nothing in the call states, and telling the model to answer again with only what the person said or the note states, saying plainly if it does not have the value. The note names no value itself, so it cannot feed the guess back.
3. It asks once more. If that reply is grounded, it is the one spoken and the one in the record.
4. If the second reply also names an ungrounded value, nothing is spoken. The record keeps `text: ""` and `blocked: {reply, ungrounded}` with the text it refused.

There is no substitution. A wrong value is never replaced by a different wrong value.

Live mode now speaks only what the turn hands it: `Conversation.speak` sends `reply_start`, 40 ms frames and `reply_end` for the checked reply. Reply audio from the voice session no longer goes straight to the client, so nothing unchecked is heard. The reply WAVs are still written under `--out` and the client still gets `barge_in` to flush what it buffered.

## The one GPU run

Configuration: run C's flags (`--snapshot reply-no-utterance`), the three-turn demo script, turn 1 from Mason's own clip, turns 2 and 3 rendered. The difference from `demo-runC`, the last run of this call: the reply fact check.

Expected to learn: whether turn 3 stops naming Sunday, whether the parrot, narration and clarification checks stay clean, and what the check costs in first-audio latency.

Outcome (measured, `ablation/demo-facts/`):

| Turn | Person | Reply, as spoken | First reply audio after the turn's audio |
| --- | --- | --- | --- |
| 1 | "What's the weather in Denver tomorrow?" | "I'm running the task to get the weather in Denver tomorrow." then the forecast | 0.586 s (was 0.514 s) |
| 2 | "Then what about Saturday?" | "I'm running the task to get the weather in Denver for Saturday." then the task update | 0.492 s (was 0.505 s) |
| 3 | "That's not what I meant." | "I do not have the information needed to answer the fact check." twice | 1.128 s (was 0.520 s) |

Turn 3's first answer was the same Sunday sentence as before and was dropped (the timeline holds it at `t` 2.803 with `ungrounded: ["weekday Sunday"]`). The re-ask at `t` 3.4 produced a reply with no fact in it. The update reply repeated the Sunday sentence and was dropped the same way at `t` 7.614. Neither reached the client.

`score.py` on both runs (measured): `demo-runC` has turn 3 flagged with `weekday Sunday` in both replies; `demo-facts` has no ungrounded fact in any of the three turns. Parroting, last-sentence echo, third-person narration and clarification asks are none in both runs, and turn 2 still answers with Denver and Saturday (recall ok), so the fix does not reopen the parroting failure.

Costs (measured): +0.61 s to first audio on the one turn that had to be re-asked. Turns whose replies pass cost nothing measurable beyond run to run noise (0.586 s and 0.492 s against 0.514 s and 0.505 s).

Not fixed, and visible in the demo: the model answered "I do not have the information needed to answer the fact check." It names the machine note, which the persona tells it never to mention, and it does not say which value it lacks. It is not a wrong fact, but it is the wrong register. The smallest next change is the wording of that note, and it needs its own run (guess that a wording change alone removes it).

## Tests

| Suite | Result |
| --- | --- |
| `voice/live/tests/test_facts.py` | 9 pass. `test_weekday_invented_from_correction` is the three recorded turns as a fixture: turn 3's reply must be caught, and its only ungrounded value must be Sunday. Written before the fix and failing on it (`Lists differ: [] != ['Sunday']`), then passing. |
| `voice/prototype/tests` | 50 pass, up from 47. Three new: the invented day is asked again and the record keeps the spoken text, a second invented value is not spoken and is kept in the record, and a grounded Saturday reply is spoken once with no re-ask. |
| `voice/live/tests` | 48 tests, 44 pass. The 4 errors are `test_client_recording`, which needs `sounddevice`; that package is in the client venv, not the server venv, and the same 4 failed before this change. |
| `voice/segmentation/tests` | 89 pass, unchanged. |

## What the check cannot see

- A place named without a preposition ("Boulder looks clear").
- A number spelled out in a reply ("seventy degrees").
- A relative day with no anchor ("tomorrow", "the day after"): the check reads weekdays, dates, times, numbers and places, not deictic words.
- A value the person said in words the patterns do not read, such as "May" (dropped on purpose, it is also a verb) or a lowercase place name in the reply.
- Whether the reply is sensible. It only stops values that nothing in the call states; it cannot tell a calm sentence from a strange one, as the "fact check" wording shows.
- Anything the model says in audio that its text deltas do not carry. The transcript deltas and the audio matched on this run (measured, both replies' audio came from the accepted re-asks).

## Reproduce

```
python "$VOICE_HOME/evidence/live/scan_facts.py" --repo <checkout>
python "$VOICE_HOME/evidence/live/memory-20261116/ablation/score.py" --repo <checkout> --run "$VOICE_HOME/evidence/live/memory-20261116/ablation/demo-runC" --run "$VOICE_HOME/evidence/live/memory-20261116/ablation/demo-facts"
python -m unittest discover -s voice/live/tests -t . ; python -m unittest discover -s voice/prototype/tests -t .
```

The GPU run is `ablation/run.py --snapshot reply-no-utterance --turns-a turns-demo.txt --clips <01_single.wav> <clips-a/turn_002.wav> <clips-a/turn_003.wav>`. The first attempt at it, `ablation/demo-facts-cutshort/`, handed the client one clip instead of three and stopped after turn 1; it is kept as evidence of the mistake and proves nothing.

## Files

| Path | Contents | Size |
| --- | --- | --- |
| `voice/live/facts.py` | the extractor and `ungrounded` | 8.0 KB |
| `voice/live/tests/test_facts.py` | the three-turn fixture and the fact cases | 4.5 KB |
| `$VOICE_HOME/evidence/live/scan_facts.py` | the scan over every record | 4.6 KB |
| `$VOICE_HOME/evidence/live/memory-20261116/ablation/demo-facts/` | the run's records, client logs and audio | 5.1 MB |
| `$VOICE_HOME/evidence/live/memory-20261116/ablation/demo-facts-cutshort/` | the aborted first attempt | 1.6 MB |
| `$VOICE_HOME/evidence/live/memory-20261116/ablation/scores-demo-facts.json` | `score.py`'s output for both demos | 4.7 KB |

No model, package or venv was added.
