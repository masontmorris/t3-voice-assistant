# Wrong facts in the voice reply, and the check that only reports them

Written 2026-11-16, rewritten 2026-11-18. Every number below is measured from records under `$VOICE_HOME/evidence/live/` except where it says inferred or guess.

**The check is a log. It never holds, cuts, mutes or regenerates an ordinary reply, and never asks the model again** (Mason, 2026-11-18). It reads each finished reply and the turn's record keeps what it would have refused. Measured on a live call (`$VOICE_HOME/evidence/live/20261118-175037`, turn 3): a legitimate reply that named a project was cut after "but I", because the project's name read as a place no one had said (`ungrounded: ["place Voice"]`), after a first attempt had been refused and asked again. First audio came 4.4 s after the speech ended.

The check was added on 2026-11-16 because the assistant invented a day. The rest of this note is that history: the failure, how often it happened, what the check reads, and two fixes that did not remove the cause. T3 lines are a separate path with their own gate: the draft is checked against its act, and the call's reply must say the draft's words and no others ([Spoken acts](spoken-acts.md)).

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

What the live turn does with a reply (`voice/prototype/run.py`, `answer`):

1. Each piece of audio goes to the speaker the moment the model makes it. Nothing waits on the check.
2. When the reply is done, `fact_check` reads its text against the sources. If anything is flagged, the timeline records `fact_check` with the reply, the ungrounded values, the machinery words, the third-person phrases, and whether the reply repeats an earlier one word for word. A clean reply records nothing.
3. That is all. No machine note is sent, the model is not asked again, no fixed line is played, and no lookup is started.

## What the check cost while it acted

Measured on 2026-11-16 and 2026-11-18, before it was made a log. The three-turn demo call re-asked turn 3 once and said a fixed line: +0.61 s to first audio on the turn that had to be re-asked (`ablation/demo-facts/`). The re-ask came back as "I do not have the information needed to answer the fact check", which names the machinery, so the check refused that too. On the 2026-11-18 call a legitimate sentence was refused for a project name, and the turn spoke the start of a note about discarding a draft in its place.

## The note's missing subject, tried and dropped

Both experiments below ran while the check acted, so "the fixed line" and "blocked" refer to behavior that no longer exists. The finding stands: the model still invents the day, and neither a subject nor a persona rule stops it.

The diagnosis written before this work was that the parroting fix left the note with a running task and no subject, so the model invented one. The smallest fix was to give the note the subject back, named by the free decision model, and it was built and run once.

What the free decision model can produce for that request (measured, 16 probe calls on 2026-11-16, records kept in `$VOICE_HOME/evidence/live/subject-probe/`): its `extractions` route returns a span of the text it is given, not a paraphrase. On the failing turn's shape, four instruction variants returned `"weather"` or `null`, never `"weather in Denver"`, and asking for a longer phrase returned `null`. Latency was 97 to 344 ms per call.

The run (`ablation/demo-subject/`, the same three-turn call as `demo-runC` and `demo-facts`, one GPU run, one server start). The latency column is turn end to the first audio the client received; on turn 3 that is the fixed line, since the re-ask's audio was dropped (the record's own `first_reply_audio_after_audio_end_s` of 1.029 s there belongs to the re-ask nobody heard):

| Turn | Person | Reply, as spoken | First reply audio after the turn's audio | Check fires | Subject |
| --- | --- | --- | --- | --- | --- |
| 1 | "What's the weather in Denver tomorrow?" | "I'm running the task to get the weather in Denver tomorrow." then the forecast | 0.638 s (was 0.586 s) | 0 | "weather" |
| 2 | "And what about Saturday?" | "I'm running the task to get the weather in Denver for Saturday." then the task update | 0.534 s (was 0.492 s) | 0 | "weather" |
| 3 | "That's not what I meant." | "Sorry, I can't confirm that right now." once; the task update said nothing | 2.266 s after the turn ended (was 1.637 s) | 4 | "weather" |

Turn 3's first answer was the same Sunday sentence as `demo-facts`, and the update's first answer was too (`ungrounded: ["weekday Sunday"]`, at `t` 2.812 and 7.706). Both re-asks came back with "I do not have the information needed to answer the fact check ..." and were refused for naming the check, so the turn said the fixed line once and dropped the update. Measured: 4 check events, 2 of them blocked, exactly 1 spoken line, and no spoken word naming the machinery.

