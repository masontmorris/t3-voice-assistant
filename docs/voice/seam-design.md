# The voice seam inside the fork's T3 server

This is the milestone 1 design for the seam that lets the Python voice server list, read, launch, send to, and follow real T3 threads. It is a design, not code. Nothing here is implemented. The PM reviews it before anyone builds it.

**Bottom line.** Add one voice-owned WebSocket route, `GET /voice-seam`, inside `apps/server/src/voiceSeam/`, mounted with one import and one line in `apps/server/src/server.ts`. Behind it, a `VoiceSeam` service calls T3's own orchestration services in-process: `ThreadLaunchService`, `ThreadManagementService`, and `OrchestrationEventStore`. Python sees only voice domain types, and every result and event carries the stored-event `sequence` it came from. The seam says a thread is done only after a fresh read of T3's own settle rule, and it speaks T3's own result for that run. Models make the judgments: the decider decides "this is T3 work", and a fast model picks the thread for Twin B. The project pick for Twin A starts as a deterministic match, and a fast model takes it over only if an eval passes. The seam performs the operation, and the launched thread is the frontier agent. The voice gets its own read-scoped bearer session, which can write only through the seam, under a ceiling the seam enforces.

Claims carry a label. Measured means read in the code at `voice/main` `ce984f9eae` (upstream `980f5d4128`) or run. Inferred means derived from the code without running it. Guess means a judgment with no evidence behind it yet.

## Problem

Milestone 1 has two goals. In Twin A, Mason says "start a thread that fixes X in project Y". A real T3 thread starts on a frontier agent, the voice says it started, and its progress and result come back as spoken updates. In Twin B, Mason asks "how's the PR thread going?". The system finds that thread, reads its live state, and speaks a short, true summary, or asks when the match is ambiguous. The voice speaks only from task records and thread events.

T3 already has every operation as an in-process service (measured):

| Operation | Existing service | New in the seam |
| --- | --- | --- |
| List threads | `loadSubscriberShellSnapshot` (`apps/server/src/orchestration-v2/ShellStream.ts:413-450`) over `listActiveShellIndex` and `readShellSnapshot` (`ProjectionStore.ts:598`, `:615`), as `orchestration-v2/http.ts:83-102` already calls it | the lineage and project filter, the voice summary type, and "state as of S" |
| Read a thread's live state | `getThreadShell`, `getThreadEventSequence`, `getThreadRecords` (`ThreadManagementService.ts:414`, `:453`, `:524`; filters at `ProjectionStore.ts:349-380`) | `VoiceThreadState`, with the latest run's result and any pending question |
| Decide that a thread is done | `waitForThread` with `untilDelegatedWorkSettled` (`ThreadManagementService.ts:991-1125`) and `subagentResultForRun` (`packages/provider-core/src/server/subagentProjection.ts:254`) | the `done` event, emitted only from that read |
| Launch | `ThreadLaunchService.launch` (`ThreadLaunchService.ts:1205`) | the launch policy, the ceiling clamp, and the receipt gate |
| Send | `ThreadManagementService.sendToThread` (`ThreadManagementService.ts:868-989`) | the mode preflight and the idempotency key |
| Interrupt | `ThreadManagementService.interruptThread` | the idempotency key |
| Follow events | `OrchestrationEventStore.readAgentEvents` for the replay, then `ThreadManagementService.streamStoredEventsFrom` live, as WS `subscribeThread` does (`ws.ts:938-1123`) | the prune checks, the distiller, and the resubscribe loop |

Four constraints make the shape non-obvious:

- **Upstream churn.** Upstream took 5,992 commits in the 30 days to `980f5d4128`, about 200 a day (measured). Each upstream file the seam touches costs merge work on every sync. `apps/server/src/server.ts` took 64 of those commits, `ws.ts` 126, and `packages/contracts/src/rpc.ts` 46 (measured).
- **T3's wire shapes churn too.** If Python parsed projection rows or Effect RPC frames, upstream drift would break the voice silently. Inside the server, the same drift fails the typecheck at merge time (inferred).
- **No ceiling comes for free.** A bearer session carries scopes only. A runtime-mode ceiling is read back only for MCP clients (`apps/server/src/auth/EnvironmentAuth.ts:1531-1554`, measured). The seam has to enforce its own maximum runtime mode and project list.
- **The voice already lied once.** When LFM3 made its own tool calls, replies claimed work was done when it was not (`docs/voice/eou-handoff.md`, measured). Only an operation's own result, a stored event, or a fresh read of current state may say what happened.

## Usage from the voice server

`voice/t3/seam.py` is the whole client. It returns voice domain types and never a T3 wire type.

```python
seam = T3Seam.connect(origin_from_runtime_json(), token_file=VOICE_SEAM_TOKEN_FILE)
seam.hello                                    # ceiling and allowed projects, sent by the server
threads = seam.list_threads()                 # ThreadList, state as of threads.as_of_sequence
state = seam.read_thread(thread_id)           # ThreadState, state as of state.as_of_sequence
launched = seam.launch(request_id=ulid(), project_id=p, instruction="Fix the flaky ASR test")
seam.watch(launched.thread_id, after_sequence=launched.sequence)
for event in seam.drain():                    # Run, Reply, NeedsYou, Resolved, Done, Gone, WatchEnded
	...
```

Every write takes a `request_id`, a ULID that Python mints once per write and journals before it calls the seam. The same `request_id` never acts twice, across reconnects, voice-server restarts, and new tokens. Every event carries the stored-event sequence it came from. A re-watch from a cursor replays what was missed, in order, and Python drops any event whose sequence is at or below its cursor.

Twin A runs inside `T3TaskService.sync`, after the gated end-of-utterance turn releases the held launch:

```python
def _launch(self, task_id, record, now):
	pick = self.picker.pick_project(record["instruction"], self.seam.hello.projects)
	if pick.kind != "one":                         # ambiguous or unknown project: ask, start nothing
		return self._ask(task_id, pick.question, now)
	link = self.journal.begin(task_id, request_id=ulid(), project_id=pick.project_id)   # saved before the call
	try:
		launched = self.seam.launch(link.request_id, pick.project_id, record["instruction"])
	except SeamRefused as refusal:                 # project_not_allowed, above_ceiling, gone, ...
		self.journal.fail(task_id, refusal.code)
		return Execution(task_id, Run.FAILED, now, None, finished_at=now,
		                 result_summary=refusal.spoken(), mode="t3")
	if launched.replayed:                          # a retry after a crash: learn the state before watching
		state = self.seam.read_thread(launched.thread_id)
		link = self.journal.started(task_id, launched.thread_id, cursor=state.as_of_sequence)
	else:
		link = self.journal.started(task_id, launched.thread_id, cursor=launched.sequence)
	self.seam.watch(link.thread_id, after_sequence=link.cursor)
	return Execution(task_id, Run.RUNNING, now, None, mode="t3", t3=link,
	                 result_summary=f"Started a thread in {pick.project_name}.")
```

A crash after `journal.begin` and before `journal.started` leaves a journal entry with a request id and no thread. On restart, Python calls `launch` again with the same request id, gets `replayed: true`, and resumes from a read. A thread the voice launched therefore never goes unreported.

Twin B is read-only, so the gate never holds it:

```python
def _ask_about(self, task_id, record, now):
	listing = self.seam.list_threads(limit=40)
	pick = self.picker.pick_thread(record["instruction"], listing.threads, self._my_threads())
	if pick.kind == "none":
		return self._done(task_id, "I don't see a thread like that.", [listing.as_of_sequence], now)
	if pick.kind == "ask":                          # two or three candidates, worded from titles
		return self._ask(task_id, pick.question, now, candidates=pick.thread_ids)
	state = self.seam.read_thread(pick.thread_id)
	return self._done(task_id, speak_state(state), [state.as_of_sequence], now)
```

## Shape

### Where it mounts

The seam touches 1 upstream file, `apps/server/src/server.ts`, with 2 lines (measured: the insertion points exist as described):

