# Bot heartbeat spike: a gated check-in that wakes a PM thread

A process outside T3 can watch worker threads, ask the free decider only when something changed or went quiet, and start a turn in the parent thread. The T3 Tots token's `orchestration:operate` scope is enough, so the fork needs no change (measured, rounds 1 and 2).

Round 2 measured whether the verdicts are right. On 12 held-out labeled cases the gate's question (v1) got 9 right. All 3 misses were missed alarms, the costly direction, and there were no needless wakes (measured). The decider does read the state. A reworded v2 failed its adoption rule, which was fixed before its held run, so the gate keeps v1. Round 2 also fixed the four defects from the design review, test first, and added repeat-wake suppression. Every path was re-run live.

Round 3 moved what the decider is bad at out of the model. Three fixed rules now catch stalls against an expected quiet time, completed runs that promise a later report, and questions left open, all with no model call. A stay_quiet answer counts only above a bar fixed from tune cases. On 22 fresh held cases, the whole gate got 19 right with 1 missed alarm, and the decider alone got 17 right with 5 missed alarms, all stalls (measured).

The code is in [`voice/bot/`](../../voice/bot). This page explains the design as built, the evidence, and the gaps.

## The pipeline as built

`heartbeat.mjs run` is one Node process. It wires `core.mjs`, which holds every decision and does no I/O, to T3, the decider and the logs. The stages are:

1. **Listener.** The bot subscribes to `orchestration.subscribeShell` on T3's WebSocket, the same stream `t3listen` follows. The first snapshot seeds each node's state. Each `thread.updated` item updates the node's status and its last-event time. There is no polling.
2. **Deterministic gate.** A timer ticks every `tickSec`. Every node with a parent is watched, and its parent is the thread the bot wakes about it. `gate()` compares a fingerprint of the thread (`status`, `activeRunId`, `latestRunId`, and the pending request id) with the one seen at the last tick. A changed field is a `change` trigger. If nothing changed, the silence timer may give a `silence` trigger. Otherwise the tick ends with no model call.
3. **Delta filter.** A change caused only by a run that the bot's own wake message started is dropped. A completed run of a node that is a T3 child of its parent is dropped too (rule 1), because T3 already wakes the parent. Failed, cancelled and interrupted child runs go through the gate. Both drops are logged with their reason.
4. **Fixed rules.** Every tick, before any model call, `ruleVerdict()` checks three rules: the stall rule, the promise rule and the question rule (see Round 3). A rule that fires wakes the parent and skips the decider for that tick.
5. **System one.** On a trigger that no rule caught, the bot sends the free decider one Decisions request. The request holds the node's charter, status, quiet time and last reply (300 characters at most), the parent's charter, the trigger and the delta. The single question is a choice among `stay_quiet`, `wake_pm` and `tell_mason`. A `stay_quiet` below the bar of 0.88 becomes a wake.
6. **Wake.** On `wake_pm`, the bot sends a `message.dispatch` to the parent thread, which starts a turn or queues one behind a running turn. When that run completes, the bot reads the thread and logs `wake_confirmed` with the run id. `tell_mason` is logged only.
7. **Backstop.** The backstop is per node. It wakes the parent without asking system one when the parent has not looked at this node for `backstopSec`.

## The rules that bound PM turns

- **Silence asks back off.** The first silence ask fires `silenceSec` after the node's last event. Each later ask waits twice as long as the one before, and a change resets the wait.
- **One wake per state.** A wake about a node is suppressed if the parent already looked at this exact fingerprint and `ceilingSec` has not passed since. A change, or the ceiling, re-arms it. Each suppressed wake is logged with the time of the earlier look and the time of the next allowed wake.
- **The backstop backs off too.** The first backstop for each node always fires, because the bot does not know what the parent saw before it started. After that, a node whose state is unchanged since the parent looked is skipped until `ceilingSec`. The bot logs the skip once per look.
- **The bot fails open.** An unreadable answer, a decider error (a spent budget, a `BILLING_STOP` file, a missing key) all produce a logged `wake_pm` whose reason names the error. These wakes follow the one-wake-per-state rule, so a decider outage costs one PM turn per stuck state per ceiling.

