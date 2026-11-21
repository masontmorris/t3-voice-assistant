# Bot spike 2: the standing root and the voice handoff

A request handed in from the voice side reaches one standing root thread, goes down to a persistent PM and a worker the PM delegates to, and comes back as a task result in one outbox the voice can read. All five exit checks passed on real runs on 2026-11-16 (measured). Check 4 passed only after a fix: its first run showed that a silence ask could pre-empt the question routing, so a question meant for Mason never reached him.

The prototype can use this path through one small Python adapter, `voice/bot/root_tasks.py`. A recorded clip replayed end to end with its read-only task going through the root and its side-effect task held. The audio model needs one tool only, "hand this to the root". The handoff, the outbox and the snapshot carry everything else.

The code is in [`voice/bot/`](../../voice/bot): `root.mjs` holds the data shapes and their functions, `serve.mjs` is the standing process, and `root_tasks.py` with `seam_replay.py` is the Python side.

## Expected outcomes, written before the runs

These are my predictions for each exit check. I committed them before any live run of this spike. Results follow in later sections.

1. **The root persists.** `serve.mjs setup` run twice prints "created" and then "reused" with the same root id. After the serve process is stopped and started again, `GET /registry` returns the same root and the same node list, with no new threads in T3.
2. **A handoff flows down and back.** A `POST /handoff` reaches the root, and the root's run is in the outbox as a `root_reply`. The root sends the task to the research PM with `t3_thread_send`, and the PM delegates it to one worker with `delegate_task`. The bot discovers that worker from T3 lineage and registers it under the PM. The worker's result reaches the PM, and the PM sends it to the root. The root's tagged reply is in the outbox as a `task_result` with the same request id. Guess: about 5 test-thread turns and 1 to 2 minutes end to end.
3. **A retry creates nothing new.** A second `POST /handoff` with the same request id returns `exists`, sends no message, and adds no root run.
4. **Only `tell_mason` reaches the outbox.** A worker that asks Mason for a token trips the question rule. The recipient question picks `tell_mason`, and the notice lands in the outbox with no PM wake. A worker blocked on its PM gets `wake_pm`, which wakes the PM and adds nothing to the outbox.
5. **The prototype can use this path.** Replaying recorded clip `05_topic_change` (run `run3-c`) through `RootTaskService` hands `task_2` ("how long is the drive to Boulder?") to the root and holds `task_1` ("Add milk to the shopping list."). The end-of-utterance snapshot shows `task_2` running and `task_1` held for confirmation. Later, `task_2` completes with the worker's answer as its `result_summary`, and one `task_update` is produced. The segmenter makes 0 live decider calls.

The likeliest failure I see is the tagging convention: Haiku may drop the `[request <id>]` tag from the root's result line, which would leave the result out of the outbox.

## The data shapes

All state lives in `state/tree.json`, and only the `serve.mjs` process writes it. Commands that change the tree, such as `add-test-workers`, run while the server is stopped.

- **Root.** `root` is the orchestrator's thread id. `ensureTree()` reuses it while the thread is live in T3, and creates a new root and research PM only when it is missing. The charters are placeholders (`rootCharter()`, `pmCharter()`).
- **Registry.** `nodes` holds every node: `role`, `threadId`, `parent`, `charter`, and round 3's optional `quietBudgetMin`. A node found from T3 lineage has `discovered: true`.
- **Handoff.** `handoffs[requestId]` holds the text, the utterance it came from, the time, the root message id, and a status of `sending` or `sent`. The root gets `[request <id>] <text>`. A `sending` handoff found again is checked against the root's messages before it is sent again, so a crash mid-send makes no second task.
- **Outbox.** `outbox` is one ordered list. Each entry has an `id` (`o1`, `o2`, and so on), a time, a `source` (`root_reply`, `task_result` or `tell_mason`), the `node`, the `requestId` it answers or null, the `text`, and `delivered`. A task result also has `chain` (see the hardening pass below). A completed root run started by a handoff message is that request's `root_reply`. Any other completed root run gives one `task_result` for each `[request <id>]` segment of its reply. A `tell_mason` verdict from the gate is a `tell_mason` entry.
- **Snapshot.** `snapshot()` builds the plan's `utterance_snapshot` from the handoffs and the outbox alone. A handoff with a task result is `completed`, with the result as `result_summary`, and any other handoff is `running`. Undelivered `tell_mason` entries ride along as `for_mason`. It makes no model call.