```ts
// apps/server/src/server.ts, after line 54: import { speechStreamRouteLayer } from "./speech/stream.ts";
import * as VoiceSeamRoute from "./voiceSeam/route.ts";

// apps/server/src/server.ts, a new line after line 859 (`speechStreamRouteLayer,`) inside layerMakeRoutes
	VoiceSeamRoute.layer,
```

`layerMakeRoutes` (`server.ts:837-908`) merges every route layer, and the services the seam needs are already in context there (measured: `ws.ts:1440-1457` yields the same services from ambient context). The precedent is `speechStreamRouteLayer` (`apps/server/src/speech/stream.ts:92-112`). It is a `Layer.unwrap` that authenticates the upgrade, checks a scope, and runs a plain-JSON socket. The lines around `server.ts:857-861` changed once in 30 days, when upstream added that speech route on 2026-11-05 (measured). The global readiness middleware (`server.ts:829-835`) holds the route until orchestration accepts commands (measured).

The voice-owned files:

```text
apps/server/src/voiceSeam/
	route.ts            GET /voice-seam: authenticate, upgrade, race the socket against revocation. Decode, call one method, map errors.
	VoiceSeam.ts        the service: hello, listThreads, readThread, launch, send, interrupt, watch
	watch.ts            the prune checks, the replay, the live follow, and the resubscribe loop
	events.ts           pure distiller from a stored event to a signal
	outcome.ts          the settle read and the run result, shared by watch and readThread
	ceiling.ts          VoiceSeamConfig schema, read from <T3 home>/voice-seam.json, and pure clamp helpers
	protocol.ts         Effect Schemas for the JSON frames. This is the only wire definition.
	fixtures/           stored events recorded from Replay (dev) turns
	*.test.ts           one test file beside each module above
```

The path `/voice-seam` sits outside `/api`, so no upstream route can collide with it. Upstream already owns `/api/voice/**` for speech (`packages/contracts/src/environmentHttp.ts:843-919`, measured). The protocol schemas stay in `voiceSeam/protocol.ts`, not in `packages/contracts`. A contracts file would add a second upstream file, and nothing else in T3 reads these schemas. Only `server.ts` imports `route.ts`, and nothing outside `voiceSeam/` imports the rest. The implementation PR adds the `server.ts` entry, with its reason, to `docs/voice/seam.md`, which the milestone 0 touched-files check (`voice/ci/check_seam.py`) reads (measured: the check is on `voice/main`).

### Data shapes

The server-side types, derived from the usage above:

```ts
// apps/server/src/voiceSeam/VoiceSeam.ts
/** A caller the route authenticated. Only sessions minted with subject "voice" get one. */
export interface VoiceCaller {
	readonly sessionId: AuthSessionId;
	/** The idempotency namespace is the subject, not the session, so a retry with a new token still replays. */
	readonly namespace: "voice";
}

/** A ULID that Python mints once per write and journals before the call. Anything else is invalid_request. */
export type VoiceRequestId = string & { readonly VoiceRequestId: unique symbol };

/** How a thread reads to someone asking about it. Derived from the shell row alone. */
export type ThreadActivity =
	| "working" // activityRunStatus is preparing, starting, or running
	| "waiting_on_you" // a pending runtime request, or a secret or link card
	| "waiting_on_background" // the turn ended, and awaited background work holds completion
	| "queued"
	| "idle" // no run yet
	| "finished" | "failed" | "interrupted" | "cancelled"; // the latest run is terminal and settled

export const VOICE_OPERATIONS = ["list", "read", "watch", "launch", "send", "interrupt"] as const;
export type VoiceOperation = (typeof VOICE_OPERATIONS)[number];

export interface VoiceProject {
	readonly projectId: ProjectId;
	readonly name: string;
	/** What Mason calls it out loud, from voice-seam.json. The project picker reads these. */
	readonly spokenNames: ReadonlyArray<string>;
}

export interface VoiceHello {
	readonly ceiling: {
		readonly maxRuntimeMode: RuntimeMode;
		readonly interactionMode: "default";
		readonly operations: ReadonlyArray<VoiceOperation>;
		readonly maxWatches: number;
	};
	readonly projects: ReadonlyArray<VoiceProject>; // allowed projects only
	readonly latestSequence: number;
}

export interface VoiceThreadSummary {
	readonly threadId: ThreadId;
	readonly projectId: ProjectId;
	readonly title: string;
	readonly activity: ThreadActivity;
	readonly lastActivityAt: string;
	readonly pinned: boolean;
}

export interface VoiceThreadList {
	/** Listed lineage only, in allowed projects, newest activity first. */
	readonly threads: ReadonlyArray<VoiceThreadSummary>;
	readonly truncated: boolean;
	/** The rows are the state as of this sequence: both come from one read-only transaction. */
	readonly asOfSequence: number;
}

export interface VoiceRunResult {
	readonly text: string; // T3's own result for the run, cut to 1,200 characters at a sentence end
	readonly messageId: MessageId | null;
	readonly turnItemId: TurnItemId | null;
}

export interface VoiceThreadState {
	readonly threadId: ThreadId;
	readonly projectId: ProjectId;
	readonly title: string;
	readonly activity: ThreadActivity;
	/** The shell's latest run, read in the same transaction as the shell, so `run` and `activity` agree. */
	readonly run: { readonly runId: RunId; readonly status: VoiceRunStatus; readonly startedAt: string | null } | null;
	readonly pending: VoicePending | null;
	/** For a settled terminal run, that run's result. Otherwise the shell's latestVisibleMessage when it is an assistant's. */
	readonly lastReply: (VoiceRunResult & { readonly at: string }) | null;
	/** This thread's event sequence, read in the same read-only transaction as the records. */
	readonly asOfSequence: number;
}

export interface VoicePending {
	readonly requestId: string;
	/** approval and question can be relayed. in_app means only the app can answer it. */
	readonly kind: "approval" | "question" | "in_app";
	readonly prompt: string;
}

/** All ten run statuses from packages/contracts/src/orchestrationV2.ts:643-654, by name. */
export type VoiceRunStatus =
	| "preparing" | "queued" | "starting" | "running" | "waiting"
	| "completed" | "interrupted" | "failed" | "cancelled" | "rolled_back";

export interface VoiceLaunchInput { readonly requestId: VoiceRequestId; readonly projectId: ProjectId; readonly instruction: string }
export interface VoiceLaunchResult {
	readonly threadId: ThreadId;
	readonly runId: RunId | null;
	readonly runtimeMode: RuntimeMode; // what it started with, after the clamp
	readonly model: string; // display name, for "started on Opus"
	/** A fresh launch: the launch's last accepted write. A replay: the thread's current event sequence. */
	readonly sequence: number;
	readonly replayed: boolean;
}

export interface VoiceSendInput { readonly requestId: VoiceRequestId; readonly threadId: ThreadId; readonly text: string }
export interface VoiceSendResult { readonly threadId: ThreadId; readonly runId: RunId; readonly delivery: "started" | "queued"; readonly sequence: number; readonly replayed: boolean }

export interface VoiceInterruptInput { readonly requestId: VoiceRequestId; readonly threadId: ThreadId }
export type VoiceInterruptResult =
	| { readonly kind: "requested"; readonly runId: RunId; readonly sequence: number }
	| { readonly kind: "nothing_running" };

/** `sequence` is the stored event's global sequence, or for `done` the thread's sequence at the settle read. */
export type VoiceEvent =
	| { readonly type: "run"; readonly threadId: ThreadId; readonly runId: RunId; readonly status: VoiceRunStatus; readonly sequence: number }
	| { readonly type: "reply"; readonly threadId: ThreadId; readonly runId: RunId | null; readonly messageId: MessageId; readonly text: string; readonly sequence: number }
	| ({ readonly type: "needs_you"; readonly threadId: ThreadId; readonly sequence: number } & VoicePending)
	| { readonly type: "resolved"; readonly threadId: ThreadId; readonly requestId: string; readonly status: "resolved" | "expired" | "cancelled"; readonly sequence: number }
	| { readonly type: "done"; readonly threadId: ThreadId; readonly runId: RunId; readonly status: "completed" | "failed" | "interrupted" | "cancelled"; readonly result: VoiceRunResult; readonly sequence: number }
	| { readonly type: "gone"; readonly threadId: ThreadId; readonly sequence: number };

export class VoiceSeamRefusal extends Schema.TaggedError<VoiceSeamRefusal>()("VoiceSeamRefusal", {
	code: Schema.Literals([
		"operation_not_allowed", "project_not_allowed", "above_ceiling", "not_found", "gone",
		"too_many_watches", "no_default_model", "invalid_request", "unavailable",
	]),
	/** Short text the voice may speak. Never built from a cause. */
	message: Schema.String,
	cause: Schema.optional(Schema.Defect()),
}) {}
```

