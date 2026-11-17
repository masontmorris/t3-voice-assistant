# Twin A: does the deterministic match plus a fast model pick the right project?

**Bottom line.** Neither arm passes the Twin B bar under the frozen golds, measured on 30
labeled requests over one frozen 11-project snapshot. Re-scored with the stated target as
gold on the four nontarget cases, a sensitivity run on the frozen rows that leaves the
manifest untouched, the model-confirm view names no wrong project and fails only the p95
latency bar at 2.3 s against 700 ms, while the deterministic arm fails only the unambiguous
gate, on A-015 out of 9 cases. The worst outcome never fired on these 30 cases: no arm named
a wrong project in any of the 120 frozen runs (measured, 0 `wrong_thread`). Every frozen
failure is cheap, a direct pick or a none answer where the gold wants a question. Ship the
deterministic match as the floor, send only its misses to the free model, and deliver every
single-project result as a confirming question. Do not ship direct picks.

Every number below carries its label in its own sentence. The tables are run output.

## What ran

**Snapshot.** One frozen list built from a read-only copy of a real T3 `statev2.sqlite` with
SQLite's read-only URI flag. It holds all 11 undeleted projects, each with its display name,
its workspace folder name, and `spokenNames` from one fixed rule written down before any
label was written: the name, the folder when it differs, and each of those with a trailing
parenthetical or bracket suffix dropped, deduped case-insensitively. The database copy was
deleted after the manifest froze. The snapshot, the spec, the labels, the run files and the
ten scored outputs stay under `$VOICE_HOME/evals/twina-pick` and are not in the repository.

**Labels.** 30 spoken requests. Ten known-hard cases were written by hand first: the four
shared-word cases, the four project-named-in-the-task cases, and two not-in-list cases. The
other 20 came from one labeler on `claude-sonnet-5-5` through T3 `delegate_task`, which saw
only the snapshot, the slice table, and the hand-written cases for wording avoidance, never
any picker output. The manifest froze the snapshot, the spec and both label files before any
picker ran, and every run verifies it.

| slice | cases | what the request does | gold |
| --- | --- | --- | --- |
| exact | 6 | says one project by its full name, plus a task | one |
| variant | 3 | a folder name, a spelled-out digit, or another non-display spoken form | one |
| partial | 6 | part of one project name or a nickname, plus a task | ask, every plausible project |
| shared | 4 | one word or name sitting in two projects | ask, both |
| nontarget | 4 | a second project named inside the task text rather than as the target | ask, both |
| absent | 4 | no project anywhere in the words | ask, empty |
| missing | 3 | an invented project with the word "project" next to it | none |

**Arms.** (1) `match`: the deterministic `spokenNames` match alone, one repeat, since it makes
no model call and cannot vary. (2) `model`: the match first, then `particle-ai/particle-1:free`
with reasoning off on every non-one, 3 repeats per case. The model's single pick is scored
twice: as a direct pick and as a confirming question, which is the design's default delivery.
One run per named configuration. Strict and lenient scoring reuse Twin B's outcome functions
through a thin pick-type bridge, so one implementation defines correct.

## Results

Cases that pass, over all repeats. A case passes only when every repeat passes.

| slice | cases | match strict | model strict / lenient / confirm / confirm-lenient |
| --- | --- | --- | --- |
| exact | 6 | 6 | 6 / 6 / 6 / 6 |
| variant | 3 | 2 | 3 / 3 / 3 / 3 |
| partial | 6 | 6 | 0 / 0 / 6 / 6 |
| shared | 4 | 4 | 4 / 4 / 4 / 4 |
| nontarget | 4 | 2 | 1 / 1 / 1 / 2 |
| absent | 4 | 4 | 0 / 0 / 0 / 0 |
| missing | 3 | 3 | 3 / 3 / 3 / 3 |
| total | 30 | 27 | 17 / 17 / 23 / 24 |