The server exposes the same shapes on `127.0.0.1:8787`: `POST /handoff`, `GET /outbox` (with a long-poll `waitMs`), `POST /outbox/ack`, `GET /snapshot` and `GET /registry`. Every route needs the shared secret from `state/api-secret` in the `x-bot-secret` header, refuses any request with an `Origin` header, and takes POST bodies only as `application/json` (see the hardening pass below).

## How node discovery works

The bot follows T3's shell stream, as the heartbeat does. Any thread whose `lineage.parentThreadId` names a registered node joins the registry one level down: under the root as a PM, and under a PM or a worker as a worker. Its charter is the thread title, which the delegating agent wrote, such as "Denver to Boulder drive time". The first snapshot after a restart repeats the pass until no new node appears, so a child created while the bot was down should be found (inferred from the code, not run).

It found all 4 workers the PM delegated to, with no help from the root or the PM (measured). It could not express these:

- **Message edges.** The root reaches the PM with `t3_thread_send`, which leaves no lineage, so the PM has to be registered at setup. The PM's received message does carry `senderThreadId` (measured in the PM thread), which a later version could read to learn such edges.
- **Top-level threads.** A thread an agent starts with `t3_thread_launch` or `create_threads` has no parent in its lineage (inferred from the schema, not tested), so it stays outside the tree.
- **Real charters.** The title is a label, not a charter, and a discovered node gets the role default for its expected quiet time.
- **Children archived while the bot was down.** They are not in the shell snapshot (inferred), so they are never registered.

## Exit check evidence

All times are UTC on 2026-11-16. The test threads were Haiku at low effort in `approval-required`, in the scratch project. All of them are archived.

1. **The root persists. Passed.** `setup` printed "created root ...124c069f" at 09:44:28 and "reused root ...124c069f" at 09:44:28.8, with 2 nodes both times. Serve A started at 09:44:32 and was force-stopped at 09:44:40. Serve B started at 09:44:45 and returned the same root, the same PM `...0a72e74b` and the same 2 nodes from `GET /registry`. T3 had exactly one live thread with each title.
2. **A handoff flows down and back. Passed.** `POST /handoff` for `check2:drive` at 09:44:59.7 returned `created`. The root replied at 09:45:10 (`o1 root_reply`). The PM delegated, and the bot discovered the worker from lineage at 09:45:16. The tagged result arrived as `o2 task_result` for `check2:drive` at 09:45:46, 47 s after the handoff.
   - The first worker stalled on a shell-command approval that `approval-required` would not grant. The PM cancelled it with `task_cancel` and delegated again with "do not run any commands", and the second worker answered. This is from the PM thread's own activity log.
3. **A retry creates nothing new. Passed.** A second `POST /handoff` for `check2:drive` at 09:46:11 returned `exists`. The root's run count stayed at 3, the root held the same 2 tagged messages (the handoff and the PM's result), and the store still had 1 handoff.
4. **Only `tell_mason` reaches the outbox. Passed on the second run.**
   - **First run (serve C, 09:47:39 to 09:52:44): failed.** The token worker's question opened at 09:47:41. At 49 s quiet, before the 60 s question timeout, a silence ask chose `wake_pm` (p=0.53) and woke the PM. Repeat-wake suppression then counted that state as seen, and the question rule never fired.
   - **The fix (`ee35e0a`, with tests that fail before it).** While a question is open, only the question rule decides, and it routes each question id once.
   - **Second run (serve D, 09:54:00 to 09:54:50).** The question rule fired, and the recipient question chose `tell_mason` at p=0.91. The outbox gained `o7`: "worker thread ... needs Mason: Please paste the Hugging Face write token for the eval dataset. Options: I'll paste it, Skip publishing." The blocked worker got `wake_pm` (p=0.93). It woke the PM (run 11) and added nothing to the outbox.
5. **The prototype can use this path. Passed.** `seam_replay.py` replayed `05_topic_change` from run `run3-c` against the live server twice, at 09:46:23 and 09:47:42. Both times:
   - `task_1` ("Add milk to the shopping list.") stayed `held_for_confirmation` and never reached the root.
   - `task_2` ("Oh, and how long is the drive to Boulder?") was handed to the root as `conv_05_seamN:task_2`. It showed as `running` in the end-of-utterance snapshot and completed 18 to 20 s after the handoff with the worker's estimate. That produced one `task_update`.
   - The bot's own `GET /snapshot` showed the same task as completed.
   - The segmenter ran on `OfflineClient`, so it made no live decider call. The prototype's call ledger is shared with another prototype run that was going on at the time, so its total moved during my run, but every entry in my window carries that run's clip labels.