The seam has one error class with a `code`, because its only consumer is the wire. Python maps each `code` to a spoken line. Voice send has no mode in milestone 1. It always queues behind an active run and never steers one (see the open questions).

### The service

```ts
export class VoiceSeam extends Context.Service<
	VoiceSeam,
	{
		readonly hello: (caller: VoiceCaller) => Effect.Effect<VoiceHello, VoiceSeamRefusal>;
		readonly listThreads: (caller: VoiceCaller, input: { readonly projectId?: ProjectId; readonly limit?: number }) => Effect.Effect<VoiceThreadList, VoiceSeamRefusal>;
		readonly readThread: (caller: VoiceCaller, input: { readonly threadId: ThreadId }) => Effect.Effect<VoiceThreadState, VoiceSeamRefusal>;
		readonly launch: (caller: VoiceCaller, input: VoiceLaunchInput) => Effect.Effect<VoiceLaunchResult, VoiceSeamRefusal>;
		readonly send: (caller: VoiceCaller, input: VoiceSendInput) => Effect.Effect<VoiceSendResult, VoiceSeamRefusal>;
		readonly interrupt: (caller: VoiceCaller, input: VoiceInterruptInput) => Effect.Effect<VoiceInterruptResult, VoiceSeamRefusal>;
		/** Replays after `afterSequence`, then follows live. Ends on `gone`, on `resync`, on a refusal, or on interruption. */
		readonly watch: (caller: VoiceCaller, input: { readonly threadId: ThreadId; readonly afterSequence: number }) => Stream.Stream<VoiceEvent | WatchEnded, VoiceSeamRefusal>;
	}
>()("t3/voiceSeam/VoiceSeam") {}

const make = Effect.gen(function* () {
	const sql = yield* SqlClient.SqlClient;
	const threads = yield* ThreadManagementService.ThreadManagementService;
	const launches = yield* ThreadLaunch.ThreadLaunchService;
	const eventStore = yield* OrchestrationEventStore.OrchestrationEventStore;
	const config = yield* Ceiling.VoiceSeamConfigFile;

	const listThreads = Effect.fn("VoiceSeam.listThreads")(function* (caller, input) {
		// TODO
		// require "list"
		// loadSubscriberShellSnapshot({ sql, listShellIndex: threads.listActiveShellIndex(),
		//   readThreads: ids => threads.readShellSnapshot({ location: "active", threadIds: ids }),
		//   listProjects, latestSequence: eventStore.latestApplicationSequence,
		//   options: { omitIdleSubagents: true, omitOlderSettled: true } })   // one read-only transaction, as http.ts:88-98
		// keep rows where isListedThreadLineage(row.lineage) && !isDelegatedAppThread(row), the rule of
		//   listProjectThreads (ThreadManagementService.ts:844-866), and row.projectId is allowed
		// map with deriveActivity; sort by lastActivityAt descending; cut to min(limit, 60)
		// asOfSequence = the snapshot's sequence
		return yield* Effect.die("not implemented");
	});

	const readThread = Effect.fn("VoiceSeam.readThread")(function* (caller, input) {
		// TODO
		// require "read"; a missing, deleted, unlisted-lineage, or disallowed thread -> not_found
		// threads.ensureLegacyTranscript(threadId)   // the only write, done first, as ws.ts:927 does
		// in one read-only transaction:
		//   asOf = threads.getThreadEventSequence(threadId); shell = threads.getThreadShell(threadId)
		//   records = threads.getThreadRecords(threadId, ["runs", "messages", "turnItems", "providerTurns", "runtimeRequests"],
		//     { runIds: [shell.latestRunId], messageRoles: ["assistant"], messageRunIds: [shell.latestRunId],
		//       turnItemRunIds: [shell.latestRunId], turnItemTypes: RESULT_AND_REQUEST_ITEM_TYPES,
		//       runtimeRequestStatuses: ["pending"] })
		// pending = Pending.fromRecords(records)                      // the join described under "Events back to the voice"
		// lastReply = activity is terminal ? Outcome.runResult(records, run) : shell.latestVisibleMessage (assistant only)
		return yield* Effect.die("not implemented");
	});

	const launch = Effect.fn("VoiceSeam.launch")(function* (caller, input) {
		// TODO
		// require "launch" and an allowed project; requestId must decode as a ULID
		// commandId = sha256(["voice", "launch", requestId]); threadId = messageId = commandId
		// if (yield* threads.hasReceipt(commandId)):                  // never call launch on a receipt
		//   shell = threads.getThreadShell(threadId); missing or deletedAt set -> refusal "gone"
		//   return { threadId, runId: shell.latestRunId, sequence: threads.getThreadEventSequence(threadId), replayed: true, ... }
		// policy = resolveLaunchPolicy(projectId, ceiling)            // below
		// launches.launch({ commandId, threadId, projectId, title: firstWords(instruction), generateTitle: true,
		//   modelSelection, runtimeMode, interactionMode, workspaceStrategy,
		//   initialMessage: { messageId, text: instruction, attachments: [] },
		//   createdBy: "user", creationSource: "server" })
		return yield* Effect.die("not implemented");
	});

	const send = Effect.fn("VoiceSeam.send")(function* (caller, input) {
		// TODO
		// require "send" and an allowed project; commandId = sha256(["voice", "send", requestId])
		// preflight: shell = threads.getThreadShell(threadId); exceededDispatchModeLimit(ceiling, shell) -> above_ceiling
		//   // before sendToThread, which restores the worktree before its own mode check (ThreadManagementService.ts:909-922)
		// threads.sendToThread({ ..., mode: "queue", createdBy: "user", creationSource: "server" })
		//   .pipe(Effect.provideService(DispatchModeLimit, ceiling))   // the recheck under the command lock
		return yield* Effect.die("not implemented");
	});

	const interrupt = Effect.fn("VoiceSeam.interrupt")(function* (caller, input) {
		// TODO
		// require "interrupt" and an allowed project; no DispatchModeLimit, so the voice can stop any thread it can see
		// threads.interruptThread with commandId = sha256(["voice", "interrupt", requestId])
		// interrupt_requested -> requested; no_active_run or already_terminal -> nothing_running
		return yield* Effect.die("not implemented");
	});

	const handlers = { hello, listThreads, readThread, launch, send, interrupt, watch: Watch.make(threads, eventStore) };
	return VoiceSeam.of(handlers);
});
```

