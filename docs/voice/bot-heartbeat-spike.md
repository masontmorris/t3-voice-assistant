# Bot heartbeat spike: a gated check-in that wakes a PM thread

The spike shows that a process outside T3 can watch worker threads, ask the free decider only when something changed or went quiet, and start a turn in a PM thread. All four exit checks passed on real runs on 2026-11-16 (measured). The T3 Tots token's `orchestration:operate` scope was enough, so the fork needs no change (measured).

The code is in [`voice/bot/`](../../voice/bot). This page explains the design as built, the evidence, and the gaps.

## The pipeline as built

`heartbeat.mjs run` is one Node process with these stages:

1. **Listener.** It subscribes to `orchestration.subscribeShell` on T3's WebSocket, the same stream `t3listen` follows. The first snapshot seeds each node's state. Each `thread.updated` item updates the node's status and its last-event time. There is no polling.
2. **Deterministic gate.** A timer ticks every `tickSec`. For each worker, `gate()` compares a fingerprint of the thread (`status`, `activeRunId`, `latestRunId`, and the pending request id) with the one seen at the last check-in. A changed field is a `change` trigger. If nothing changed, the worker's silence timer may give a `silence` trigger. Otherwise the tick ends with no model call.
3. **System one.** On a trigger, the bot sends the free decider one Decisions request. The request holds the worker's charter, status, quiet time, last reply (300 characters at most), the PM's charter, the trigger and the delta. The single question is a choice among `stay_quiet`, `wake_pm` and `tell_mason`.
4. **Wake.** On `wake_pm`, the bot sends a `message.dispatch` to the PM thread. This starts a turn, or queues one if the PM is running. When the PM's run completes, the bot reads the thread and logs `wake_confirmed` with the run id. `tell_mason` is logged only.
5. **Backstop.** If a PM has had no check-in for `backstopSec`, the bot wakes it without asking system one.

The silence timer first fires `silenceSec` after the worker's last event. Each later silence ask waits twice as long as the one before it, and any change resets the wait. A worker that stays quiet for an hour therefore costs a handful of calls, not one per tick (inferred from the schedule, not run for an hour).

The bot fails open in two ways. An unreadable decider answer counts as `wake_pm`, and the backstop wakes the PM whatever system one says.

## Data shapes

- **Node.** `role` (`orchestrator`, `pm` or `worker`), `threadId`, `parent`, `charter`, plus `status` and `lastEventAt` from the stream. The spike's config has one PM and one worker.
- **Check-in log entry** (`state/checkins.jsonl`). `at`, `node`, `trigger` (`change`, `silence` or `backstop`), `delta`, `verdict`, `reason`, `source` (`system_one`, `forced` or `backstop`), and `cached`. A wake adds `wakeMessageId`, and a later `wake_confirmed` entry names the run it started.
- **Tick ledger** (`state/ticks.jsonl`). One line per tick with its triggers and the number of model calls it made.
- **Call ledger** (`state/calls.jsonl`). One line per HTTP attempt to the decider. The client refuses to call once the ledger reaches the budget of 60.

The `state/` folder is git-ignored. It holds thread text and is never committed.

## Step 0: a client outside T3 can start a turn

`step0.mjs` launched a throwaway Haiku thread with `orchestration.launchThread` and waited on the stream until its first run completed. It then sent a second message with `message.dispatch`. T3 started run ordinal 2 for that message, and the run completed with the reply "woken" 2.5 s after the send (measured, 08:39:43.1 to 08:39:45.5 UTC). Both calls used only the T3 Tots token, whose scopes are `orchestration:read` and `orchestration:operate`.

## Exit check evidence

Two runs on 2026-11-16 used one test PM thread (id ending `b44a0bf3`) and one test worker thread (id ending `6b9bdac8`) in the scratch project, both on `claude-haiku-5-5` at low effort. The worker's charter says it needs a build number from its PM, and its only reply says it is blocked waiting for one.