The spike's defaults are `backstopSec` 180 and `ceilingSec` 1440, so the ceiling is 8 backstop periods. For the standing bot I would start at a 30-minute backstop and a 4-hour ceiling (guess, not measured). That bounds a healthy, unchanging worker to one unconditional PM turn every 4 hours.

## Data shapes

- **Node.** `role` (`orchestrator`, `pm` or `worker`), `threadId`, `parent`, `charter`, an optional `quietBudgetMin`, plus `status` and `lastEventAt` from the stream.
- **Watch, one per watched node.** `seen` and the silence timer drive the gate. `lookedAt` and `lookedFp` record when the parent was last woken about this node and what the node's state was then. `lastReply` is read again on each change, and `question` holds the open question's id and the time it opened.
- **Check-in log entry** (`state/checkins.jsonl`). `at`, `node`, `trigger` (`change`, `silence` or `backstop`), `delta`, `verdict`, `reason` and `source` (`system_one`, `forced`, `fail_open`, `backstop`, `rule_stall`, `rule_promise` or `rule_question`). A system-one entry also has `modelChoice`, which differs from `verdict` when the bar or the test switch changed it. A wake adds `wakeMessageId`, or `suppressed` or `wakeError`. A dropped delta has `dropped` and a reason, and no verdict. A later `wake_confirmed` entry names the run the wake started.
- **Tick ledger** (`state/ticks.jsonl`). One line per tick with its triggers and the number of model calls it made.
- **Call ledger** (`state/calls.jsonl`). One line per HTTP attempt to the decider. The client refuses to call once the ledger reaches the budget, which is 120 from round 3.

The `state/` folder is git-ignored. It holds thread text and is never committed.

## Verdict quality

`eval/cases.json` holds 18 synthetic cases: 6 situations, with 1 dev case and 2 held cases each. Each case lists its expected verdict, and the labels were committed (`a28fe20`) before any call on them. `eval/run.mjs` builds every request with the gate's own `verdictRequest`, so the set tests the request the gate sends.

### v1 on the held cases: 9 of 12, every miss a missed alarm

Rows are the expected verdict, and columns are what the decider chose (measured, `eval/results-v1-all.json`).

| expected \ got | `stay_quiet` | `wake_pm` | `tell_mason` |
| -------------- | ------------ | --------- | ------------ |
| `stay_quiet`   | 6            | 0         | 0            |
| `wake_pm`      | 2            | 2         | 0            |
| `tell_mason`   | 1            | 0         | 1            |

- **s2, stalled.** The node is completed and has been quiet for 60 minutes, and its last reply says it will report back once a background export finishes. v1 chose `stay_quiet` (p=0.93).
- **w2, waiting on a PM answer.** The change is a newly opened question card. v1 chose `stay_quiet` (p=0.77).
- **m3, waiting on Mason.** The reply says the decision is the owner's. v1 chose `stay_quiet` (p=0.81).

On the 6 dev cases, v1 got 5 right. Its one miss, s1, was also a missed alarm: the worker was quiet for 45 minutes while "running" and had a calm "will check back" reply.

A missed alarm is the costly error. The backstop still catches the node, but only after `backstopSec`. A needless wake costs one PM turn. With 12 held cases, each case moves accuracy by about 8 points, so treat 9 of 12 as rough.

### The decider reads the state

Round 1 saw `wake_pm` at about p=0.97 on every call, but every one of those workers was blocked. Round 2 rules out the worry that the verdict tracks only the question (measured).

- Over an empty state, the same question got an unsure `stay_quiet` (0.59, against 0.32 for `wake_pm`).
- Across the 18 cases the verdict follows the state, from `stay_quiet` at 1.00 for finished work to `wake_pm` at 0.98 for w1 and `tell_mason` at 0.93 for m2.
- The alarming-reply cases (a1, a2, a3), with words like `FAILED` and `Error` in healthy states, all got `stay_quiet` at 0.94 or higher.

### v2 traded one missed alarm for two needless wakes

