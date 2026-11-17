# The voice seam inside the fork's T3 server

This is the milestone 1 design for the seam that lets the Python voice server list, read, launch, send to, and follow real T3 threads. It is a design, not code. Nothing here is implemented. The PM reviews it before anyone builds it.

**Bottom line.** Add one voice-owned WebSocket route, `GET /voice-seam`, inside `apps/server/src/voiceSeam/`, mounted with one import and one line in `apps/server/src/server.ts`. Behind it, a `VoiceSeam` service calls T3's own orchestration services in-process: `ThreadLaunchService`, `ThreadManagementService`, and the stored-event stream. Python sees only voice domain types, and every result and event carries the stored-event `sequence` it came from. Models make the judgments: the decider decides "this is T3 work", and a fast model picks the project or the thread. The seam performs the operation, and the launched thread is the frontier agent. The voice gets its own read-scoped bearer session, which can write only through the seam, under a ceiling the seam enforces.

Claims carry a label. Measured means read in the code at `voice/main` `3beea49b43` (upstream `980f5d4128`) or run. Inferred means derived from the code without running it. Guess means a judgment with no evidence behind it yet.

## Problem

Milestone 1 has two goals. In Twin A, Mason says "start a thread that fixes X in project Y". A real T3 thread starts on a frontier agent, the voice says it started, and its progress and result come back as spoken updates. In Twin B, Mason asks "how's the PR thread going?". The system finds that thread, reads its live state, and speaks a short, true summary, or asks when the match is ambiguous. The voice speaks only from task records and thread events.

T3 already has every operation as an in-process service (measured):

| Operation | Existing service | New in the seam |
| --- | --- | --- |
| List threads | `ThreadManagementService.listActiveShellIndex` plus `readShellSnapshot` (`apps/server/src/orchestration-v2/ShellStream.ts:413-451`) | the project filter, the voice summary type, and the `asOfSequence` stamp |
| Read a thread's live state | `getThreadShell`, `getThreadEventSequence`, `getThreadRecords` (`ThreadManagementService.ts:414`, `ProjectionStore.ts:349-402`) | `VoiceThreadState`, which holds the newest reply, any pending question, and the run status |
| Launch | `ThreadLaunchService.launch` (`ThreadLaunchService.ts:1205`) | the launch policy, the ceiling clamp, and idempotency keyed on the voice request id |
| Send | `ThreadManagementService.sendToThread` (`ThreadManagementService.ts:868-989`) | the ceiling check and the idempotency key |
| Interrupt | `ThreadManagementService.interruptThread` | the ceiling check and the idempotency key |
| Follow events | `ThreadManagementService.streamStoredEventsFrom` (`Orchestrator.ts:16416-16424`), which also backs WS `subscribeThread` | the distiller that turns stored events into voice events, and the resume logic |

Four constraints make the shape non-obvious:

- **Upstream churn.** Upstream took 5,992 commits in the 30 days to `980f5d4128`, about 200 a day (measured). Each upstream file the seam touches costs merge work on every sync. `apps/server/src/server.ts` took 64 of those commits, `ws.ts` 126, and `packages/contracts/src/rpc.ts` 46 (measured).
- **T3's wire shapes churn too.** If Python parsed projection rows or Effect RPC frames, upstream drift would break the voice silently. Inside the server, the same drift fails the typecheck at merge time (inferred).
- **No ceiling comes for free.** A bearer session carries scopes only. A runtime-mode ceiling is read back only for MCP clients (`apps/server/src/auth/EnvironmentAuth.ts:1531-1554`, measured). The seam has to enforce its own maximum runtime mode and project list.
- **The voice already lied once.** When LFM3 made its own tool calls, replies claimed work was done when it was not (`docs/voice/eou-handoff.md`, measured). Only an operation's own result, or a stored event, may say what happened.

## Usage from the voice server

`voice/t3/seam.py` is the whole client. It returns voice domain types and never a T3 wire type.

```python
seam = T3Seam.connect(origin_from_runtime_json(), token_file=VOICE_SEAM_TOKEN_FILE)
seam.hello                                    # ceiling and allowed projects, sent by the server
threads = seam.list_threads(active_only=True) # ThreadList with as_of_sequence
state = seam.read_thread(thread_id)           # ThreadState with as_of_sequence
launched = seam.launch(request_id="conv_7:task_3", project_id=p, instruction="Fix the flaky ASR test")
seam.watch(launched.thread_id, after_sequence=launched.sequence)
for event in seam.drain():                    # RunChanged, Reply, NeedsYou, Resolved, Gone
	...
```

Every write takes a `request_id`. The same `request_id` never acts twice, across reconnects, voice-server restarts, and new tokens. Every event carries the stored-event sequence it came from. A re-watch from a cursor replays what was missed, in order, and Python drops any event whose sequence is at or below its cursor.

Twin A runs inside `T3TaskService.sync`, after the gated end-of-utterance turn releases the held launch:

```python
def _launch(self, task_id, record, now):
	pick = self.picker.pick_project(record["instruction"], self.seam.hello.projects)
	if pick.kind != "one":                         # ambiguous or unknown project: ask, start nothing
		return self._ask(task_id, pick.question, now)
	try:
		launched = self.seam.launch(self.request_id(task_id), pick.project_id, record["instruction"])
	except SeamRefused as refusal:                 # project_not_allowed, above_ceiling, ...
		return Execution(task_id, Run.FAILED, now, None, finished_at=now,
		                 result_summary=refusal.spoken(), mode="t3")
	link = T3Link(thread_id=launched.thread_id, project_id=pick.project_id,
	              cursor=launched.sequence, evidence=[launched.sequence])
	self.seam.watch(link.thread_id, after_sequence=link.cursor)
	self.journal.save(task_id, link)               # survives a voice-server restart
	return Execution(task_id, Run.RUNNING, now, None, mode="t3", t3=link,
	                 result_summary=f"Started a thread in {pick.project_name}.")
```

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

`layerMakeRoutes` (`server.ts:837-908`) merges every route layer, and the services the seam needs are already in context there (measured: `ws.ts:1440-1457` yields the same services from ambient context). The precedent is `speechStreamRouteLayer` (`apps/server/src/speech/stream.ts:91-112`). It is a `Layer.unwrap` that authenticates the upgrade, checks a scope, and runs a plain-JSON socket. The lines around `server.ts:857-861` changed once in 30 days, when upstream added that speech route on 2026-11-05 (measured). The global readiness middleware (`server.ts:829-835`) holds the route until orchestration accepts commands (measured).

The voice-owned files:

```text
apps/server/src/voiceSeam/
	route.ts            GET /voice-seam: authenticate, upgrade, socket loop. Decode, call one method, map errors.
	VoiceSeam.ts        the service: hello, listThreads, readThread, launch, send, interrupt, watch
	ceiling.ts          VoiceSeamConfig schema, read from <T3 home>/voice-seam.json, and pure clamp helpers
	events.ts           pure distiller from a stored event to a voice event
	protocol.ts         Effect Schemas for the JSON frames. This is the only wire definition.
	VoiceSeam.test.ts   idempotency, ceiling, resume, and launch parity with t3_thread_launch
	events.test.ts      distiller table over stored events recorded from a Replay (dev) turn
	protocol.test.ts    golden JSON Schema export. Fails when voice/t3/seam.schema.json is stale.
```

The path `/voice-seam` sits outside `/api`, so no upstream route can collide with it. Upstream already owns `/api/voice/**` for speech (`packages/contracts/src/environmentHttp.ts:843-919`, measured). The protocol schemas stay in `voiceSeam/protocol.ts`, not in `packages/contracts`. A contracts file would add a second upstream file, and nothing else in T3 reads these schemas. Only `server.ts` imports `route.ts`, and nothing outside `voiceSeam/` imports the rest. The implementation PR adds the `server.ts` entry, with its reason, to `docs/voice/seam.md`. That file does not exist on `voice/main` yet, because milestone 0's touched-files check creates it (measured).

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

/** "<conversation_id>:<task_id>" from Python, 1 to 200 characters. */
export type VoiceRequestId = string & { readonly VoiceRequestId: unique symbol };

/** How a thread reads to someone asking about it. Derived from the shell row's status fields. */
export type ThreadActivity =
	| "working" // activityRunStatus is preparing, starting, or running
	| "waiting_on_you" // a pending runtime request, or run status "waiting"
	| "queued"
	| "idle" // no run yet
	| "finished" | "failed" | "interrupted" | "cancelled"; // the latest run is terminal

export const VOICE_OPERATIONS = ["list", "read", "watch", "launch", "send", "interrupt"] as const;
export type VoiceOperation = (typeof VOICE_OPERATIONS)[number];

export interface VoiceProject {
	readonly projectId: ProjectId;
	readonly name: string;
	/** What Mason calls it out loud, from voice-seam.json. The project picker reads these. */
	readonly spokenNames: ReadonlyArray<string>;
}

export interface VoiceHello {
	readonly ceiling: { readonly maxRuntimeMode: RuntimeMode; readonly operations: ReadonlyArray<VoiceOperation>; readonly maxWatches: number };
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
	readonly threads: ReadonlyArray<VoiceThreadSummary>; // allowed projects only, newest activity first
	readonly truncated: boolean;
	readonly asOfSequence: number; // read before the shell rows, so the rows are at least this fresh
}

export interface VoiceThreadState {
	readonly threadId: ThreadId;
	readonly projectId: ProjectId;
	readonly title: string;
	readonly activity: ThreadActivity;
	readonly run: { readonly runId: RunId; readonly status: VoiceRunStatus; readonly startedAt: string | null } | null;
	readonly pending: { readonly kind: "approval" | "question"; readonly prompt: string } | null;
	/** The newest assistant message, cut to 1,200 characters at a sentence end. `final` is streaming === false. */
	readonly lastReply: { readonly text: string; readonly final: boolean; readonly at: string } | null;
	readonly asOfSequence: number; // this thread's event sequence, read before the records
}

/** All ten run statuses from packages/contracts/src/orchestrationV2.ts:643-655, by name. */
export type VoiceRunStatus =
	| "preparing" | "queued" | "starting" | "running" | "waiting"
	| "completed" | "interrupted" | "failed" | "cancelled" | "rolled_back";

export interface VoiceLaunchInput { readonly requestId: VoiceRequestId; readonly projectId: ProjectId; readonly instruction: string }
export interface VoiceLaunchResult {
	readonly threadId: ThreadId;
	readonly runId: RunId | null;
	readonly runtimeMode: RuntimeMode; // what it started with, after the clamp
	readonly model: string; // display name, for "started on Opus"
	readonly sequence: number; // the launch's last accepted write
	readonly replayed: boolean;
}