Runs, counted separately. The match arm ran 30 runs with no model call. The model arm ran 90
runs with 60 model calls, since 10 deterministic ones per repeat never reach the model.

| arm and view | correct runs | wrong runs | cheap non-correct runs |
| --- | --- | --- | --- |
| match strict | 27 | 2, both a pick where the gold asks | 1 unnecessary question |
| model strict | 53 | 25, all a direct pick where the gold asks | 12 none answers where the gold asks |
| model confirm | 66 correct + 3 confirmed + 3 `question_missing_gold` (lenient only) | 6, all a pick where the gold asks | 12 none answers where the gold asks |

Wrong means `wrong_thread` or `picked_when_should_ask`. No run in either arm produced
`wrong_thread` (measured, 0 of 120 runs on these 30 cases). As a confirming question the model was never
wrong about the project (measured, 0 `confirm_wrong` in 3 confirmations).

The pass bar, strict and confirm views (measured). The gates mirror Twin B's: zero wrong in
the shared, nontarget, absent and missing slices, at most two wrong overall, at least
90 percent correct on the unambiguous exact and variant slices, at most 10 percent
unnecessary questions, and single-request p95 at most 700 ms.

| arm | view | zero wrong in shared, nontarget, absent, missing | overall wrong | unambiguous | unnecessary | p95 | passes |
| --- | --- | --- | --- | --- | --- | --- | --- |
| match | strict | 2, fail | 2, pass | 88.9 percent, fail | 3.3 percent, pass | no request, pass | no |
| model | strict | 9, fail | 25, fail | 100 percent, pass | 0.0 percent, pass | 2341 ms, fail | no |
| model | confirm | 6, fail | 6, fail | 100 percent, pass | 0.0 percent, pass | 2341 ms, fail | no |

## Sensitivity: the stated target as gold

A sensitivity run on the frozen rows, not a new run: cases A-005 to A-008 re-scored with
the project named after "in" as a `one` gold, through the same outcome functions, with the
manifest and labels untouched and no picker re-run. Measured on the 30 match rows and 90
model rows already on disk.

| arm | view | zero wrong in shared, nontarget, absent, missing | overall wrong | unambiguous | unnecessary | p95 aside, what still fails |
| --- | --- | --- | --- | --- | --- | --- |
| match | strict | 0, pass | 0, pass | 8 of 9, fail, on A-015 | 3 of 30, pass | unambiguous only |
| model | strict | 0, pass | 16, fail, all partial-slice fragment picks | 27 of 27, pass | 3 of 90, pass | wrong and p95 |
| model | confirm | 0, pass | 0, pass | 27 of 27, pass | 3 of 90, pass | p95 only, 2341 ms |

The PM's ruling: a target stated with "in X" is X, so those four golds were too strict and
the frozen manifest stands as recorded. Under that reading the model-confirm view has 0
wrong and fails only on p95 latency at 2.3 s, and the match arm fails only the unambiguous
gate, on A-015 out of 9 cases. The frozen tables above stay as measured; this table is the
lens the recommendation rests on.

**Latency, tokens, transport (measured).** The model arm's pooled p50 sits near 600 ms per
slice and its pooled p95 at 2341 ms, with the worst slice at 2636 ms, against the 700 ms bar.
The free tier's own queueing is the limiter (inferred from a p50 far below the p95 on a
client paced 3.2 seconds apart). Reasoning held at 0 tokens on all 60 calls; content averaged
34 tokens a call. No reply was unparseable, no call failed, and the 60 calls made zero
retries. The match arm answers in under half a millisecond.

## The known failures

- Two projects share a name stem where one is a fork of the other. The shared slice asks
  correctly in all 4 cases on both arms (measured).
- A lone exact hit with a fragment of another project in the task text picks where the gold
  asks. This is the only deterministic wrong class, 2 cases, and it never reaches the model
  (measured, frozen scoring; the sensitivity re-score above reads both as correct).