**The launch policy.** A voice launch must start with the same model, runtime mode, interaction mode, and workspace that `t3_thread_launch` would choose for that project, for a caller whose limits equal the voice ceiling. That policy is not a service method. Its modes come from `McpToolAccess.startsThreads` (`apps/server/src/mcp/McpToolAccess.ts:254-272`), which runs `resolveRuntimeMode` and `resolveInteractionMode` (`apps/server/src/mcp/delegatedTaskModes.ts:36`, `:75`) against the caller's limits. The rest lives inline in the MCP launch handler (`apps/server/src/mcp/toolkits/project/handlers.ts:200-284`, measured). The seam calls the same exported pieces: `resolveProjectSettings` (`packages/shared/src/projectSettings.ts:55`), `resolveNewThreadModelSelection` (`packages/shared/src/usageLimits.ts:81`), both mode resolvers, and `ThreadLaunchService.checkWorktreeBase` (`ThreadLaunchService.ts:370`), all measured to exist. It copies only the composition, about 40 lines (inferred), into one private `resolveLaunchPolicy`. The parity test launches the same project through the seam and through `t3_thread_launch` on `layerInProcessMcpServer` (`McpHttpServer.ts:1319`), with an MCP caller whose limits equal the voice ceiling. It asserts that both pick the same model, runtime mode, interaction mode, and workspace. Drift then fails CI on the merge that causes it.

**The launch guard.** The orchestrator's `DispatchModeLimit` recheck reads the target thread's shell (`Orchestrator.ts:15420-15429`, measured). A `thread.create` has no shell yet, so the recheck does not cover a launch. The seam's clamp in `resolveLaunchPolicy` is the guard. The managed-policy ceiling still refuses a `thread.created` event above it (`DispatchModeLimit.ts:72-90`, measured).

**The receipt gate.** A launch replay inside `ThreadLaunchService` reads the original command's events by command id (`Orchestrator.ts:15366`). When the event log has pruned them, it looks for a `run.created` event that is gone, dispatches the initial message again, and fails with "Initial message was accepted without a durable run" (`ThreadLaunchService.ts:1369-1414`, inferred from the code). The seam therefore checks `hasReceipt(commandId)` (`ThreadManagementService.ts:475`) and never calls `launch` when a receipt exists. It answers from the thread's current state instead, and reports a deleted thread as `gone`.

### Who calls the thread operations

Option 3 says the worker behind the task service becomes a frontier agent with T3's thread tools. In this design, that worker is the launched thread itself. Agents that T3 starts get T3's orchestrator tools, including `t3_thread_launch` and `t3_thread_list` (measured: the agent writing this doc runs in a T3 thread and has both, and the launch handler is at `mcp/toolkits/project/handlers.ts:113`). A request like "split these three bugs into threads" therefore runs as a visible thread that launches its own children.

Models make every judgment that needs one:

1. The decider decides that an utterance is T3 work, and the gate holds it as a side effect.
2. The picker resolves the thread for Twin B from what the seam returned. It answers with one id, a question, or none. A fast model picks only after the Twin B eval passes.
3. The picker resolves the project for Twin A with the same three answers. In milestone 1 it starts as a deterministic match on `spokenNames`. A fast model takes over only after its own eval passes (open question 2).
4. The frontier agent in the launched thread does the work.

Code performs every operation, and its result is the only record of what happened. The seam has no standing dispatcher thread. A dispatcher would add a frontier turn of several seconds before every launch, and its tool result is hidden from watchers. See "Alternatives considered".

### Events back to the voice

The watch has three parts: the event path, a pure distiller, and two reads. The distiller turns one stored event into at most one signal. It has no state, so a replay from any cursor yields the same signals in the same order. The reads turn some signals into voice events against current state.

| Stored event | Signal | Voice event |
| --- | --- | --- |
| `run.updated` | the run id and status | `run`. Python drops a repeated pair of run id and status. |
| `run.updated` with a terminal status | the same, plus a settle check | `run`, then `done` only if the settle read says so |
| `subagent.updated` or `provider-thread.updated`, while a settle check is outstanding | a settle check | `done` only if the settle read says so |
| `message.updated` with role `assistant` and `streaming: false` | the message | `reply`, once per `messageId` |
| `runtime-request.updated` with status `pending` | the request id and kind | `needs_you`, after the pending read |
| `runtime-request.updated` with status `resolved`, `expired`, or `cancelled` | the request id and status | `resolved` |
| `turn-item.updated` with a pending `secret_request` or `link_request` | the item id | `needs_you` with kind `in_app` |
| `thread.deleted` | gone | `gone`, and the watch ends |
| everything else | nothing | nothing |

The interrogate pass counted 1,078 streaming message frames among 158,596 stored events in a read-only copy of real data (measured there, not re-run here). The distiller drops them. The same count found 3,096 messages with one final frame and 9 with two, so duplicate finals are real. The seam and Python both dedupe `reply` on `messageId`.

**The settle read decides "done".** A terminal `run.updated` from a replay can belong to a run that a later run has replaced. A replayed event cannot tell that, so the distiller never decides "done". On each settle check, `outcome.ts` reads fresh state:

1. It calls `threads.waitForThread({ projectId, threadId, timeoutMs: 1, untilDelegatedWorkSettled: true })`. With no `runId`, `waitForThread` selects the thread's latest run, follows restart continuations, and waits for children, background tasks, and completion deliveries through `delegatedTaskResultPending` (`ThreadManagementService.ts:991-1125`, measured). A 1 ms timeout makes it a check, not a wait (inferred).
2. If it timed out, nothing is spoken. The next settle check repeats the read.
3. If it settled, the seam reads the settled run's records, narrowed by run id, and computes the result with `subagentResultForRun`. That function orders by turn item, because providers can finalize two messages with the same timestamp. It also returns an error turn item for a failed run and honors stop notices (`subagentProjection.ts:254-330`, measured). The seam passes no `runs`, so the function never falls back to an earlier run's reply (inferred from `delegatedTaskReportRuns`, `subagentProjection.ts:224-249`).
4. The `done` event carries that result and the thread's event sequence read before the records.

**The pending read builds `needs_you`.** A `runtime-request.updated` payload has an id, a kind, and a status, and no text (`packages/contracts/src/orchestrationV2.ts:1283-1307`, measured). The text lives on the turn item with the same request id: `approval_request` has `prompt` and `reason` (`:1811-1821`), and `user_input_request` has `questions[].question` (`:1766-1773`, `:1437-1440`). On a pending request, the seam reads that turn item with `getThreadRecords(threadId, ["turnItems"], { turnItemRequestIds: [id] })`. `readThread` uses the same join. The kinds map like this (the 5 provider kinds are at `packages/contracts/src/providerPolicy.ts:49-55`):

| Runtime request kind | `needs_you` kind | Prompt |
| --- | --- | --- |
| `command`, `file-read`, `file-change`, `permission`, `mcp-elicitation` | `approval` | the turn item's `prompt`, else its `reason`, else a fixed line per kind |
| `user_input` | `question` | the first question, plus "and N more" |
| `dynamic_tool_call`, `auth_refresh` | none | T3 does not count these as waiting on the user (`RestartContinuation.ts:68-72`, measured) |
| a secret or link card, which the shell reports as `user_input` with `userOnly` (`ProjectionStore.ts:1712-1723`) | `in_app` | a fixed line: "It needs something from you in the app." |

A pending request whose turn item is not yet stored gets the fixed line for its kind. The recorded fixture shows which arrives first.

**The event path.** `watch.ts` follows the order that `/ws` uses for `subscribeThread` (`ws.ts:1033-1123`, measured), on every subscribe and every resubscribe:

1. Read the thread's high-water with `eventStore.latestAgentSequence(threadId)` and the prune floor with `eventStore.prunedThroughSequence` (`apps/server/src/persistence/OrchestrationEventStore.ts:94`, `:101`).
2. If the cursor is below the high-water and below the floor, end the watch with `resync`. A cursor at or above the high-water missed nothing.
3. Read the replay separately with `readAgentEvents({ threadId, afterSequence: cursor, throughSequence: highWater, limit })`. A per-thread replay uses the `(stream_id, sequence)` index (`apps/server/src/persistence/Migrations/OrchestrationV2/ApplicationEventSequenceIndexes.ts:14-16`, measured). More than `limit` events also ends with `resync`.
4. Check the floor again. Pruning publishes its boundary before it deletes, so a pass during the read shows up here (`ws.ts:1100-1104`, measured). If the cursor is now below it, end with `resync`.
5. Emit the distilled replay, then follow `streamStoredEventsFrom({ threadId, afterSequence: highWater })` live. Every scanned event moves `lastSeen`, whether or not it yields a voice event.
6. The live stream fails past 1,000 items or 8 MiB of backlog with `LiveStreamBufferError` (`LiveStreamBudget.ts:13-19`, measured), and that error arrives wrapped in `OrchestratorDomainEventStreamError` (`Orchestrator.ts:16416-16424`, measured). The watch unwraps the cause. On `LiveStreamBufferError` it goes back to step 1 with `cursor = lastSeen`. Any other error ends the watch with `unavailable`.

