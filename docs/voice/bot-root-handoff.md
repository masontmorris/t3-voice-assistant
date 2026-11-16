# Bot spike 2: the standing root and the voice handoff

## Expected outcomes, written before the runs

These are my predictions for each exit check. I committed them before any live run of this spike. Results follow in later sections.

1. **The root persists.** `serve.mjs setup` run twice prints "created" and then "reused" with the same root id. After the serve process is stopped and started again, `GET /registry` returns the same root and the same node list, with no new threads in T3.
2. **A handoff flows down and back.** A `POST /handoff` reaches the root, and the root's run is in the outbox as a `root_reply`. The root sends the task to the research PM with `t3_thread_send`, and the PM delegates it to one worker with `delegate_task`. The bot discovers that worker from T3 lineage and registers it under the PM. The worker's result reaches the PM, and the PM sends it to the root. The root's tagged reply is in the outbox as a `task_result` with the same request id. Guess: about 5 test-thread turns and 1 to 2 minutes end to end.
3. **A retry creates nothing new.** A second `POST /handoff` with the same request id returns `exists`, sends no message, and adds no root run.
4. **Only `tell_mason` reaches the outbox.** A worker that asks Mason for a token trips the question rule. The recipient question picks `tell_mason`, and the notice lands in the outbox with no PM wake. A worker blocked on its PM gets `wake_pm`, which wakes the PM and adds nothing to the outbox.
5. **The prototype can use this path.** Replaying recorded clip `05_topic_change` (run `run3-c`) through `RootTaskService` hands `task_2` ("how long is the drive to Boulder?") to the root and holds `task_1` ("Add milk to the shopping list."). The end-of-utterance snapshot shows `task_2` running and `task_1` held for confirmation. Later, `task_2` completes with the worker's answer as its `result_summary`, and one `task_update` is produced. The segmenter makes 0 live decider calls.

The likeliest failure I see is the tagging convention: Haiku may drop the `[request <id>]` tag from the root's result line, which would leave the result out of the outbox.