- A spelled-out digit misses the deterministic match and the model resolves it (measured).
- With no project wording the model says none where the gold asks, in all 4 cases and all
  3 repeats (measured). The design treats both as a question, so this is cheap but undecided.
- A fragment of a name never picks on the deterministic arm by design. The model names the
  one plausible project directly in 16 of 18 runs, which strict scores as wrong and confirm
  scores as right (measured).

## What this metric cannot see

- It scores the pick only. A correct pick can still start the wrong work if the launch maps
  the id wrongly, which this eval does not touch.
- Text only. No ASR error, no misheard project name, no repair.
- 30 cases from one snapshot, ten of them hand-written and 20 from one model labeler. A label
  a careful reader would argue flips a case. The folder-wording variant is the shakiest gold.
- Three repeats at temperature 0 measure transport variance, not sampling variance.
- The free tier was paced to one request in flight. A deployed pick would meet rate limits
  this run never priced.
- None and ask both surface as a question in Twin A, so that confusion is cheap here in a
  way the bar does not show.
- Three confirmations is thin evidence for the confirming-question delivery.
- It never tried the B2 shapes from review: an explicit unlisted project beside a listed name
  in the task, a common-word name inside the task, a strict prefix of a longer listed name, a
  task-side exact match beside a fragmentary target, or singular/plural drift. Those are unit
  tests now, not eval cases.
- Confirming every single pick, deterministic hits included, is a policy choice that turns
  B2-class misses into spoken questions. It is not a measured result: no scored view treats
  a deterministic one as a confirming question, and the recommended configuration as a whole,
  match floor plus model misses plus confirming delivery, was never scored end to end.

## Privacy: what was sent where

Project names and spoken forms went as plain text to OpenRouter and nowhere else, in 60
calls (measured). Every call used a model id ending in `:free` asserted in code, at cost 0,
and neither log holds a 402 or billing language (measured). The labeler ran as a T3 child
agent on local data and sent nothing externally. Snapshots, labels, run files, raw replies
and the ten scored outputs stay under `$VOICE_HOME/evals/twina-pick`.

## Recommendation: what Twin A ships

1. Ship the deterministic match as the floor. It answers 27 of 30 cases in under a
   millisecond and never named a wrong project (measured).
2. Send only its misses to `particle-ai/particle-1:free` with reasoning off. The model
   itself never named a wrong project in 60 calls (measured).
3. Deliver every single-project result as a confirming question, deterministic hits
   included. Direct picks fail the bar in 25 runs under the frozen scoring; the confirming
   view fails it in 6, all carried deterministic ones (measured). This delivery itself is a
   policy choice, not a measured result; see the blind spots above.
4. Do not tune the two carried cases on this data and call it a pass. The foreign-fragment
   rule (a lone exact hit plus any fragment of another project asks) clears them in-sample
   by inspection, and it needs its own labeled round before it ships.
5. Decide the absent behavior: the model says none and the gold asks, and both surface as a
   question. Pick one wording and label it.
6. Treat the 700 ms bar as unmet at $0. Either the pick runs ahead of the utterance, or the
   bar needs the PM's revision.

## Reproduce

```text
python -m unittest discover -s voice/t3/pick/tests -t .
python -m voice.t3.pick.manifest verify $VOICE_HOME/evals/twina-pick
python -m voice.t3.pick.project_harness --arm match --repeats 1
VOICE_ENV_FILE=<path to the env file with OPENROUTER_API_KEY> python -m voice.t3.pick.project_harness --arm model --repeats 3
```

`project_harness.py` reads `$VOICE_HOME/evals/twina-pick/{snapshots,labels}` and writes
`runs/`. Every arm verifies the frozen manifest first. The snapshot comes from
`voice/t3/pick/project_snapshot.py` run against a read-only copy of a `statev2.sqlite`,
with the database copy deleted after freezing. Ten scored real outputs are at
`$VOICE_HOME/evals/twina-pick/sample-10-scored.md`, alongside every run file.