The stream itself has no prune check, so steps 2 and 4 are the only guard against a silent gap (measured: `streamStoredEventsFrom` passes straight to `eventSink.stream`).

On `resync`, Python reads the thread, speaks only what the read shows, and watches again from the read's `asOfSequence`. Python owns one cursor per watched thread, stored in the task record and saved across restarts. Sequences are global and increase monotonically (measured). Dropping any event at or below the cursor rules out duplicates, and re-watching from the cursor rules out loss (inferred). The server keeps no per-client cursor.

The outbound socket queue holds 256 frames. When Python stops reading and the queue fills, the seam closes with code 1013, and Python reconnects with its cursors. Each socket may hold at most `maxWatches` watches, 16 by default.

Every spoken claim traces to evidence:

| Spoken claim | Evidence |
| --- | --- |
| "Started a thread in t3-voice." | `VoiceLaunchResult.sequence`, and the task record |
| "I've asked T3 to start it." | the journaled request id, before a launch result arrives |
| "It's waiting for your approval to run the tests." | `needs_you.sequence` |
| "It finished. It said ..." | `done.sequence`, `done.runId`, and `done.result.messageId` or `turnItemId` |
| "I've asked it to stop." | `VoiceInterruptResult.sequence`, saved in the task record |
| "The PR thread is still working. Its last reply said ..." | `VoiceThreadState.asOfSequence` |
| "I don't see a thread like that." | `VoiceThreadList.asOfSequence`, and the picker's logged decision |

Python attaches these to each `task_update` and snapshot entry as `evidence`.

### Transport

The transport is one WebSocket at `GET /voice-seam` with plain JSON text frames, as in `speech/stream.ts`. It has no Effect RPC framing, no 8-chunk Ack window, and no protocol query string, so the traps a Python port of `/ws` would hit do not apply (measured: those traps are in `terminal/OutputProtocol.ts:30-34` and `ws.ts:793-798`).

```text
client -> server  { "id": 7, "op": "list_threads" | "read_thread" | "launch" | "send" | "interrupt" | "watch" | "unwatch", "input": {...} }
server -> client  { "type": "hello", "ceiling": {...}, "projects": [...], "latestSequence": 123 }   always the first frame
server -> client  { "type": "result", "id": 7, "ok": true, "value": {...} }
server -> client  { "type": "result", "id": 7, "ok": false, "error": { "code": "above_ceiling", "message": "..." } }
server -> client  { "type": "event", "event": VoiceEvent }
server -> client  { "type": "watch_ended", "threadId": "...", "reason": "gone" | "resync" | "not_found" | "unavailable" }
```

`route.ts` keeps one handler table typed `Record<WireOp, Handler>`, and `voice-seam.json` decodes its `operations` against `VOICE_OPERATIONS`. Adding an operation therefore fails the build until it is handled. Each request runs in its own fiber, with up to 4 in flight, so a slow launch never blocks a read. Each watch is a forked fiber that writes to the bounded outbound queue, and `unwatch` interrupts it.

### Access and ceiling

The voice gets its own bearer session. It never uses the T3 Tots token. Mason mints it once, with the server stopped:

```text
t3 auth session issue --subject voice --label "Voice server (ceiling in voice-seam.json)" \
	--scope orchestration:read --ttl 30d --token-only > "$VOICE_HOME/state/t3-seam-token"
```

`issueSession` passes the subject through and marks the client `deviceType: "bot"` (`EnvironmentAuth.ts:1290-1317`, measured). The session appears in **Settings > Connections** with that label. Mason can revoke it there, with `t3 auth session revoke <id>`, or with `POST /api/auth/clients/revoke`. The speech route does not watch for revocation, so `route.ts` adds it. It races the socket against `SessionStore.awaitInvalidation`, as `ws.ts:3737-3751` does (measured for `/ws`, and a requirement for the seam that its route test checks). Mint the token with the server stopped, because CLI writes to a running server's SQLite are unverified.

The route admits only sessions with subject `voice` and the `orchestration:read` scope. A browser cookie, the T3 Tots token, and an MCP client session all fail that check (an MCP session fails earlier, at `EnvironmentAuth.ts:681-688`, measured).

The token gets `orchestration:read` and not `orchestration:operate`. Outside the seam it cannot launch threads or dispatch commands, because `/ws` requires `operate` for `launchThread` and `dispatchCommand` (`apps/server/src/auth/RpcAuthorization.ts:42`, `:60`, measured). Read scope can still trigger a provider refresh, a pull request invalidation, and VCS refreshes (`RpcAuthorization.ts:69`, `:151-152`, `:185`, `:189`, measured). None of those touch threads. Inside the seam the token can do only what `voice-seam.json` allows. The voice credential therefore cannot reach above the ceiling by going around the seam.

```jsonc
// <T3 home>/voice-seam.json. The seam is off (404) until this file exists. A new socket re-reads it.
{
	"maxRuntimeMode": "auto-accept-edits",
	"operations": ["list", "read", "watch", "launch", "send", "interrupt"],
	"projects": [
		{ "projectId": "proj_...", "spokenNames": ["voice", "the voice repo"] }
	],
	"workspace": { "proj_...": { "type": "worktree" } },
	"maxWatches": 16
}
```

The seam enforces the ceiling in four places:

1. Every operation checks `operations` and the allowed projects. A thread outside them, or one with unlisted lineage, reads as `not_found`, so the voice cannot probe for it.
2. Launch clamps the runtime mode to `maxRuntimeMode` and the interaction mode to `default`, through the mode resolvers.
3. Send preflights the thread's modes against the ceiling before it calls `sendToThread`, so a refused send never restores a worktree. It then runs under a provided `DispatchModeLimit`, which the orchestrator rechecks under the command lock (`Orchestrator.ts:15420-15429`, measured). A thread Mason raised to `full-access` after the preflight is refused, not driven.
4. Interrupt runs with no `DispatchModeLimit`. The recheck refuses every command on a thread above the limit, and only the managed ceiling exempts commands that lower a mode or start no work (`DispatchModeLimit.ts:67-71`, measured). Without this exception the voice could not stop a thread Mason raised.

The managed-policy ceiling still applies on top of all four (`Orchestrator.ts:1558-1564`, measured).

### What changes in `voice/`

```text
voice/t3/seam.py           T3Seam client: one socket on a background thread, typed calls, drain() for events
voice/t3/types.py          Hello, Project, ThreadSummary, ThreadList, ThreadState, Launched, Sent, VoiceEvent
voice/t3/seam.schema.json  written by the server's golden test; a Python test checks types.py against it
voice/t3/journal.py        T3Journal: request ids, thread links, cursors, and interrupt results, saved before each call
voice/t3/pick/             the Pick type, the prefilter, and the model picker, from the Twin B eval
voice/live/t3_tasks.py     T3TaskService(MockTaskService)
```

`T3TaskService` keeps the `MockTaskService` interface (`submit_delegate`, `sync`, `poll`, `next_due`, `snapshot`, `take_updates`, `executions`), as `voice/bot/root_tasks.py` already does for the root thread (measured). Tasks that are not T3 work keep the parent's behavior.