The snapshot built from `tree.json` alone after the runs, with 0 calls, listed `conv_05_seam2:task_2` as completed with its result and the token notice `o7` under `for_mason`.

## The seam for check 5, and why

The seam is the prototype's task-service interface, unchanged: `submit_delegate`, `sync`, `poll`, `next_due`, `snapshot`, `take_updates` and `executions`. `RootTaskService` subclasses `MockTaskService` and replaces three of them:

- `sync` sends each dispatch that the mock would run to `POST /handoff` as `<conversation_id>:<task_id>`, and holds the rest exactly as the mock does.
- `poll` reads `task_result` entries from `GET /outbox`, and `next_due` keeps the caller polling while a task runs.

`snapshot` and `take_updates` are the mock's. They acknowledge the outbox entries they hand to the voice.

I chose this seam because `run.py` already calls exactly these methods, so swapping the service is a one-line change on either prototype branch, with no edit to `voice/prototype/`. The adapter needs only the standard library and `voice.prototype.tasks`. `replay.py` could not serve as the driver, because it never touches the task service. `seam_replay.py` does what `run.py` does around the service, with recorded transcript updates in place of live audio, so this run needed no GPU lease.

The seam leaves out cancellation, which stays local because the root is not told, and the root's `root_reply`, which the prototype has no slot for yet.

## Turns and calls

The spike spent 27 test-thread turns (measured from every run in the registry, plus the probe thread and its helper).

| Work                                                              | Turns |
| ----------------------------------------------------------------- | ----- |
| Probe of the T3 tools and `delegate_task`                         | 3     |
| Check 1 (setup; the restart cost 0)                               | 3     |
| Check 2 (root 2, PM 1, workers 2, 1 needless heartbeat to the PM) | 6     |
| Check 3                                                           | 0     |
| Check 4 (2 worker setups, 5 heartbeat turns to the PM)            | 7     |
| Check 5 (two replays, 4 each)                                     | 8     |

The decider ledger went from 84 to 116 of 120: 32 calls. Serve C spent 24 of them in 5 minutes on silence asks across 8 nodes, mostly finished delegated workers, before the fix that leaves finished T3 children alone. Serve D spent 4. The recipient question used 1.

## Hardening pass

The hardening pass made four changes on this branch. Each has tests that fail on the commit before it and pass after (measured, `node --test`), and each was re-run live on 2026-11-16 (UTC).

### 1. The local API is locked down

Binding to 127.0.0.1 was not enough. Any web page in a browser on this machine could send `POST /handoff` as a `text/plain` body, which needs no preflight, and instruct the tree. It could also read `/outbox` and `/registry`. `api.mjs` now checks every request, in this order:

1. A request with an `Origin` header gets 403. Browsers send one on cross-site requests.
2. A request without the shared secret in `x-bot-secret` gets 401. The secret is compared in constant time. A web page cannot set a custom header without a preflight, and this server never answers one.
3. A POST whose content type is not `application/json` gets 415.

`serve.mjs` creates the secret once in `state/api-secret`, which is git-ignored, and never prints or logs it. `root_tasks.py` reads it in-process.

The tests in `test/api.test.mjs` fail on the refactor-only handler (`97aa209`) and pass from `f22dce4`. Live, on serve E at 10:05:37 and again on serve F at 10:16:39, the probe got these results (measured):

- 401 with no secret, and 401 with a wrong secret;
- 403 for a no-secret `text/plain` POST with an `Origin` header, as a web page would send it;
- 403 for the secret with an `Origin` header;
- 415 for the secret with `text/plain`;
- 200 for a correct request.

The server log shows no handoff from any refused request.

### 2. A parent's own cancel is not a failure

Before the gate wakes a parent about a child run that ended interrupted or cancelled, `parentCancelled()` reads the parent's turn items for a `task_cancel` (T3 names the child's task `node:...` where its thread is `thread:...`) or a `t3_thread_interrupt` aimed at that child. If it finds one, it drops the change with a logged reason. A child run that ends badly without such a cancel wakes the parent through `rule_child_end`, with no model call.

The tests in `test/delegated.test.mjs` fail on `c405090` and pass from `f956f67`. The first live run failed. On serve F at 10:17:46, the PM had cancelled its worker, but the bot woke it anyway. T3's HTTP thread snapshot puts `toolName` and `input` on the item, while `parentCancelled()` only read the JSON text that the MCP reader shows. A test in that shape failed (`cf902ca`), and the fix passed it (`4ade55a`). On serve G at 10:19:39, the PM delegated a counting worker (`...1935657d`) and cancelled it at once. The bot logged "the run ended interrupted because its parent cancelled it", with no wake and no decider call (measured).