The protocol in `cases.json` allowed one rewording, tuned on dev cases only. v2 (`eval/v2.mjs`) changes three things from v1, all aimed at s1. It gives quiet time in minutes. Its instructions say to weigh quiet time, status and last reply together. Its stall criterion names long quiet and waiting on a background job. I had seen the held results before writing v2, so v2 leaves alone the question-card and owner's-decision wording that the held misses point to.

v2 got 6 of 6 dev cases. Then, frozen at `0382375`, it got 8 of 12 held cases (measured, `eval/results-v2-held.json`). It caught s2 but still missed w2 and m3, and it woke the PM needlessly on h2 and h3, healthy runs quiet for 4 and 10 minutes. The adoption rule allowed at most 1 needless alert, so `core.mjs` keeps v1.

The two misses that both versions share point to a gate rule, not more wording. An open question card is the "worker waiting on an answer nobody saw" case, and the fingerprint already sees it as `pendingRequest`. A deterministic wake when a question stays open past a timeout would catch w2 with no model call (inferred, not built). The owner's-decision miss (m3) needs labeled cases of its own before a rewording is worth testing.

## Step 0: a client outside T3 can start a turn

`step0.mjs` launches a throwaway Haiku thread with `orchestration.launchThread`, waits on the stream until its first run completes, and then sends a second message with `message.dispatch`. In round 1 the second run completed 2.5 s after the send, with the reply "woken" (measured). Round 2 re-ran it with the threads in `approval-required`, the narrowest runtime mode. The second run completed 2.2 s after the send, again with "woken" (measured). Both calls use only the T3 Tots token.

## Round 1 exit checks

Round 1 used one test PM and one test worker on `claude-haiku-5-5` at low effort.

| Check | Result | Evidence (UTC, measured) |
| ----- | ------ | ------------------------ |
| 1. A quiet worker gets a verdict, and `wake_pm` starts a PM turn | Passed | At 53 s quiet, system one chose `wake_pm` with p=0.97. PM run ordinal 2 was requested at 08:42:14.769 and completed at 08:42:22.2. A separate read through T3's thread reader confirmed it. |
| 2. A tick with no change makes no model call | Passed | 28 of 33 ticks had no trigger and made 0 calls. The ledger has exactly 5 attempts. |
| 3. The backstop fires while system one keeps saying `stay_quiet` | Passed | With the test switch `forceVerdict: "stay_quiet"`, the backstop woke the PM at 100 s without a model call. PM run ordinal 4 completed at 08:46:17.8. |
| 4. Every verdict is logged with node, delta, verdict and reason | Passed | `checkins.jsonl` has 6 verdict entries. By their `source` field, 2 are from system one, 3 are forced (the model was asked, then the test switch overrode it), and 1 is from the backstop. Every entry has all four fields. |

## Round 2 fixes, tested first

Each fix has a test in `test/core.test.mjs` that drives the core with a fake clock, a fake T3 and a fake decider. Commit `910c7cf` moves the decisions into `core.mjs` with no change in behavior. On that core, all 6 tests in `8537bd2` fail. On `0653aab` all 6 pass (measured, `node --test`).

| Defect | Test | Before the fix | After |
| ------ | ---- | -------------- | ----- |
| 1. The backstop was per PM, so a noisy worker reset a quiet worker's clock | A noisy worker wakes the PM every tick. The quiet worker must still get a backstop within 150 s. | No backstop for the quiet worker | A backstop within 150 s |
| 2. A decider error threw, wrote no check-in, and woke nobody | The decider throws "call budget of 60 attempts used" | No entry and no wake | A logged `wake_pm` whose reason names the budget, and one wake |
| 3. The backstop never backed off | A healthy, unchanged worker for 1000 s with a 100 s backstop and a 500 s ceiling | Wakes at 100, 200, 300 and so on up to 1000 s | Wakes at 100 and 600 s, with the skip logged |
| 4. The bot would trigger itself in a deeper tree | An orchestrator, a watched PM and a stalled worker. The bot's wake starts a run in the PM, and later the PM opens a question. | The PM was not watched at all | The wake run is dropped, and the question wakes the orchestrator once |
| 4, rule 1. Finished runs of T3 children went to the decider | A child and a non-child of the PM both finish a run | Both asked | The child's delta is dropped with a "rule 1" reason, and the non-child's is asked |
| Repeat wakes | A stalled worker gets `wake_pm` at 30 s and at 90 s, then changes | 2 wakes for one stall | 1 wake, then 1 suppressed entry, then a new wake after the change |