Request ids change shape. Today the live path builds a conversation id from the turn's clip name, `conv_{stem}` (`voice/prototype/run.py:862`, with `stem` set per turn at `voice/live/server.py:212`), and task ids count up per service (`voice/prototype/tasks.py:223`). The bot spike does the same (`voice/bot/seam_replay.py:39`). Two turns can therefore mint the same `conv_x:task_3`, and the seam would replay an old launch while the voice said "started" for different work (inferred). The seam takes only ULIDs, and Python mints one per write.

```python
@dataclass
class T3Link:
	request_id: str             # the launch's ULID, journaled before the call
	project_id: str
	thread_id: str | None = None
	cursor: int = 0             # the last applied stored-event sequence for this thread
	evidence: list[int] = field(default_factory=list)
	run_id: str | None = None
	replies: dict[str, int] = field(default_factory=dict)   # messageId -> sequence, for dedupe
	pending: dict | None = None
	stop: dict | None = None    # the first interrupt result: {request_id, kind, sequence}

class T3TaskService(MockTaskService):
	"""A record is T3 work when the decider's tool pick names t3_start, t3_ask, or t3_tell.
	t3_start and t3_tell write, so they are side effects by capability. t3_ask reads."""

	def sync(self, now): ...
		# As MockTaskService.sync, but a T3 record goes to _launch, _tell, or _ask_about.
		# cancel_requested on a running T3 task: if link.stop is set, report it again.
		#   Else journal a new ULID in link.stop, call seam.interrupt, and save the result there.
		#   A retry after success gets nothing_running; the saved first result is what the voice reports.

	def poll(self, now): ...
		# For each event from seam.drain(): skip it if event.sequence <= link.cursor, else advance and save the cursor.
		# reply: skip a messageId already in link.replies.
		# needs_you: a one-time update "It needs you: <prompt>"; in_app says to open the app.
		# done: COMPLETED with the first two sentences of done.result.text, or FAILED, INTERRUPTED,
		#   or CANCELLED, said as that status. Then unwatch.
		# run: progress only. A terminal run event never completes a task.
		# watch_ended resync: read_thread, speak only from it, re-watch from its as_of_sequence.
		# gone: FAILED, "That thread was deleted."
```

A T3 task record links to its thread through `Execution.t3`, a `T3Link`, which `T3Journal` saves so a restarted voice server re-watches each thread from its cursor. `take_updates` and `build_snapshot` gain one field per entry, `evidence: {thread_id, sequences}`. Snapshot status still comes from the `Execution` alone, so T3 adds no state that the voice model can read around the task record.

The gate and the end-of-utterance turn treat a launch as a side effect:

1. `t3_start` and `t3_tell` are held by `needs_hold` until the gated end-of-utterance turn runs them with `run_task`. The voice session never calls tools, as `docs/voice/eou-handoff.md` prescribes.
2. The project pick runs inside `sync`, before the launch. An ambiguous pick becomes a question and starts nothing.
3. The snapshot shows the task as pending until the launch result returns. `sync` waits up to 3 s (guess). After that the voice says "I've asked T3 to start it", which is true from the journal alone. The started acknowledgement arrives later as a `task_update`. A retry is safe because the request id replays.
4. The voice says "started" only from a `VoiceLaunchResult` and "done" only from a `done` event.

### How to measure the Twin B pick before trusting it

The PM's Twin B eval brief runs this eval on branch `voice/twinb-pick`, before the seam exists. It follows the evaluation rules in the lane's common worker rules. Labels are written before any picker run, known failures come first, every output is saved before scoring, and Mason reads 10 scored outputs, with their reasons, before anyone trusts the result. Mason reads results. He does not label.

**The snapshots** are four frozen thread lists in the `VoiceThreadSummary` shape, built from a read-only copy of the real `statev2.sqlite` with varied windows and project subsets. Each has 20 to 60 threads, and each applies the seam's listing rule, so subagent children and delegated threads never appear. They include the cases the slices need: shared title prefixes, several threads in one project, and two threads that finished recently. Snapshots, labels, and raw outputs stay local. Only the harness, the prefilter, its test on a synthetic fixture, and an aggregate results doc with no real titles go into the repo.

**The labels** come from an Opus labeler that sees the snapshots and the slice table and never sees a picker output. The known-failure cases are written first, by hand, from the list below. The labels are committed to the local eval directory before the first picker run, and the PM reads a sample of them first.

| Slice | Count | Gold label |
| --- | --- | --- |
| Exact or near-exact title | 30 | one id |
| Paraphrase or partial ("the flaky test one") | 40 | one id |
| By project ("the unity thread") | 15 | one id, or ask when the project has several |
| By state or recency ("the one waiting on me") | 20 | one id |
| Back-reference to a voice-launched task ("how's that going") | 15 | one id, resolved from the task record |
| Ambiguous, two or more plausible | 25 | ask, with the candidate set |
| A thread that does not exist | 15 | none |
| No subject at all ("how's it going?") with no recent voice task | 10 | ask |
| A same-title thread outside the allowed projects | 10 | none, or the allowed one |

**The metric** is a per-slice outcome matrix with six outcomes: correct, wrong thread named, asked when one was right, picked when it should have asked, said none when the thread exists, and invalid id. An invalid id cannot reach the voice, because `Pick` turns an id that is not in the list into a question. The eval asserts that this conversion still happens. Each case runs 3 times at the deployed settings and passes only if all 3 pass. The eval also tracks p95 pick latency.

**The pass bar** (guess at every number):

- Zero wrong-thread picks in the ambiguous, nonexistent, and outside-project slices.
- At most 2 wrong-thread picks across all 180 cases.
- At least 90% correct on the unambiguous slices.
- Unnecessary questions on at most 10% of cases.
- p95 latency at or under 700 ms.

**The arms.** A deterministic prefilter returns a thread only when exactly one allowed title contains every content word the utterance names. It never picks the closest title. Its unit test must resolve the exact-title cases and return nothing on every ambiguous and no-subject case. The eval runs the prefilter alone with ask-on-tie, then the prefilter plus a free fast model (an OpenRouter model whose id ends in `:free`), then the prefilter plus a local model if one already on disk answers in under about 700 ms. An Opus ceiling arm is optional. If no model arm beats the prefilter alone on correct picks at an equal wrong-pick count, ship the prefilter.

The eval must catch these known failures:

- Two threads that share a title prefix must ask.
- A project-only mention, when that project has several threads, must ask.
- "The PR thread", when no title or state mentions a PR, must say none and must not pick the newest thread.
- Recency words, when two threads just finished, must ask.
- A deleted thread, absent from the list, must say none.
- A back-reference after a reconnect must resolve through the task record, not through the model.

**What the metric cannot see.** It scores the pick on frozen lists, not the spoken summary that follows, and not live lists that change while Mason talks. It uses typed utterances, so ASR errors on thread titles are out of scope.

A separate 30-utterance set over `hello.projects` measures Twin A's project pick with the same three outcomes and the same arms. It gates open question 2.

## Synthesis decision

The arena ran two runners on the same task and grounding, each assigned one pole of the "who calls the thread operations" question so the candidates would differ in structure. Seat 1 ran on Claude Opus 5.5 at high effort. Seat 2 ran on DeepSeek V4.1 Flash Fast at max effort, through Command Code, which is the fallback for that seat because Codex is off this lane. The cross-judge was skipped, because the brief allows only the explorer and runner subagents. I scored the candidates alone against the rubric below. A three-seat interrogate pass then reviewed the result and returned "accept with changes". This revision designs in each of its eight blocking changes.

| Candidate | Who performs operations | Upstream files touched | Transport |
| --- | --- | --- | --- |
| 1. Service seam | seam code calls `ThreadLaunchService` and `ThreadManagementService` | 1 (`server.ts`, 2 lines) | voice-owned JSON WebSocket |
| 2. Agent seam | a standing frontier dispatcher thread launches, a standing fast-model "lookout" thread picks, and the seam makes every other call through in-process MCP | 1 (`server.ts`, 2 lines), and it imports MCP internals that churn 36 commits a month | voice-owned JSON WebSocket |

Scores by criterion:

1. **Upstream files.** Tied at 1. Candidate 2 also depends on `McpHttpServer.ts` exports and MCP tool result shapes. Those exports exist (`McpHttpServer.ts:1258`, `:1286`, `:1319`, measured), but the dependency is on higher-churn internals.
2. **Interface depth.** Candidate 2's `work.start` and `work.ask` hide more, because Python passes the utterance. They also hide two standing threads, a role state file, and a model inside the server. Candidate 1 hides T3's shapes and policy behind seven operations, with no standing state. Candidate 1 wins on depth per moving part.
3. **Truth and traceability.** Candidate 1 wins. Its "started" comes from the launch result. Candidate 2 attributes a launch to its dispatcher through the `senderThreadId` stamp on the launched thread's first message (`ThreadLaunchService.ts:1308-1310`, measured that the stamp is written). The approach is clever but unproven end to end, and it needs `pending`, `unconfirmed`, and `ambiguous launch` states that candidate 1 never has.
4. **Access and ceiling.** Candidate 1 wins. Its token has read scope only, so it cannot launch or dispatch outside the seam. Candidate 2's token has `operate`, which can launch at `full-access` over `/ws`. Its project allowlist can only be checked before and after, not enforced, as candidate 2 says itself.
5. **Live and resumable events.** Candidate 1 wins. Candidate 2 treats `LiveStreamBufferError` as the signal for a pruned cursor. That error is the backlog budget (`LiveStreamBudget.ts:13-19`, measured), so pruning would go undetected. Candidate 1 now checks `prunedThroughSequence` before and after the replay, as `/ws` does.
6. **Fidelity to option 3.** Candidate 2 is literal. Candidate 1 puts the frontier agent in the launched thread and the picker in Python, which honors the intent with less machinery. Candidate 2 also removes the project choice from its own dispatcher. That leaves the dispatcher writing only a title and a prompt, and T3 already generates titles. Candidate 2's lookout thread costs 2 to 6 s per question (its own estimate, a guess), which is too slow for a spoken answer.

Candidate 1 is the base. These grafts came from candidate 2:

- The deterministic prefilter before any model call, with its unit test, which became the eval's baseline arm.
- One list of operations, with a `Record` handler table and config decoded against the same literal set.
- Voice send always queues and never steers in milestone 1.
- Two more eval slices: no subject at all, and a same-title thread outside the allowed projects.

Candidate 2's deployment recommendation, to run the fork's desktop build as the daily driver, is kept as the alternative in the first open question below.

Rejected from candidate 2: the standing dispatcher and lookout threads, the sender-stamp attribution, the `operate` token, and in-process MCP for every call. The reasons are in the scores above.

Verification after revision: I re-read every load-bearing citation in this doc at `ce984f9eae`, whose server code matches `3beea49b43`. The interrogate pass pointed three citations at the wrong symbols. They now point at `loadSubscriberShellSnapshot` (`ShellStream.ts:413-450`), at the store methods (`ProjectionStore.ts:598`, `:615`), and at `checkWorktreeBase` (`ThreadLaunchService.ts:370`). The replay index is in `ApplicationEventSequenceIndexes.ts:14-16`, not in the legacy migration the first draft cited. The order of stored events within one turn, such as whether an approval's turn item precedes its runtime request, is still inferred. The first implementation unit records it.

## Tradeoffs accepted

- We accept copying about 40 lines of launch-policy composition in exchange for launches with no MCP session, no model hop, and no dispatcher. The parity test fails on drift.
- We accept a Settings row that says "read" for a session that can launch through the seam, in exchange for a token that cannot launch or dispatch anywhere else. The label and the `hello` frame name the real ceiling.
- We accept that the same token can read every project over T3's own read routes, such as `GET /api/orchestration/shell`, even though the seam shows the voice only the allowed projects. The project list limits what the voice does, not what the token can see. The voice client uses only the seam.
- We accept `creationSource: "server"` and `createdBy: "user"` on voice launches in exchange for no contracts edit. The voice's task records, not a T3 field, record which threads the voice started.
- We accept a voice-owned JSON protocol beside T3's Effect RPC, in exchange for Python never parsing T3 wire shapes. Python has exactly one way in.
- We accept an extractive result summary, the first two sentences of T3's own result for the settled run, over a model-written one. It is less fluent, and it cannot invent.
- We accept one settle read per terminal run and per later subagent or provider-thread event while a check is outstanding. That costs a few reads per run, in exchange for never saying "done" from a stale event (inferred cost).
- We accept that a request with no confident project asks Mason instead of handing it to a frontier agent to work out. That costs a turn.

## Alternatives considered

- **A standing dispatcher thread with T3's MCP thread tools** (candidate 2's pole). It adds a frontier turn of seconds and tokens before every launch. Its tool result is hidden in thread snapshots (`outputOmitted: true`, measured in `docs/voice/bot-root-handoff.md`). It is a thread caller only during a run (`apps/server/src/mcp/threadAccess.ts:162-189`, measured), and it holds the whole orchestrator toolkit rather than a voice-sized set.
- **No seam: a Python port of `voice/bot/t3.mjs` over `/ws`, with its own token.** It touches 0 upstream files. Python would parse T3 projection rows and stored events with no compile-time signal when they drift. Scopes alone cannot cap runtime mode or projects. With `operate` the token can launch at `full-access`, and without it the token cannot launch at all.
- **In-process MCP for launch only**, through `McpServer.callTool` on `layerInProcessMcpServer`, with the seam's own `requestNamespace`. It would reuse MCP's launch policy and remove the copy. It loses because the seam would decode agent-facing tool results back into domain types, and that path runs only in tests today. It stays the fallback if the parity test keeps failing.
- **An `HttpApi` group or a `WsRpcGroup` method.** These touch 2 upstream files (`environmentHttp.ts` and `server.ts`) or 3 and more (`rpc.ts`, `ws.ts`, and `RpcAuthorization.ts`, at 46, 126, and 31 commits a month). They buy typed TypeScript clients that Python cannot use.

## Facts from the T3 investigation that changed

`docs/voice/t3code-investigation.md` was written for an outside adapter at fork `main` `eeae88de5`. These facts changed or need a qualifier at `ce984f9eae`:

1. Upstream churn is about 200 commits a day (5,992 in 30 days, measured). The investigation measured about 196 a day. The charter's "about 300" is high.
2. The cited MCP claims all still hold, at shifted lines. `McpHttpServer.ts:1363-1373` still sends only `notifications/tools/list_changed`. The `delegate_task` active-parent refusal is now `OrchestratorMcpService.ts:2698-2710`, and the launch handler is `mcp/toolkits/project/handlers.ts:113-338` (measured).
3. MCP client sessions are refused on every route except `/mcp` (`EnvironmentAuth.ts:681-688`, measured). An OAuth-signed-in MCP client cannot use its token for the WebSocket streams the investigation offered as the alternative.
4. `t3 auth session issue` mints a scoped, labeled, revocable bearer session for a headless client (`apps/server/src/cli/auth.ts:246-279`, measured). The investigation listed only OAuth and host-issued MCP sessions.
5. "The same `clientRequestId` replays an accepted launch" holds only within one MCP session, because the key is hashed with the session's request namespace (`mcp/toolkits/project/handlers.ts:97-127`, measured).
6. The claim that a non-TypeScript client must reimplement Effect RPC turned out cheap in practice. Frames are plain JSON. The traps are the 8-chunk Ack window and a protocol version that `/ws` reads only from the query string (measured).
7. `t3_thread_read` marks delivered child results as a side effect, and with no cursor an outside client gets the oldest page first, although the tool description says newest first (`OrchestratorMcpService.ts:3348-3350`, `:3424-3454`, inferred).
8. Agents on the ACP Registry provider see the thread tools only after they call `tool_search` (`apps/server/src/mcp/McpToolExposure.ts:7-12`, measured).
9. Upstream now owns `/api/voice/**` for speech, and `apps/server/src/speech/stream.ts` (added 2026-11-05) is a raw-WebSocket route precedent (measured).
10. A launch replay depends on the original command's stored events, which the event log prunes after about a day (`Orchestrator.ts:15366`, `docs/internals/overview.md:61-72`). An outside client that retries an old launch key can get an error instead of the thread (inferred).
11. Unchanged: the event log keeps about a day, MCP sends no progress notifications, `delegate_task` needs an active parent run, and a top-level launch does not.