Verdict, and it is why the subject is not in this branch: the model still invents. The subject does not remove the guess at its cause, because the reply was not missing a subject, it was missing a day, and a subject does not supply one. Latency was not the problem: the turn waited 0.1 ms for the extraction, since the tool turn covered it in full (measured at all three turns, `subject.waited_s`; the extractions themselves took 195.5, 117.1 and 343.9 ms and cost one extra decider call per turn). The screen and the fixed line are what made the turn shippable, so those stayed and the subject came out in `5949f98c32`.

What this does not say: one word from one free model on one call is not proof that no subject-shaped note can help. A note that names the value the model is reaching for, or a persona line that says not to add a day the call has not stated, is a different change and untested (inferred).


## A persona line that forbids unstated values, tried and dropped

The check keeps the invented day out of the person's ear, but the turn still costs them the fixed line, so the next smallest change was at the source. The persona already carries a numbers rule ("Use exactly the numbers, times, days and names in the note") and the model broke it on this turn, so the question this run asked is whether a rule written for the failure itself does better than a rule about where values come from. The line added, one paragraph after the numbers rule, in the persona's own register:

> Never state a day, date, time, number or place the call has not given: if the note does not carry it, say you do not have it rather than choosing one.

Measured: it changed nothing on the failing turn.

The run (`ablation/demo-persona/`) used the same three-turn call as `demo-subject`, with the line in a copy of the shared persona (`ablation/persona-fact-line.txt`, `sha256:fb60db533ad0`, against `prompt.txt`'s `02700e254b8f`). The copy went in through the server's own `--prompt`, so no other lane's persona moved, and every turn record carries `live.prompt: persona-fact-line.txt sha256:fb60db533ad0` (measured), which is how the run knows the line reached the model.

| Turn | Person | Reply, as spoken | First reply audio after the turn's audio | Check fires |
| --- | --- | --- | --- | --- |
| 1 | "What's the weather in Denver tomorrow?" | "I'm running the task to get the weather in Denver tomorrow." then "The forecast for Denver, Colorado on Tuesday, November 17th shows a high of 69 degrees Fahrenheit and a 0 percent chance of precipitation." | 0.620 s, and 1.076 s from the client's own turn end (was 0.638 s and 1.051 s) | 0 |
| 2 | "And what about Saturday?" | "I'm running the task to get the weather in Denver for Saturday." then "The task to get the weather in Denver for Saturday has been completed, but no real work was done." | 0.523 s, and 1.030 s from the client's turn end (was 0.534 s and 1.012 s) | 0 |
| 3 | "That's not what I meant." | "Sorry, I can't confirm that right now." once; the task update said nothing | 2.538 s from the client's turn end (was 2.266 s) | 4, 2 of them blocked |

Turn 3's first reply was, word for word, `demo-subject`'s: "I'm running the task to get the weather in Denver for Sunday." The check dropped it at t 2.87 with `ungrounded: ["weekday Sunday"]`. The update's first reply said "for Sunday" too ("The task to get the weather in Denver for Sunday has been completed, but no real work was done.") and was dropped at t 7.34 (measured, `server/turn_003.json`).

Turns 1 and 2 got no worse (measured): no refusal, no fallback, Denver and Saturday both kept, and `score.py` marks both turns grounded. The only wording that moved is turn 2's update, which dropped its trailing clause "as the mock worker finished without actual processing" (measured); it is still grounded and still a completed status. Recall is ok on turn 2 and parroting, last-sentence echo, third-person narration and clarification asks are none in all three turns, the same as `demo-subject`.

Both re-asks came back in the new wording, "I do not have the information needed to answer the fact check as nothing in this conversation refers to a specific day, date, time, number, or place that was named or described." Both named the check and were refused. That sentence is close to what the persona line asks for, but it arrived only after the check spoke, and the check's own note asks for exactly that, so this run does not separate the line from the note as its cause (inferred).

Verdict: the line is out. The shared persona never carried it, so this experiment adds no product change, and the branch carries this record instead.

What the run shows is a limit on persona rules. The model does not apply a rule in its own persona in the reply that invents the day, even when the rule names that exact failure. This is the second measured case of a persona rule about values being broken, the numbers rule being the first, and the persona carried that rule on every recorded run. Rewording the rule is unlikely to reach the first reply (inferred). The correction names no referent, and neither the note nor the persona supplies one without the person's own words.

What this run cannot see: one call, one seed, one model. A persona line that fails on this turn can still change other turns, and a different line with the same meaning was not tried, which the brief deliberately allowed only one of per run.

## Tests

`voice/live/tests/test_facts.py` holds the extractor cases, with `test_weekday_invented_from_correction` as the three recorded turns: turn 3's reply must be caught and its only ungrounded value must be Sunday. The turn-level tests (`voice/prototype/tests`) pin the log-only behavior: an invented weekday, a reply that names the check, a third-person reply and a place the call never named are each spoken whole, asked once, and recorded by `fact_check`; a clean reply records nothing; and a repeat of an earlier line is logged as `repeated`.

## What the check cannot see

- A place named without a preposition ("Boulder looks clear").
- A number spelled out in a reply ("seventy degrees").
- A relative day with no anchor ("tomorrow", "the day after"): the check reads weekdays, dates, times, numbers and places, not deictic words.
- A value the person said in words the patterns do not read, such as "May" (dropped on purpose, it is also a verb) or a lowercase place name in the reply.
- Whether the reply is sensible. It only stops values that nothing in the call states and the machinery words listed above; it cannot tell a calm sentence from a strange one.
- Anything the model says in audio that its text deltas do not carry.

## Reproduce

```
python "$VOICE_HOME/evidence/live/scan_facts.py" --repo <checkout>
python "$VOICE_HOME/evidence/live/memory-20261116/ablation/score.py" --repo <checkout> --run "$VOICE_HOME/evidence/live/memory-20261116/ablation/demo-runC" --run "$VOICE_HOME/evidence/live/memory-20261116/ablation/demo-facts"
python -m voice.ci.voice_tests
```

The GPU run is `ablation/run.py --snapshot reply-no-utterance --turns-a turns-demo.txt --clips <01_single.wav> <clips-a/turn_002.wav> <clips-a/turn_003.wav>`. The first attempt at it, `ablation/demo-facts-cutshort/`, handed the client one clip instead of three and stopped after turn 1; it is kept as evidence of the mistake and proves nothing. The subject run is the same command with `--out .../demo-subject` and `--turns-b` added, since `run.py` requires it even when `--clips` makes the second call unused. The persona run is `ablation/run_persona.py` with the same arguments plus `--prompt ablation/persona-fact-line.txt` and `--out .../demo-persona`.

## Files

| Path | Contents | Size |
| --- | --- | --- |
| `voice/live/facts.py` | the extractor, `ungrounded`, `names_machinery` and `speaks_of_person` | |
| `voice/live/tests/test_facts.py` | the three-turn fixture, the fact cases and the machinery words | |
| `$VOICE_HOME/evidence/live/scan_facts.py` | the scan over every record | 4.6 KB |
| `$VOICE_HOME/evidence/live/subject-probe/` | the 16 subject probes' cached requests and answers | 25 KB |
| `$VOICE_HOME/evidence/live/memory-20261116/ablation/demo-facts/` | the run's records, client logs and audio | 5.1 MB |
| `$VOICE_HOME/evidence/live/memory-20261116/ablation/demo-subject/` | the subject run's records, client logs and audio | 6.4 MB |
| `$VOICE_HOME/evidence/live/memory-20261116/ablation/demo-persona/` | the persona line's run, records, client log and audio | 5.7 MB |
| `$VOICE_HOME/evidence/live/memory-20261116/ablation/persona-fact-line.txt` | the persona copy that run used, one paragraph longer than `prompt.txt` | 1.8 KB |
| `$VOICE_HOME/evidence/live/memory-20261116/ablation/run_persona.py` | `run.py` with the server's `--prompt` passed through, so a persona test need not touch the shared file | 5.9 KB |
| `$VOICE_HOME/evidence/live/memory-20261116/ablation/demo-facts-cutshort/` | the aborted first attempt | 1.6 MB |
| `$VOICE_HOME/evidence/live/memory-20261116/ablation/scores-demo-facts.json` | `score.py`'s output for both demos | 4.7 KB |

No model, package or venv was added.