The one-PM, two-worker live runs cannot show defect 4, because no PM is watched there. The test covers it.

## Round 2 live runs

Round 2 used a test PM, a stalled worker (blocked on a build number) and a finished worker (done, needs nothing). All three were on Haiku at low effort in `approval-required`, and all are archived. The runs exercise every path edited since round 1's evidence runs (measured, 2026-11-16 UTC).

- **Run A, real verdicts.** The stalled worker got `wake_pm` (p=0.94) at 41 s quiet, and PM run 2 confirmed the wake. At 121 s its second `wake_pm` was logged as suppressed. The finished worker got `stay_quiet` (p=0.99) twice. Its first backstop fired at 120 s (PM run 3). 120 s after the PM looked at the stalled worker, its backstop was skipped and logged, because its state had not changed since that look.
- **Run B, forced `stay_quiet`.** A nudge started and finished a run on the stalled worker. The gate asked about each change, and the switch overrode the model's real `wake_pm` each time, with each override logged. Both per-worker backstops fired at 100 s (PM runs 4 and 5).
- **Run C, decider error.** With `budget` set to 0, every call threw before any HTTP request. Both silence triggers logged `wake_pm` with the reason "decider error (call budget of 0 attempts used), failing open", and both woke the PM (runs 6 and 7). The ledger did not move.

Across the three runs, the log has 16 verdict entries, and none lacks node, delta, verdict or reason. 26 of 37 ticks had no trigger and made 0 calls.

## Round 3: fixed rules for what the decider can't judge

Round 2's probabilities showed the decider could not tell a stall from a healthy long run. Its missed stalls got stay_quiet at 0.94 and 0.93, and healthy runs got 0.93 to 0.98. The likely reason is that a stall is quiet time measured against an expectation, and the state gave the model none (inferred by the design thread). Round 3 gives each node an expectation and turns stalls and open questions into rules.

### The rules

- **Stall rule.** A running node that has been quiet longer than its budget wakes its parent. `budgetFor()` takes the first of these that exists: the node's `quietBudgetMin` in config; a charter line such as "Expect progress every 15 min"; twice the worker's own estimate in its last reply ("it takes about 10 minutes" gives 20); the role default (worker 30 min, PM 60, orchestrator 120). The reason names where the budget came from.
- **Promise rule.** A completed run whose last reply promises a later report ("I'll report back when", "once it finishes") wakes the parent after `promiseGraceSec` (default 300), if nothing is running and T3 lists no background task. Nothing can deliver that report.
- **Question rule.** A question open longer than `questionTimeoutSec` (default 600) wakes the parent. The open time comes from the request's `createdAt`.
- **The bar.** `applyBar()` accepts `stay_quiet` only at p of 0.88 or more. Below the bar, the gate wakes the likelier of `wake_pm` and `tell_mason`, and `wake_pm` on a tie, because the PM can still escalate to Mason.

Each rule wake follows the one-wake-per-state rule, so a stall that the parent already saw does not wake it again until the state changes or the ceiling passes.

### What the expectation could and couldn't express

It expressed a per-node budget from config, a budget written into the charter, and a budget scaled from the worker's own estimate, with a role default behind them. On the held cases it caught all three stalls of that kind: a run quiet past the role default, a run past twice its own 5-minute estimate, and a run past its charter's 15 minutes. It also left alone a 2-hour training run 25 minutes in.