export interface VoiceSendInput { readonly requestId: VoiceRequestId; readonly threadId: ThreadId; readonly text: string }
export interface VoiceSendResult { readonly threadId: ThreadId; readonly runId: RunId; readonly delivery: "started" | "queued"; readonly sequence: number; readonly replayed: boolean }

export interface VoiceInterruptInput { readonly requestId: VoiceRequestId; readonly threadId: ThreadId }
export type VoiceInterruptResult =
	| { readonly kind: "requested"; readonly runId: RunId; readonly sequence: number }
	| { readonly kind: "nothing_running" };

/** At most one voice event per stored event. `sequence` is that stored event's global sequence. */
export type VoiceEvent =
	| { readonly type: "run"; readonly threadId: ThreadId; readonly runId: RunId; readonly status: VoiceRunStatus; readonly sequence: number;
	    /** Terminal statuses only. True while a child, a background task, or a delivery will wake the thread again. */
	    readonly moreWorkPending?: boolean }
	| { readonly type: "reply"; readonly threadId: ThreadId; readonly runId: RunId | null; readonly text: string; readonly sequence: number }
	| { readonly type: "needs_you"; readonly threadId: ThreadId; readonly requestId: string; readonly kind: "approval" | "question"; readonly prompt: string; readonly sequence: number }
	| { readonly type: "resolved"; readonly threadId: ThreadId; readonly requestId: string; readonly sequence: number }
	| { readonly type: "gone"; readonly threadId: ThreadId; readonly sequence: number };