## Open questions for Mason

Each question carries a recommendation. The design proceeds on the recommendation unless Mason says otherwise.

1. **Which server runs the seam?** The seam exists only in a server built from the fork, and the installed desktop app runs upstream's nightly. Recommendation: run the fork's server on the Desktop as a separate environment with its own T3 home, and link it from Mason's desktop, web, and mobile apps in **Settings > Connections**. Fork code then never writes his daily database, and no fork-versus-nightly migration skew can reach it. The cost is that Twin B sees only the fork environment's threads in milestone 1 (inferred: linked environments' threads appear in the apps, to confirm on the installed nightly). The alternative is to install the fork's desktop build as his daily app, with auto-update off. That gives one thread list, but `voice/main`'s merge lag becomes his daily-driver risk.
2. **May a fast model pick the project for Twin A?** This departs from the charter. The charter allows a fast model only for Twin B's lookup and has a frontier agent use the thread operations for Twin A. Here the launched thread is the frontier agent, and the project is chosen before it exists. A fast model picking the project buys two things. It saves a frontier turn before every launch, and it keeps the time from "start a thread" to "started" short enough for a spoken reply (guess: under 1 s for the pick against several seconds for a frontier turn). Recommendation: yes, gated. Until the 30-utterance Twin A project-pick eval passes the same bar as Twin B, Twin A uses only the deterministic match on `spokenNames` and asks whenever it is not exact. A later step could launch requests with no clear project into a configured triage project, where a frontier thread finds the repository and launches the real thread itself.
3. **What is the default ceiling?** Recommendation: `auto-accept-edits` for one or two named projects. Under `approval-required`, a voice-started thread stops at its first command and the voice relays "it needs you". That is safe, but every demo turn would wait on Mason's app. `full-access` stays out of reach of the voice.
4. **Should voice ever steer a running thread?** Recommendation: not in milestone 1. A voice follow-up always queues behind the active run, so it never changes a turn mid-flight. Steering can come later behind an explicit phrase.
5. **Is a read-scoped token that writes only through the seam acceptable?** Recommendation: yes, because it is the only shape in which the credential cannot launch above the ceiling over `/ws`. The other option is an `operate` token, which shows "operate" in Settings and can launch at `full-access` outside the seam.

## Risks

- **Event order within a turn is inferred.** Whether an approval's turn item is stored before its runtime request decides whether `needs_you` gets the real prompt or the fixed line. The first unit records it.
- **The launch-policy copy can drift.** The parity test catches the drift, and the fallback is in-process MCP for launch.
- **Reads can write.** `getThreadRecords` first runs `ensureLegacyTranscript`, which can import a v1 transcript (`ThreadManagementService.ts:655-677`, `:1742-1746`, measured). `readThread` calls it once before its read-only transaction. If the second call inside `getThreadRecords` tries to write within that transaction, the unit test fails and `readThread` moves the records read out of the transaction, reading the sequence first.
- **The settle read uses `waitForThread` with a 1 ms timeout.** That still opens and closes event subscriptions on each check (inferred from `ThreadManagementService.ts:1076-1116`). Unit 2 counts the reads per run, and if they cost too much it calls the same evaluation without the wait.
- **The CLI mint and a running server share SQLite.** Mint with the server stopped until concurrent writes are verified.

## Next implementation steps

Each unit is one small PR into `voice/main` that ends in a check. A unit starts only after the units it depends on have merged. Units 1 to 6 live in `apps/server/src/voiceSeam/` and do not mount the route until unit 4, so `server.ts` stays untouched until then.

1. **The distiller and the event path.** Write `events.ts`, the pure map from a stored event to a signal, and `watch.ts`, with the two prune checks, the separate replay read, the live follow from the high-water, and the resubscribe loop that unwraps `LiveStreamBufferError`. Record stored events from Replay (dev) turns into `fixtures/`: a plain reply, an approval, a user-input question, an interrupted run, a failed run, and a run that starts a subagent. Proof: `events.test.ts` is a table over those fixtures, and its first rows record the event order the risks name. `watch.test.ts` covers a cursor below the floor that resyncs before the replay, a prune that lands during the replay read and resyncs after it, a wrapped buffer error that resubscribes from `lastSeen` with no duplicate and no gap, and a cursor at the high-water that replays nothing. Depends on nothing.
2. **The settle read and the run result.** Write `outcome.ts`: the `waitForThread` check and `subagentResultForRun` over records narrowed by run id. Wire it into the watch as the only source of `done`. Proof: `outcome.test.ts` over fixtures shows an old run's terminal event replayed while a newer run is active emits no `done`, a parent emits `done` only after its subagent settles, a failed run's result is its error text, and a message with two finals yields one `reply`. Depends on 1.
3. **List and read.** Write `listThreads` and `readThread`, with the one read-only transaction, the lineage and project filter, `deriveActivity` from the shell row, and the pending join. Proof: `VoiceSeam.test.ts` on a seeded projection shows a subagent child and a delegated thread are absent from the list and read as `not_found`, every runtime request kind maps as the table says, and a watch from a read's `asOfSequence` misses no event committed after the read. Depends on 1 and 2.
4. **The route, the protocol, and the mount.** Write `route.ts`, `protocol.ts`, `ceiling.ts`, and the two `server.ts` lines, and add the `server.ts` entry to `docs/voice/seam.md`. Proof: `route.test.ts` shows the seam answers 404 with no `voice-seam.json`, refuses a session without subject `voice`, and closes the socket when the session is revoked. `protocol.test.ts` fails when `voice/t3/seam.schema.json` is stale. `voice/ci/check_seam.py` passes. Depends on 3.
5. **Launch.** Write `resolveLaunchPolicy`, the clamp, and the receipt gate. Proof: the parity test against `t3_thread_launch` compares model, runtime mode, interaction mode, and workspace. A launch whose receipt exists never calls `ThreadLaunchService.launch`, even with its events pruned, and a deleted thread answers `gone`. Depends on 4.
6. **Send and interrupt.** Write the send preflight and `DispatchModeLimit`, and interrupt without it. Proof: on a thread raised above the ceiling, send refuses with `above_ceiling` and no worktree restore runs, while interrupt succeeds. A second interrupt with the same request id answers `nothing_running`. Depends on 5.
7. **The Python client.** Write `voice/t3/seam.py` and `voice/t3/types.py`. Proof: a Python test checks `types.py` against the golden schema, and another drives the client against a fake server that replays frames recorded from unit 4's route test. Depends on 4.
8. **The task service and the journal.** Write `voice/t3/journal.py` and `voice/live/t3_tasks.py`, with ULID request ids, the journal-before-call order, cursors, reply dedupe, and evidence on every update. Proof: unit tests show a crash between `launch` and `journal.started` replays on restart and resumes from a read, two turns never share a request id, "done" comes only from a `done` event, and the saved interrupt result survives a retry. Depends on 7.
9. **The pickers.** Wire in `voice/t3/pick/` from the Twin B eval PR, and run the 30-utterance Twin A project-pick eval. Proof: the Twin B results doc, `docs/voice/twinb-pick-eval.md`, and a matching Twin A results table. Until Twin A passes, `pick_project` uses only the `spokenNames` match. Depends on 8 and on the Twin B eval.
10. **One integrated pass.** Run the fork's dev server with its own T3 home and the Replay (dev) provider, and drive both twins with synthetic audio through the live voice server. Proof: a run transcript where each spoken line carries its evidence sequence and the threads appear in the web client. Mason's live call then closes the milestone. Depends on 1 to 9.