It could not express these (measured on held cases, or inferred where marked):
- **A loop that keeps making events.** A retry loop updates the thread, so its quiet time stays short. The decider caught the held one (s3-loop, wake_pm with stay_quiet at 0.03). The rule never could.
- **Waiting that is not phrased as a promise.** "Waiting on the CI run to finish before I merge" matches no promise phrase. The decider gave it stay_quiet at 0.90, just above the bar, so it was the gate's one missed alarm.
- **A conditional promise.** "I will report back if anything changes" from a finished worker matched the promise rule, which woke the PM needlessly.
- **Per-step budgets.** One budget covers a whole run. A worker whose steps take very different times needs a new estimate per step, and only its replies can give one (inferred).
- **Regex limits.** The estimate parser reads digits and number words next to a cue word such as "takes", "about" or "ETA". It does not read ranges or dates (inferred from the code).

### The bar, fixed from tune cases only

`eval/r3/cases.json` stated the bar rule before any call. On the 8 tune cases, the highest bar with no needless wake was 0.88: their lowest healthy stay_quiet was 0.895, and their highest alarm stay_quiet was 0.127. The bar was committed (`f6e395a`) before the held run.

On the 22 held cases the bar decided nothing (measured). Every other alarm that reached the decider got stay_quiet at 0.15 or less, and every stay_quiet it gave was 0.89 or more. The exception, the one missed alarm s3-ci-wait at 0.90, sat above the bar. A bar above 0.90 would have caught it, but it would also have woken the PM needlessly on a3-429 (0.89). On fresh cases, the bar is insurance that has not paid out yet.

### Fresh labeled set: 19 of 22 for the gate, 17 of 22 for the decider alone

The set has 8 tune cases and 22 held cases, covering the six round 2 situations plus seven stalls of different shapes. Labels and predictions were committed (`39b462d`) before any call. The rules and the bar in the eval are the same `core.mjs` functions the live gate runs.

The whole gate (measured, `eval/r3/results-held.json`). Rows are expected, columns are what the gate did.

| expected \ got | `stay_quiet` | `wake_pm` | `tell_mason` |
| -------------- | ------------ | --------- | ------------ |
| `stay_quiet`   | 8            | 1         | 0            |
| `wake_pm`      | 1            | 9         | 0            |
| `tell_mason`   | 0            | 1         | 2            |

- **s3-ci-wait, missed alarm, decided by the decider.** Waiting on CI with nothing running got stay_quiet at 0.90.
- **f3-promise-phrase, needless alert, decided by the promise rule.** "I will report back if anything changes" read as a promise.
- **m3-card-token, wrong recipient, decided by the question rule.** A question asking Mason for a token woke the PM, because the question rule always wakes the PM. The PM still sees it.

The decider alone, its top choice on the same 22 cases:

| expected \ got | `stay_quiet` | `wake_pm` | `tell_mason` |
| -------------- | ------------ | --------- | ------------ |
| `stay_quiet`   | 9            | 0         | 0            |
| `wake_pm`      | 5            | 5         | 0            |
| `tell_mason`   | 0            | 0         | 3            |

All 5 misses are missed alarms, and all are stalls. They are s3-run-default (stay_quiet at 0.77), s3-estimate (0.85), s3-charter (0.58), s3-promise (0.93) and s3-ci-wait (0.90). The rules caught the first four.

The layer that decided each held case: the decider 15, the stall rule 3, the promise rule 2, the question rule 2, the bar 0.

Against my predictions in `cases.json`: the stall, promise and question rules did exactly what I predicted, including the needless alert and the wrong recipient. The bar landed inside my guessed range, but it changed no case, because the decider got both question cards and all three Mason cases right on its own. The whole gate beat my guess of 17, and the decider alone beat my guess of 14.

With 22 cases, each case moves accuracy by about 5 points, so treat these as rough.

### Tests and live runs

`test/rules.test.mjs` has 6 tests. On the round 2 core (`823563a`) they fail, and on `7527c2c` they pass, together with the 6 round 2 tests (measured). They cover: the stall rule from a configured budget; the stall rule from twice the worker's estimate; the promise rule, and its hold-off while a background task exists; the question rule; the bar routing to wake_pm or tell_mason, and passing a confident stay_quiet; a failed child run going through the gate while a completed one is dropped.