export class VoiceSeamRefusal extends Schema.TaggedError<VoiceSeamRefusal>()("VoiceSeamRefusal", {
	code: Schema.Literals([
		"operation_not_allowed", "project_not_allowed", "above_ceiling", "not_found",
		"history_pruned", "too_many_watches", "no_default_model", "invalid_request", "unavailable",
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
		readonly listThreads: (caller: VoiceCaller, input: { readonly projectId?: ProjectId; readonly activeOnly?: boolean; readonly limit?: number }) => Effect.Effect<VoiceThreadList, VoiceSeamRefusal>;
		readonly readThread: (caller: VoiceCaller, input: { readonly threadId: ThreadId }) => Effect.Effect<VoiceThreadState, VoiceSeamRefusal>;
		readonly launch: (caller: VoiceCaller, input: VoiceLaunchInput) => Effect.Effect<VoiceLaunchResult, VoiceSeamRefusal>;
		readonly send: (caller: VoiceCaller, input: VoiceSendInput) => Effect.Effect<VoiceSendResult, VoiceSeamRefusal>;
		readonly interrupt: (caller: VoiceCaller, input: VoiceInterruptInput) => Effect.Effect<VoiceInterruptResult, VoiceSeamRefusal>;
		/** Replays after `afterSequence`, then follows live. Ends only on `gone`, a refusal, or interruption. */
		readonly watch: (caller: VoiceCaller, input: { readonly threadId: ThreadId; readonly afterSequence: number }) => Stream.Stream<VoiceEvent, VoiceSeamRefusal>;
	}
>()("t3/voiceSeam/VoiceSeam") {}

const make = Effect.gen(function* () {
	const threads = yield* ThreadManagementService.ThreadManagementService;
	const launches = yield* ThreadLaunch.ThreadLaunchService;
	const config = yield* Ceiling.VoiceSeamConfigFile;

	const listThreads = Effect.fn("VoiceSeam.listThreads")(function* (caller, input) {
		// TODO
		// require "list"; asOf = latest stored sequence, read FIRST (waitForThread's order)
		// index = threads.listActiveShellIndex()   // uses the covering index from 980f5d4128; no payload parse
		// keep ids in allowed projects; rows = yield* (yield* threads.readShellSnapshot({ location: "active", threadIds }))
		// the nested Effect reads inside the transaction and decodes after commit (ShellStream.ts:453-457)
		// map rows with deriveActivity; sort by lastActivityAt descending; cut to min(limit, 60)
		return yield* Effect.die("not implemented");
	});

	const readThread = Effect.fn("VoiceSeam.readThread")(function* (caller, input) {
		// TODO
		// require "read"; a missing thread, a deleted one, or one outside the allowed projects -> not_found
		// asOf = threads.getThreadEventSequence(threadId)   // before the records, never after
		// records = threads.getThreadRecords(threadId, ["runs", "messages", "runtimeRequests"],
		//           { latestRunLimit: 1, messageRoles: ["assistant"] })   // filter names to confirm at ProjectionStore.ts:349-402
		return yield* Effect.die("not implemented");
	});

	const launch = Effect.fn("VoiceSeam.launch")(function* (caller, input) {
		// TODO
		// require "launch" and an allowed project
		// commandId = sha256(["voice", "launch", requestId]); threadId = messageId = commandId
		// existing thread with that id -> return it with replayed: true. A launch never runs twice.
		// policy = resolveLaunchPolicy(projectId)   // below
		// launches.launch({ commandId, threadId, projectId, title: firstWords(instruction), generateTitle: true,
		//   modelSelection, runtimeMode, interactionMode: "default", workspaceStrategy,
		//   initialMessage: { messageId, text: instruction, attachments: [] },
		//   createdBy: "user", creationSource: "server" }).pipe(withVoiceModeLimit(ceiling))
		return yield* Effect.die("not implemented");
	});

	const send = Effect.fn("VoiceSeam.send")(function* (caller, input) {
		// TODO
		// require "send" and an allowed project; commandId = sha256(["voice", "send", requestId])
		// threads.sendToThread({ ..., mode: "queue", createdBy: "user", creationSource: "server" })
		//   .pipe(withVoiceModeLimit(ceiling))   // a thread Mason raised to full-access refuses here: above_ceiling
		return yield* Effect.die("not implemented");
	});

	const interrupt = Effect.fn("VoiceSeam.interrupt")(function* (caller, input) {
		// TODO: require "interrupt"; threads.interruptThread with a derived commandId
		// interrupt_requested -> requested; no_active_run or already_terminal -> nothing_running
		return yield* Effect.die("not implemented");
	});

	const watch = (caller: VoiceCaller, input: { threadId: ThreadId; afterSequence: number }) =>
		// TODO
		// require "watch" and an allowed thread
		// afterSequence below the retained floor of the event log -> history_pruned
		// lastSeen = afterSequence
		// threads.streamStoredEventsFrom({ threadId, afterSequence: lastSeen })   // bounded: replay, then live
		//   tap(stored => lastSeen = stored.sequence)       // every scanned event moves the cursor
		//   mapEffect(Events.distill), filter(nonNull)
		//   on LiveStreamBufferError: resubscribe from lastSeen   // the replay covers the gap
		//   takeUntil(event.type === "gone")
		Stream.die("not implemented");

	const handlers = { hello, listThreads, readThread, launch, send, interrupt, watch };
	return VoiceSeam.of(handlers);
});
```

**The launch policy.** A voice launch must start with the same model, runtime mode, and workspace that `t3_thread_launch` would choose for that project. That policy is not a service method. It lives inline in the MCP launch handler (`apps/server/src/mcp/toolkits/project/handlers.ts:200-283`, measured). The seam calls the same exported pieces: `resolveProjectSettings` (`packages/shared/src/projectSettings.ts:55`), `resolveNewThreadModelSelection` (`packages/shared/src/usageLimits.ts:81`), `resolveRuntimeMode` (`apps/server/src/mcp/delegatedTaskModes.ts`), and `ThreadLaunchService.checkWorktreeBase` (`ThreadLaunchService.ts:229`), all measured to exist. It copies only the composition, about 40 lines (inferred), into one private `resolveLaunchPolicy`. `VoiceSeam.test.ts` launches the same project through the seam and through `t3_thread_launch` on `layerInProcessMcpServer` (`McpHttpServer.ts:1319-1332`), and asserts that both pick the same model, runtime mode, and workspace. Drift then fails CI on the merge that causes it.

### Who calls the thread operations

Option 3 says the worker behind the task service becomes a frontier agent with T3's thread tools. In this design, that worker is the launched thread itself. Every T3-started agent gets T3's orchestrator toolkit, including `t3_thread_launch` and `t3_thread_list` (`apps/server/src/mcp/McpSessionRegistry.ts:121-165`, measured). A request like "split these three bugs into threads" therefore runs as a visible thread that launches its own children.

Models make every judgment that needs one:

1. The decider decides that an utterance is T3 work, and the gate holds it as a side effect.
2. A fast model picks the project for Twin A, or the thread for Twin B, from what the seam returned. It answers with one id, a question, or none.
3. The frontier agent in the launched thread does the work.

Code performs every operation, and its result is the only record of what happened. The seam has no standing dispatcher thread. A dispatcher would add a frontier turn before every launch, its reply would be model text about an action, and its tool result is hidden from watchers. See "Alternatives considered" for the full case.

### Events back to the voice

`events.ts` turns one stored event into at most one voice event. It is pure, so a replay from any cursor yields the same voice events in the same order.

| Stored event | Voice event |
| --- | --- |
| `run.updated` | `run`, with the run id and status. Python drops a repeated pair of run id and status. |
| `message.updated` with role `assistant` and `streaming: false` | `reply`. Streaming frames, which are most of a thread's events, are dropped. |
| `runtime-request.updated`, pending approval or user input | `needs_you`, then `resolved` when it closes |
| `thread.deleted` | `gone` |
| everything else | nothing |

The payload field names in this table are inferred from `packages/contracts/src/orchestrationV2.ts`. `events.test.ts` must confirm each one against stored events recorded from a Replay (dev) turn before the voice speaks from them.

The event path has three properties:

- **Live, with no polling.** `streamStoredEventsFrom` replays the events after the cursor and then follows the live PubSub, with no gap and no duplicate (`EventSink.ts:994-1037`, measured). A per-thread replay uses the `(thread_id, sequence)` index (`apps/server/src/persistence/Migrations/055_OrchestrationV2.ts:35`, measured).
- **Resumable.** Python owns one cursor per watched thread, stored in the task record and saved across restarts. Sequences are global and increase monotonically (measured). Dropping any event at or below the cursor rules out duplicates, and re-watching from the cursor rules out loss (inferred). The server keeps no per-client cursor. A cursor older than the retained log, about a day (`docs/internals/overview.md:61-72`), gets `history_pruned`. Python then reads the thread and speaks only what the read shows.
- **Bounded.** The source stream fails past 1,000 items or 8 MiB of backlog (`LiveStreamBudget.ts:13-19`, measured). The seam then resubscribes from its own `lastSeen`. The outbound socket queue holds 256 frames. When Python stops reading and the queue fills, the seam closes with code 1013, and Python reconnects with its cursors. Each socket may hold at most `maxWatches` watches, 16 by default.

`moreWorkPending` is read when the event is sent, not when it was stored. On a replay it reflects the thread's current state, which is still true when spoken. Python never says "done" while it is true.

Every spoken claim traces to evidence:

| Spoken claim | Evidence |
| --- | --- |
| "Started a thread in t3-voice." | `VoiceLaunchResult.sequence`, and the task record |
| "It's waiting for your approval to run the tests." | `needs_you.sequence` |
| "It finished. It said ..." | the terminal `run.sequence` with `moreWorkPending: false`, and `reply.sequence` |
| "The PR thread is still working. Its last reply said ..." | `VoiceThreadState.asOfSequence` |
| "I don't see a thread like that." | `VoiceThreadList.asOfSequence`, and the picker's logged decision |

Python attaches these sequences to each `task_update` and snapshot entry as `evidence`.

### Transport

The transport is one WebSocket at `GET /voice-seam` with plain JSON text frames, as in `speech/stream.ts`. It has no Effect RPC framing, no 8-chunk Ack window, and no protocol query string, so the traps a Python port of `/ws` would hit do not apply (measured: those traps are in `terminal/OutputProtocol.ts:30-34` and `ws.ts:793-798`).

```text
client -> server  { "id": 7, "op": "list_threads" | "read_thread" | "launch" | "send" | "interrupt" | "watch" | "unwatch", "input": {...} }
server -> client  { "type": "hello", "ceiling": {...}, "projects": [...], "latestSequence": 123 }   always the first frame
server -> client  { "type": "result", "id": 7, "ok": true, "value": {...} }
server -> client  { "type": "result", "id": 7, "ok": false, "error": { "code": "above_ceiling", "message": "..." } }
server -> client  { "type": "event", "event": VoiceEvent }
server -> client  { "type": "watch_ended", "threadId": "...", "reason": "gone" | "history_pruned" | "not_found" }
```

`route.ts` keeps one handler table typed `Record<WireOp, Handler>`, and `voice-seam.json` decodes its `operations` against `VOICE_OPERATIONS`. Adding an operation therefore fails the build until it is handled. Each request runs in its own fiber, with up to 4 in flight, so a slow launch never blocks a read. Each watch is a forked fiber that writes to the bounded outbound queue, and `unwatch` interrupts it.

### Access and ceiling

The voice gets its own bearer session. It never uses the T3 Tots token. Mason mints it once, with the server stopped:

```text
t3 auth session issue --subject voice --label "Voice server (ceiling in voice-seam.json)" \
	--scope orchestration:read --ttl 30d --token-only > "$VOICE_HOME/state/t3-seam-token"
```

`issueSession` passes the subject through and marks the client `deviceType: "bot"` (`EnvironmentAuth.ts:1290-1317`, measured). The session appears in **Settings > Connections** with that label. Mason can revoke it there, with `t3 auth session revoke <id>`, or with `POST /api/auth/clients/revoke`. Revocation closes the seam's socket, because `route.ts` races the socket against `SessionStore.awaitInvalidation`, as `ws.ts:3737-3751` does (inferred for the seam, measured for `/ws`). Mint the token with the server stopped, because CLI writes to a running server's SQLite are unverified.

The route admits only sessions with subject `voice` and the `orchestration:read` scope. A browser cookie, the T3 Tots token, and an MCP client session all fail that check (an MCP session fails earlier, at `EnvironmentAuth.ts:681-688`, measured).

The token gets `orchestration:read` and not `orchestration:operate`. Outside the seam it can only read, because `/ws` requires `operate` for `launchThread` and `dispatchCommand` (`apps/server/src/auth/RpcAuthorization.ts:42`, `:60`, measured). Inside the seam it can do only what `voice-seam.json` allows. The voice credential therefore cannot reach above the ceiling by going around the seam.

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

The seam enforces the ceiling in three places:

1. Every operation checks `operations` and the allowed projects. A thread outside them reads as `not_found`, so the voice cannot probe for it.
2. Launch picks a runtime mode at or below `maxRuntimeMode`.
3. Launch, send, and interrupt run under a provided `DispatchModeLimit` (`orchestration-v2/DispatchModeLimit.ts:26-29`). The orchestrator rechecks it under the command lock (`Orchestrator.ts:15420-15429`, measured), so a thread Mason raised to `full-access` after the preflight is refused, not driven.

The managed-policy ceiling still applies on top of all three (`Orchestrator.ts:1558-1564`, measured).

### What changes in `voice/`

```text
voice/t3/seam.py           T3Seam client: one socket on a background thread, typed calls, drain() for events
voice/t3/types.py          Hello, Project, ThreadSummary, ThreadList, ThreadState, Launched, Sent, VoiceEvent
voice/t3/seam.schema.json  written by the server's golden test; a Python test checks types.py against it
voice/t3/picker.py         ThreadPicker: pick_project and pick_thread, each a prefilter plus a fast model
voice/live/t3_tasks.py     T3TaskService(MockTaskService)
voice/evals/thread_pick/   the labeled set and its runner
```

`T3TaskService` keeps the `MockTaskService` interface (`submit_delegate`, `sync`, `poll`, `next_due`, `snapshot`, `take_updates`, `executions`), as `voice/bot/root_tasks.py` already does for the root thread (measured). Tasks that are not T3 work keep the parent's behavior.

```python
@dataclass
class T3Link:
	thread_id: str
	project_id: str
	cursor: int                 # the last applied stored-event sequence for this thread
	evidence: list[int]         # the sequences behind what has been or will be said
	run_id: str | None = None
	last_reply: tuple[str, int] | None = None   # (text, sequence)
	pending: dict | None = None

class T3TaskService(MockTaskService):
	"""A record is T3 work when the decider's tool pick names t3_start, t3_ask, or t3_tell.
	t3_start and t3_tell write, so they are side effects by capability. t3_ask reads."""

	def request_id(self, task_id):
		return f"{self.conversation_id}:{task_id}"

	def sync(self, now): ...
		# As MockTaskService.sync, but a T3 record goes to _launch, _tell, or _ask_about.
		# cancel_requested on a running T3 task -> seam.interrupt(request_id + ":stop").

	def poll(self, now): ...
		# For each event from seam.drain(): skip it if event.sequence <= link.cursor, else advance and save the cursor.
		# reply -> link.last_reply. needs_you -> a one-time update "It needs you: <prompt>".
		# terminal run with moreWorkPending false -> COMPLETED with the first two sentences of last_reply,
		#   or FAILED or CANCELLED, said as that status. Then unwatch.
		# gone -> FAILED, "That thread was deleted."
```

A T3 task record links to its thread through `Execution.t3`, a `T3Link`, which `T3Journal` saves so a restarted voice server re-watches each thread from its cursor. `take_updates` and `build_snapshot` gain one field per entry, `evidence: {thread_id, sequences}`. Snapshot status still comes from the `Execution` alone, so T3 adds no state that the voice model can read around the task record.

The gate and the end-of-utterance turn treat a launch as a side effect:

1. `t3_start` and `t3_tell` are held by `needs_hold` until the gated end-of-utterance turn runs them with `run_task`. The voice session never calls tools, as `docs/voice/eou-handoff.md` prescribes.
2. The project pick runs inside `sync`, before the launch. An ambiguous pick becomes a question and starts nothing.
3. The snapshot shows the task as pending until the launch result returns. `sync` waits up to 3 s (guess). After that the voice says it is starting, and the acknowledgement arrives as a `task_update`. A retry is safe because the request id replays.
4. The voice says "started" only from a `VoiceLaunchResult` and "done" only from a terminal `run` event.

### How to measure the Twin B pick before trusting it

The picker sees what the seam returns, so the eval runs on real `VoiceThreadList` snapshots. It follows the evaluation rules in the lane's common worker rules. Labels are written before any run, known failures come first, every output is saved before scoring, and Mason reads 10 scored outputs before anyone trusts the result.

**The labeled set** uses four frozen `list_threads` snapshots from the fork environment, seeded from a read-only copy of real data with `vp run migrate-dev-db`. Each snapshot has 20 to 60 threads. Mason labels 180 utterances against them:

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

A separate 30-utterance set over `hello.projects` covers Twin A's project pick with the same three outcomes.

**The metric** is a per-slice outcome matrix with six outcomes: correct, wrong thread named, asked when one was right, picked when it should have asked, said none when the thread exists, and invalid id. An invalid id cannot reach the voice, because `Pick` turns an id that is not in the list into a question. The eval asserts that this conversion still happens. Each case runs 3 times at the deployed settings and passes only if all 3 pass. The eval also tracks p95 pick latency.

**The pass bar** (guess at every number):

- Zero wrong-thread picks in the ambiguous, nonexistent, and outside-project slices.
- At most 2 wrong-thread picks across all 180 cases.
- At least 90% correct on the unambiguous slices.
- Unnecessary questions on at most 10% of cases.
- p95 latency at or under 700 ms.

**The baseline.** Before the fast model is called, a deterministic prefilter returns a thread only when exactly one allowed title contains every content word the utterance names. It never picks the closest title. A unit test pins it against the labels: it must resolve the exact-title slice, and it must return nothing on every ambiguous and no-subject case. The eval runs three arms: the prefilter alone with ask-on-tie, the prefilter plus the fast model, and the prefilter plus Opus as a ceiling. If the fast model does not beat the prefilter alone on correct picks at an equal wrong-pick count, ship the prefilter. The fast model must be free: an OpenRouter model whose id ends in `:free`, or a local model.

The eval must catch these known failures:

- Two threads that share a title prefix must ask.
- A project-only mention, when that project has several threads, must ask.
- "The PR thread", when no title or state mentions a PR, must say none and must not pick the newest thread.
- Recency words, when two threads just finished, must ask.
- A deleted thread, absent from the list, must say none.
- A back-reference after a reconnect must resolve through the task record, not through the model.

## Synthesis decision

The arena ran two runners on the same task and grounding, each assigned one pole of the "who calls the thread operations" question so the candidates would differ in structure. Seat 1 ran on Claude Opus 5.5 at high effort. Seat 2 ran on DeepSeek V4.1 Flash Fast at max effort, through Command Code, which is the fallback for that seat because Codex is off this lane. The cross-judge was skipped, because the brief allows only the explorer and runner subagents. I scored the candidates alone against the rubric below.

| Candidate | Who performs operations | Upstream files touched | Transport |
| --- | --- | --- | --- |
| 1. Service seam | seam code calls `ThreadLaunchService` and `ThreadManagementService` | 1 (`server.ts`, 2 lines) | voice-owned JSON WebSocket |
| 2. Agent seam | a standing frontier dispatcher thread launches, a standing fast-model "lookout" thread picks, and the seam makes every other call through in-process MCP | 1 (`server.ts`, 2 lines), and it imports MCP internals that churn 36 commits a month | voice-owned JSON WebSocket |

Scores by criterion:

1. **Upstream files.** Tied at 1. Candidate 2 also depends on `McpHttpServer.ts` exports and MCP tool result shapes. Those exports exist (`McpHttpServer.ts:1258`, `:1286`, `:1319`, measured), but the dependency is on higher-churn internals.
2. **Interface depth.** Candidate 2's `work.start` and `work.ask` hide more, because Python passes the utterance. They also hide two standing threads, a role state file, and a model inside the server. Candidate 1 hides T3's shapes and policy behind seven operations, with no standing state. Candidate 1 wins on depth per moving part.
3. **Truth and traceability.** Candidate 1 wins. Its "started" comes from the launch result. Candidate 2 attributes a launch to its dispatcher through the `senderThreadId` stamp on the launched thread's first message (`ThreadLaunchService.ts:1308-1310`, measured that the stamp is written). The approach is clever but unproven end to end, and it needs `pending`, `unconfirmed`, and `ambiguous launch` states that candidate 1 never has.
4. **Access and ceiling.** Candidate 1 wins. Its token has read scope only, so it cannot bypass the seam. Candidate 2's token has `operate`, which can launch at `full-access` over `/ws`. Its project allowlist can only be checked before and after, not enforced, as candidate 2 says itself.
5. **Live and resumable events.** Candidate 1 wins. Candidate 2 treats `LiveStreamBufferError` as the signal for a pruned cursor. That error is the backlog budget (`LiveStreamBudget.ts:13-19`, measured), so pruning would go undetected.
6. **Fidelity to option 3.** Candidate 2 is literal. Candidate 1 puts the frontier agent in the launched thread and the fast model in Python, which honors the intent with less machinery. Candidate 2 also removes the project choice from its own dispatcher. That leaves the dispatcher writing only a title and a prompt, and T3 already generates titles. Candidate 2's lookout thread costs 2 to 6 s per question (its own estimate, a guess), which is too slow for a spoken answer.

Candidate 1 is the base. These grafts came from candidate 2:

- The deterministic prefilter before any model call, with its unit test against the labels, which became the eval's baseline arm.
- One list of operations, with a `Record` handler table and config decoded against the same literal set.
- Voice send always queues and never steers in milestone 1.
- Two more eval slices: no subject at all, and a same-title thread outside the allowed projects.

Candidate 2's deployment recommendation, to run the fork's desktop build as the daily driver, is kept as the alternative in the first open question below.

Rejected from candidate 2: the standing dispatcher and lookout threads, the sender-stamp attribution, the `operate` token, and in-process MCP for every call. The reasons are in the scores above.

Verification after synthesis: I re-read every load-bearing citation in this doc at `3beea49b43`. One risk candidate 1 raised is now settled, because per-thread replay uses an existing index (`055_OrchestrationV2.ts:35`, measured). The event payload field names are still inferred and remain the first thing to test.

## Tradeoffs accepted

- We accept copying about 40 lines of launch-policy composition in exchange for launches with no MCP session, no model hop, and no dispatcher. The parity test fails on drift.
- We accept a Settings row that says "read" for a session that can launch through the seam, in exchange for a token that cannot exceed the ceiling anywhere else. The label and the `hello` frame name the real ceiling.
- We accept that the same token can read every project over T3's own read routes, such as `GET /api/orchestration/shell`, even though the seam shows the voice only the allowed projects. The project list limits what the voice does, not what the token can see. The voice client uses only the seam.
- We accept `creationSource: "server"` and `createdBy: "user"` on voice launches in exchange for no contracts edit. The voice's task records, not a T3 field, record which threads the voice started.
- We accept a voice-owned JSON protocol beside T3's Effect RPC, in exchange for Python never parsing T3 wire shapes. Python has exactly one way in.
- We accept an extractive result summary, the first two sentences of the final reply, over a model-written one. It is less fluent, and it cannot invent.
- We accept that a request with no confident project asks Mason instead of handing it to a frontier agent to work out. That costs a turn.

## Alternatives considered

- **A standing dispatcher thread with T3's MCP thread tools** (candidate 2's pole). It adds a frontier turn of seconds and tokens before every launch. Its reply is model text about an action, which repeats the false "done" failure measured in `docs/voice/eou-handoff.md`. Its tool result is hidden in thread snapshots (`outputOmitted: true`, measured in `docs/voice/bot-root-handoff.md`). It is a thread caller only during a run (`apps/server/src/mcp/threadAccess.ts:162-189`, measured), and it holds the whole orchestrator toolkit rather than a voice-sized set.
- **No seam: a Python port of `voice/bot/t3.mjs` over `/ws`, with its own token.** It touches 0 upstream files. Python would parse T3 projection rows and stored events with no compile-time signal when they drift. Scopes alone cannot cap runtime mode or projects: with `operate` the token can launch at `full-access`, and without it the token cannot launch at all.
- **In-process MCP for launch only**, through `McpServer.callTool` on `layerInProcessMcpServer`, with the seam's own `requestNamespace`. It would reuse MCP's launch policy and remove the copy. It loses because the seam would decode agent-facing tool results back into domain types, and that path runs only in tests today. It stays the fallback if the parity test keeps failing.
- **An `HttpApi` group or a `WsRpcGroup` method.** These touch 2 upstream files (`environmentHttp.ts` and `server.ts`) or 3 and more (`rpc.ts`, `ws.ts`, and `RpcAuthorization.ts`, at 46, 126, and 31 commits a month). They buy typed TypeScript clients that Python cannot use.

## Facts from the T3 investigation that changed

`docs/voice/t3code-investigation.md` was written for an outside adapter at fork `main` `eeae88de5`. These facts changed or need a qualifier at `3beea49b43`:

1. Upstream churn is about 200 commits a day (5,992 in 30 days, measured). The investigation measured about 196 a day. The charter's "about 300" is high.
2. The cited MCP claims all still hold, at shifted lines. `McpHttpServer.ts:1363-1373` still sends only `notifications/tools/list_changed`. The `delegate_task` active-parent refusal is now `OrchestratorMcpService.ts:2698-2710`, and the launch handler is `mcp/toolkits/project/handlers.ts:113-338` (measured).
3. MCP client sessions are refused on every route except `/mcp` (`EnvironmentAuth.ts:681-688`, measured). An OAuth-signed-in MCP client cannot use its token for the WebSocket streams the investigation offered as the alternative.
4. `t3 auth session issue` mints a scoped, labeled, revocable bearer session for a headless client (`apps/server/src/cli/auth.ts:246-279`, measured). The investigation listed only OAuth and host-issued MCP sessions.
5. "The same `clientRequestId` replays an accepted launch" holds only within one MCP session, because the key is hashed with `client:<sessionId>` (inferred from `mcp/toolkits/project/handlers.ts:97-128`).
6. The claim that a non-TypeScript client must reimplement Effect RPC turned out cheap in practice. Frames are plain JSON. The traps are the 8-chunk Ack window and a protocol version that `/ws` reads only from the query string (measured).
7. `t3_thread_read` marks delivered child results as a side effect, and with no cursor an outside client gets the oldest page first, although the tool description says newest first (`OrchestratorMcpService.ts:3348-3350`, `:3424-3454`, inferred).
8. Agents on the ACP Registry provider see the thread tools only after they call `tool_search` (`apps/server/src/mcp/McpToolExposure.ts:7-12`, measured).
9. Upstream now owns `/api/voice/**` for speech, and `apps/server/src/speech/stream.ts` (added 2026-11-05) is a raw-WebSocket route precedent (measured).
10. Unchanged: the event log keeps about a day, MCP sends no progress notifications, `delegate_task` needs an active parent run, and a top-level launch does not.

## Open questions for Mason

Each question carries a recommendation. The design proceeds on the recommendation unless Mason says otherwise.

1. **Which server runs the seam?** The seam exists only in a server built from the fork, and the installed desktop app runs upstream's nightly. Recommendation: run the fork's server on the Desktop as a separate environment with its own T3 home, and link it from Mason's desktop, web, and mobile apps in **Settings > Connections**. Fork code then never writes his daily database, and no fork-versus-nightly migration skew can reach it. The cost is that Twin B sees only the fork environment's threads in milestone 1 (inferred: linked environments' threads appear in the apps, to confirm on the installed nightly). The alternative is to install the fork's desktop build as his daily app, with auto-update off. That gives one thread list, but `voice/main`'s merge lag becomes his daily-driver risk.
2. **Is the launched thread an acceptable reading of option 3?** Recommendation: yes. The worker behind the task service is a frontier agent with T3's thread tools, and the voice gains no standing dispatcher. A request with no clear project asks in milestone 1. A later step could launch such requests into a configured triage project, where the frontier thread finds the repository and launches the real thread itself.
3. **What is the default ceiling?** Recommendation: `auto-accept-edits` for one or two named projects. Under `approval-required`, a voice-started thread stops at its first command and the voice relays "it needs you". That is safe, but every demo turn would wait on Mason's app. `full-access` stays out of reach of the voice.
4. **Should voice ever steer a running thread?** Recommendation: not in milestone 1. A voice follow-up always queues behind the active run, so it never changes a turn mid-flight. Steering can come later behind an explicit phrase.
5. **Is a read-scoped token that writes only through the seam acceptable?** Recommendation: yes, because it is the only shape in which the credential cannot exceed the ceiling over `/ws`. The other option is an `operate` token, which shows "operate" in Settings and can launch at `full-access` outside the seam.

## Risks

- **Event field names are inferred.** The distiller's reading of `message.updated` and `runtime-request.updated` payloads must be confirmed against recorded stored events before anything is spoken from them.
- **The launch-policy copy can drift.** The parity test catches the drift, and the fallback is in-process MCP for launch.
- **Reads can write.** `getThreadRecords` first runs `ensureProjectionTranscript`, which can import a v1 transcript (`ThreadManagementService.ts:655-677`, measured). This matches what happens when the app opens an old thread.
- **A run can end while delegated work continues.** `moreWorkPending` depends on the shell's settle fields being current at the terminal `run.updated`. Test it with a launched thread that starts a subagent.
- **The CLI mint and a running server share SQLite.** Mint with the server stopped until concurrent writes are verified.

## Next implementation step

Write `apps/server/src/voiceSeam/events.ts` and its table test against stored events recorded from a Replay (dev) turn, then `VoiceSeam.watch`. Everything both twins speak depends on that distiller and the resume cursor.