| Check | Result | Evidence (UTC, measured) |
| ----- | ------ | ------------------------ |
| 1. A quiet worker gets a verdict, and `wake_pm` starts a PM turn | Passed | Run 1, silence at 53 s quiet: system one chose `wake_pm` with p=0.97 at 08:42:14.7. PM run ordinal 2 was requested at 08:42:14.769 and completed at 08:42:22.2. A second silence ask at 93 s quiet started PM run ordinal 3. A separate read through T3's thread reader showed both runs, each with the heartbeat message and a completed reply. |
| 2. A tick with no change makes no model call | Passed | Across both runs, 28 of 33 ticks had no trigger and made 0 calls. The other 5 ticks made 1 call each. The call ledger has exactly 5 attempts, all HTTP 200. |
| 3. The backstop fires while system one keeps saying `stay_quiet` | Passed | Run 2 set the test switch `forceVerdict: "stay_quiet"`, `silenceSec` 30 and `backstopSec` 100. The bot forced `stay_quiet` three times: once on a change and twice on silence. Each log line says that the test switch overrode the model's real `wake_pm`. At 08:46:15.8 the backstop woke the PM without a model call, and PM run ordinal 4 completed at 08:46:17.8. |
| 4. Every verdict is logged with node, delta, verdict and reason | Passed | `checkins.jsonl` has 6 verdict entries (5 from system one and 1 from the backstop) and 3 `wake_confirmed` entries. Every verdict entry has all four fields plus its source. |

In run 2, the nudge's whole turn started and finished between two ticks. The gate saw that as one change (`latestRunId` from ordinal 1 to ordinal 2), so the decider was asked once for it, not twice (measured).

The decider answered in 115 to 234 ms per call (measured, ledger `ms`). Every response reported `usage.cost` 0 (measured). This was never a timing run, so treat those latencies as rough.

## Decisions that differ from the brief

- **Node, not Python, for the decider client.** `decider.mjs` ports `voice/segmentation/client.py` from `voice/stage2-hybrid`. It keeps the request-hash cache, the ledger, the attempt budget, the `:free` assertion, the billing stop and the 429 backoff. One Node process now does the stream, the gate, the call and the wake. Node 22 has `WebSocket` and `fetch` built in, so the bot needs no dependencies.
- **Own T3 client, not `t3ctl` as a subprocess.** `t3ctl new` writes `started-here.json` into the T3 Tots folder, and the live `t3listen` auto-resumes the threads listed there. Using it would have written to a read-only folder and coupled the test threads to the live listener. `t3.mjs` instead makes the same RPC calls in about 140 lines.
- **The test switch still asks system one.** Under `forceVerdict`, the bot sends the real request and then overrides the answer. The log keeps the model's own choice next to the forced one. That cost 3 calls and shows that the decider would have woken the PM each time.
- **A `nudge` command.** It sends the worker one message so the gate sees a real change during run 2.

## Gaps

- **The verdict question is untested on healthy workers.** Every one of the 5 calls chose `wake_pm` with p of about 0.97, because the only worker was blocked by design (measured). Whether the decider says `stay_quiet` for a worker that is busy or done, and `tell_mason` for one that needs money or a credential, is not yet known. A small labelled set of worker states would settle that for a few calls.
- **No dedupe across PM wakes.** In run 1 the PM was woken twice about the same stall, 40 s apart. A real bot needs to remember that the PM already saw this stall, or the PM turns will cost more than the gate saves (inferred).
- **Rule 1 is not enforced.** The gate treats a finished run as a change and asks system one, though T3 already wakes the parent on run completion. The real bot should drop run-finished deltas for workers that are T3 children of their PM (inferred).
- **One PM, one worker, no orchestrator.** The node tree, reconnects and state across restarts were out of scope.

## Rerun the spike

The scripts read the T3 Tots token from `T3_TOTS_DIR` and the OpenRouter key from `OPENROUTER_API_KEY` or the env file named by `VOICE_ENV_FILE`. They never print either.

```sh
cd voice/bot
node step0.mjs <scratch project id>
node heartbeat.mjs setup <scratch project id>
echo '{"runSec":150}' > state/run1.json
node heartbeat.mjs run state/run1.json
echo '{"runSec":200,"silenceSec":30,"backstopSec":100,"forceVerdict":"stay_quiet"}' > state/run2.json
node heartbeat.mjs run state/run2.json   # in another shell, run `node heartbeat.mjs nudge` a few seconds in
node heartbeat.mjs archive
```

`setup` writes `state/config.json` with the two thread ids. Add the step 0 thread id to its `extraThreads` list so that `archive` archives it too.