The round 3 live runs used a test PM and three Haiku workers in `approval-required`, all archived afterwards (measured, 2026-11-16 UTC).
- **Run A.** The question rule fired 90 s after the worker's question opened, and the promise rule fired at 69 s of quiet. Neither asked the decider. Both wakes started PM turns (runs 2 and 3). 20 s before its rule fired, the question worker had got stay_quiet from the decider at 0.95. The finished worker got stay_quiet at 0.99, above the bar.
- **Run B.** With a 1-minute worker budget, the stall rule fired on the worker that was still running and waiting on its question, with no model call (PM run 4).
- **Run C.** The per-worker backstop still fires after the tick changes: three wakes at 20 s (PM runs 5, 6 and 7).
- **Archive.** It interrupted the question worker's waiting run, then archived all four threads.

Across the three runs, the log has 8 verdict entries, and none lacks a field. The 14 ticks made 2 calls.

Two fixtures needed a second prompt. Haiku refused to state a false upload line, so the promise worker promised something true instead: to report back once the PM sends a bundle name. In `approval-required`, `echo` ran without an approval prompt, so the question worker opened a multiple-choice question instead.

## Decisions that differ from the briefs

- **Node, not Python, for the decider client.** `decider.mjs` ports `voice/segmentation/client.py` from `voice/stage2-hybrid`, with the same cache, ledger, budget, `:free` assertion, billing stop and 429 backoff. The bot needs no dependencies.
- **Own T3 client, not `t3ctl new`.** `t3ctl new` writes `started-here.json` into the read-only T3 Tots folder, and the live listener auto-resumes the threads listed there.
- **The test switch still asks system one,** so the log keeps the model's choice next to the forced one.
- **Fail-open wakes follow the one-wake-per-state rule.** Without the rule, a decider outage would wake the PM on every silence ask.
- **Every node with a parent is watched,** not only workers, so that fix 4 has something to fix in a deeper tree.
- **The decider's question is still v1.** Round 3 added the expectation to the rules, not to the decider's state, so the decider-alone table stays comparable with round 2.
- **The question rule always wakes the PM,** even when the question is for Mason. Telling the two apart would need a model call, which the rule exists to avoid.

## Gaps

- **Missed alarms remain.** Waiting phrased as "waiting on CI" slips past both the promise rule and the decider. The backstop catches it, but only after `backstopSec`. A wider waiting-phrase rule is the next candidate, and it needs its own false-positive cases first.
- **The promise rule fires on conditional promises.** It needs to skip "if anything changes".
- **A busy worker still costs one backstop turn per `backstopSec`.** The brief skips the backstop only for unchanged state. A worker whose runs keep changing is woken about every period, even when it is healthy (inferred from the rule).
- **The labeled sets are small and synthetic.** I wrote all 48 cases. Real worker threads would test the gate better.
- **No reconnects and no state across restarts.** A restart forgets what each parent has looked at, so every node's first backstop fires again.

## Rerun the spike

The scripts read the T3 Tots token from `T3_TOTS_DIR` and the OpenRouter key from `OPENROUTER_API_KEY` or the env file named by `VOICE_ENV_FILE`. They never print either.

```sh
cd voice/bot
node --test
node eval/r3/run.mjs tune
node eval/r3/run.mjs held
node heartbeat.mjs setup <scratch project id>
echo '{"runSec":90,"silenceSec":60,"backstopSec":100000,"promiseGraceSec":60,"questionTimeoutSec":90}' > state/runA.json
node heartbeat.mjs run state/runA.json
echo '{"runSec":40,"silenceSec":600,"backstopSec":100000,"promiseGraceSec":1000000,"questionTimeoutSec":1000000,"roleBudgetMin":{"worker":1}}' > state/runB.json
node heartbeat.mjs run state/runB.json
echo '{"runSec":40,"silenceSec":600,"backstopSec":20,"promiseGraceSec":1000000,"questionTimeoutSec":1000000}' > state/runC.json
node heartbeat.mjs run state/runC.json
node heartbeat.mjs archive
```

`setup` writes `state/config.json` with the four thread ids. If a worker does not reply with the phrase or question it was asked for, send it the second prompt from Round 3 before run A. Cached decider answers in `state/cache` make a rerun of either eval free. The round 1 and round 2 runs need the setup from PR 8.
