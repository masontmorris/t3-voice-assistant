# Bot heartbeat spike: a gated check-in that wakes a PM thread

A process outside T3 can watch worker threads, ask the free decider only when something changed or went quiet, and start a turn in the parent thread. The T3 Tots token's `orchestration:operate` scope is enough, so the fork needs no change (measured, rounds 1 and 2).

Round 2 measured whether the verdicts are right. On 12 held-out labeled cases the gate's question (v1) got 9 right. All 3 misses were missed alarms, the costly direction, and there were no needless wakes (measured). The decider does read the state. A reworded v2 failed its adoption rule, which was fixed before its held run, so the gate keeps v1. Round 2 also fixed the four defects from the design review, test first, and added repeat-wake suppression. Every path was re-run live.

The code is in [`voice/bot/`](../../voice/bot). This page explains the design as built, the evidence, and the gaps.

## The pipeline as built

`heartbeat.mjs run` is one Node process. It wires `core.mjs`, which holds every decision and does no I/O, to T3, the decider and the logs. The stages are:

1. **Listener.** The bot subscribes to `orchestration.subscribeShell` on T3's WebSocket, the same stream `t3listen` follows. The first snapshot seeds each node's state. Each `thread.updated` item updates the node's status and its last-event time. There is no polling.
2. **Deterministic gate.** A timer ticks every `tickSec`. Every node with a parent is watched, and its parent is the thread the bot wakes about it. `gate()` compares a fingerprint of the thread (`status`, `activeRunId`, `latestRunId`, and the pending request id) with the one seen at the last tick. A changed field is a `change` trigger. If nothing changed, the silence timer may give a `silence` trigger. Otherwise the tick ends with no model call.
3. **Delta filter.** A change caused only by a run that the bot's own wake message started is dropped. A finished run of a node that is a T3 child of its parent is dropped too (rule 1), because T3 already wakes the parent. Both drops are logged with their reason.
4. **System one.** On a trigger, the bot sends the free decider one Decisions request. The request holds the node's charter, status, quiet time and last reply (300 characters at most), the parent's charter, the trigger and the delta. The single question is a choice among `stay_quiet`, `wake_pm` and `tell_mason`.
5. **Wake.** On `wake_pm`, the bot sends a `message.dispatch` to the parent thread, which starts a turn or queues one behind a running turn. When that run completes, the bot reads the thread and logs `wake_confirmed` with the run id. `tell_mason` is logged only.
6. **Backstop.** The backstop is per node. It wakes the parent without asking system one when the parent has not looked at this node for `backstopSec`.

## The rules that bound PM turns

- **Silence asks back off.** The first silence ask fires `silenceSec` after the node's last event. Each later ask waits twice as long as the one before, and a change resets the wait.
- **One wake per state.** A wake about a node is suppressed if the parent already looked at this exact fingerprint and `ceilingSec` has not passed since. A change, or the ceiling, re-arms it. Each suppressed wake is logged with the time of the earlier look and the time of the next allowed wake.
- **The backstop backs off too.** The first backstop for each node always fires, because the bot does not know what the parent saw before it started. After that, a node whose state is unchanged since the parent looked is skipped until `ceilingSec`. The bot logs the skip once per look.
- **The bot fails open.** An unreadable answer, a decider error (a spent budget, a `BILLING_STOP` file, a missing key) and a failed T3 read all produce a logged `wake_pm` whose reason names the error. These wakes follow the one-wake-per-state rule, so a decider outage costs one PM turn per stuck state per ceiling.

The spike's defaults are `backstopSec` 180 and `ceilingSec` 1440, so the ceiling is 8 backstop periods. For the standing bot I would start at a 30-minute backstop and a 4-hour ceiling (guess, not measured). That bounds a healthy, unchanging worker to one unconditional PM turn every 4 hours.

## Data shapes

- **Node.** `role` (`orchestrator`, `pm` or `worker`), `threadId`, `parent`, `charter`, plus `status` and `lastEventAt` from the stream.
- **Watch, one per watched node.** `seen` and the silence timer drive the gate. `lookedAt` and `lookedFp` record when the parent was last woken about this node and what the node's state was then.
- **Check-in log entry** (`state/checkins.jsonl`). `at`, `node`, `trigger` (`change`, `silence` or `backstop`), `delta`, `verdict`, `reason` and `source` (`system_one`, `forced`, `fail_open` or `backstop`). A wake adds `wakeMessageId`, or `suppressed` or `wakeError`. A dropped delta has `dropped` and a reason, and no verdict. A later `wake_confirmed` entry names the run the wake started.
- **Tick ledger** (`state/ticks.jsonl`). One line per tick with its triggers and the number of model calls it made.
- **Call ledger** (`state/calls.jsonl`). One line per HTTP attempt to the decider. The client refuses to call once the ledger reaches the budget of 60.

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

## Decisions that differ from the briefs

- **Node, not Python, for the decider client.** `decider.mjs` ports `voice/segmentation/client.py` from `voice/stage2-hybrid`, with the same cache, ledger, budget, `:free` assertion, billing stop and 429 backoff. The bot needs no dependencies.
- **Own T3 client, not `t3ctl new`.** `t3ctl new` writes `started-here.json` into the read-only T3 Tots folder, and the live listener auto-resumes the threads listed there.
- **The test switch still asks system one,** so the log keeps the model's choice next to the forced one.
- **Fail-open wakes follow the one-wake-per-state rule.** Without the rule, a decider outage would wake the PM on every silence ask.
- **Every node with a parent is watched,** not only workers, so that fix 4 has something to fix in a deeper tree.

## Gaps

- **Missed alarms remain.** v1 misses an open question card and an owner's decision. The backstop catches both, but only after `backstopSec`.
- **A busy worker still costs one backstop turn per `backstopSec`.** The brief skips the backstop only for unchanged state. A worker whose runs keep changing is woken about every period, even when it is healthy (inferred from the rule).
- **Rule 1 also drops failed and cancelled runs of T3 children.** This assumes that T3 wakes the parent on those ends too (inferred from `t3listen`'s events, not verified in T3).
- **The labeled set is small and synthetic.** I wrote all 18 cases. Real worker threads would test the gate better.
- **No reconnects and no state across restarts.** A restart forgets what each parent has looked at, so every node's first backstop fires again.

## Rerun the spike

The scripts read the T3 Tots token from `T3_TOTS_DIR` and the OpenRouter key from `OPENROUTER_API_KEY` or the env file named by `VOICE_ENV_FILE`. They never print either.

```sh
cd voice/bot
node --test
node eval/run.mjs v1 all --probe
node step0.mjs <scratch project id>
node heartbeat.mjs setup <scratch project id>
echo '{"runSec":200,"silenceSec":40,"backstopSec":120}' > state/runA.json
node heartbeat.mjs run state/runA.json
echo '{"runSec":150,"silenceSec":30,"backstopSec":100,"forceVerdict":"stay_quiet"}' > state/runB.json
node heartbeat.mjs run state/runB.json   # in another shell, run `node heartbeat.mjs nudge` a few seconds in
echo '{"runSec":50,"silenceSec":20,"backstopSec":1000,"budget":0}' > state/runC.json
node heartbeat.mjs run state/runC.json
node heartbeat.mjs archive
```

`setup` writes `state/config.json` with the three thread ids. Add the step 0 thread id to its `extraThreads` list so that `archive` archives it too. Cached decider answers in `state/cache` make a rerun of `eval/run.mjs` free.