### 3. Delegated workers get only the fixed rules

A delegated worker (a T3 child of its parent) now gets the stall and question rules, plus `rule_child_end`. It gets no decider asks, no promise rule and no backstop, since T3 reports its completion. The one decider call left is the recipient question, when a delegated worker's question rule fires. The tests fail on `c405090` and pass from `f956f67`.

**The idle-tree call rate.** On serve G, I left a tree of 10 nodes idle for 15 min 7 s, from 10:21:39 to 10:36:46: the root, the PM and 8 delegated workers (6 completed and 2 cancelled by the PM), all quiet, at the standing-bot timings (silence ask 10 min, backstop 30 min, ceiling 4 h). Over 91 ticks, the bot made 1 decider call: the PM's first silence ask at 602 s quiet, which chose stay_quiet at p=0.91. It woke nobody. The 8 delegated workers made 0 calls (measured). That is 1 call in 15.1 minutes, or about 4 calls per hour over this window.

The window is too short to show the steady state, which follows from the timings (inferred, not measured). Each non-delegated node gets silence asks at 10, 30, 70, 150 and 310 minutes of quiet, doubling after that, so an idle PM costs about 2 calls in its first hour and fewer every hour after. The root is not watched. The backstop makes no decider call, but it costs one frontier turn in the parent per node: once 30 minutes after the bot starts, then once per 4-hour ceiling while the state is unchanged. A tree left idle for a day costs about 7 decider calls and 6 root turns per persistent PM.

### 4. Results are tied to requests by tag, checked by the sender chain

The sender id is on the messages. A message that `t3_thread_send` delivers names its sender in `senderThreadId` (measured). But two live facts keep the chain from being the tie.

- **The bot's view of T3 omits tool outputs.** In the HTTP thread snapshot the bot reads, a tool call has `outputOmitted: true`, so the id of the message it created is missing. The bot can still match a received message to its sender's `t3_thread_send` call by recipient and exact text.
- **T3 batches messages that arrive together into one run.** On serve E at 10:05:44, two handoffs sent 20 ms apart went to the PM as one run (PM run 3), and both results came back to the root as one run (root run 4), whose single reply had both tags. So a result's chain leads back to the set of requests that run served, not to one request.

The tag therefore stays the tie: each `[request <id>]` segment of a root reply is its own task result. The chain checks it, and `chain` is `confirmed` when the run leads back to that request, `not_in_chain` when it does not, and `unavailable` without a thread reader. Before this change, the batched reply's second result was lost. On serve E, `two:b` ("Canberra") never reached the outbox.

The first chain-tie test (`d00d9d2`) assumed one request per run. It was replaced by a test with the batched shape (`00ca76d`), which fails on `d0f9e5b` and passes from `922a465`. Live on serve F at 10:16:39, two handoffs in flight (`two:c`, `two:d`) were again batched into one PM run and one root run. The reply split into `o6` (`two:c`, "About 70 miles...") and `o7` (`two:d`, "Ottawa."), both `chain: confirmed`, 31 s after the handoffs (measured). The seam replay on serve G (`conv_05_seam3`) also came back `confirmed`.

### Calls and turns in the hardening pass

The hardening pass used 13 decider calls (the ledger went from 116 to 129 of 160) and 25 test-thread turns (measured from every run in the new tree): setup 3, two-in-flight on serve E 4 and on serve F 4, the parent-cancel tests 4, the seam 3, the delegated workers those requests used 6, and 1 wrong wake from the first parent-cancel run. A gate sample of 12 real verdicts with their inputs, including the known-wrong ones, is saved outside the repo for the PM to read.

## Gaps

- **The tag convention depends on the model.** The root kept the `[request <id>]` tag on every result in both passes. An untagged result never reaches the outbox, and the sender chain cannot replace the tag while T3 batches requests (see the hardening pass).
- **The gate's cost still grows with the non-delegated nodes.** Delegated workers cost nothing now, but each persistent PM still gets decider asks on its changes and its silence (see the idle measurement).
- **Workers in `approval-required` can stall on a command approval** that nobody grants. Here the PM caught it. The question rule would catch it after `questionTimeoutSec` (inferred, not run).
- **Cancellation does not reach the root.** A cancelled prototype task stays running in the tree.
- **The API secret is a file.** Anything that can read the bot's state dir can use the API. That is the same trust as the T3 Tots token next to it.
