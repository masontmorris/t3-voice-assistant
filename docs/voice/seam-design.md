# The voice seam inside the fork's T3 server

This is the milestone 1 design for the seam that lets the Python voice server list, read, launch, send to, and follow real T3 threads. It is a design, not code. Nothing here is implemented. The PM reviews it before anyone builds it.

**Bottom line.** Add one voice-owned WebSocket route, `GET /voice-seam`, inside `apps/server/src/voiceSeam/`, mounted with one import and one line in `apps/server/src/server.ts`. Behind it, a `VoiceSeam` service calls T3's own orchestration services in-process: `ThreadLaunchService`, `ThreadManagementService`, and `OrchestrationEventStore`. Python sees only voice domain types, and every result and event carries the `sequence` of the stored event or the read it came from. The seam says a thread is done only after a fresh read of T3's own settle rule, and it speaks T3's own result for that run. Models make the judgments: the decider decides "this is T3 work", and a fast model picks the thread for Twin B. The project pick for Twin A starts as a deterministic match, and a fast model takes it over only if an eval passes. The seam performs the operation, and the launched thread is the frontier agent. The voice gets its own read-scoped bearer session, which can write only through the seam, under a ceiling the seam enforces.

Claims carry a label. Measured means read in the code at `voice/main` `ce984f9eae` (upstream `980f5d4128`) or run. The server code is identical at `voice/main` `7af2f4911c` (measured: no diff under `apps/` or `packages/`). Inferred means derived from the code without running it. Guess means a judgment with no evidence behind it yet.

## Problem

Milestone 1 has two goals. In Twin A, Mason says "start a thread that fixes X in project Y". A real T3 thread starts on a frontier agent, the voice says it started, and its progress and result come back as spoken updates. In Twin B, Mason asks "how's the PR thread going?". The system finds that thread, reads its live state, and speaks a short, true summary, or asks when the match is ambiguous. The voice speaks only from task records and thread events.

T3 already has every operation as an in-process service (measured):

| Operation                    | Existing service                                                                                                                                                                                                                        | New in the seam                                                                                                          |
| ---------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------ |
| List threads                 | `loadSubscriberShellSnapshot` (`apps/server/src/orchestration-v2/ShellStream.ts:413-450`) over `listActiveShellIndex` and `readShellSnapshot` (`ProjectionStore.ts:598`, `:615`), as `orchestration-v2/http.ts:83-102` already calls it | the lineage and project filter, the voice summary type, the truncation count, and "state as of S"                        |
| Read a thread's live state   | `getThreadRecords`, `getThreadShell`, `getThreadEventSequence` (`ThreadManagementService.ts:414`, `:453`, `:524`; filters at `ProjectionStore.ts:349-380`)                                                                              | `VoiceThreadState`, with the latest run's result and any pending question                                                |
| Decide that a thread is done | `waitForThread` with `untilDelegatedWorkSettled` (`ThreadManagementService.ts:991-1125`) and `subagentResultForRun` (`packages/provider-core/src/server/subagentProjection.ts:254`)                                                     | the `done` event, emitted only from that read                                                                            |
| Launch                       | `ThreadLaunchService.launch` (`ThreadLaunchService.ts:1205`)                                                                                                                                                                            | the launch policy, the ceiling clamp, and the receipt gate over `CommandReceiptStoreV2` (`CommandReceiptStore.ts:84-87`) |
| Send                         | `ThreadManagementService.sendToThread` (`ThreadManagementService.ts:868-989`)                                                                                                                                                           | the mode preflight, the receipt gate, and the idempotency key                                                            |
| Interrupt                    | `ThreadManagementService.interruptThread`                                                                                                                                                                                               | the idempotency key                                                                                                      |
| Follow events                | `OrchestrationEventStore.readAgentEvents` for the replay, then `ThreadManagementService.streamStoredEventsFrom` live, as WS `subscribeThread` does (`ws.ts:938-1123`)                                                                   | the prune checks, the distiller, and the resubscribe loop                                                                |

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

Every write takes a `request_id`, a ULID that Python mints once per write and journals before it calls the seam. The same `request_id` never acts twice, across reconnects, voice-server restarts, and new tokens. Every event except `done` comes from one stored event and carries its sequence, and only those events move Python's cursor. A re-watch from the cursor replays what was missed, in order. `done` comes from a read, not from a stored event, so it never moves the cursor (see "The cursor rule").

Twin A runs inside `T3TaskService.sync`, after the gated end-of-utterance turn releases the held launch:

```python
def _launch(self, task_id, record, now):
	pick = self.picker.pick_project(record["instruction"], self.seam.hello.projects)
	if pick.kind != "one":                         # ambiguous or unknown project: ask, start nothing
		return self._ask(task_id, pick.question, now)
	link = self.journal.begin(task_id, request_id=ulid(), project_id=pick.project_id)   # saved before the call
	try:
		launched = self.seam.launch(link.request_id, pick.project_id, record["instruction"])
	except SeamUnavailable:                        # row launch-unavailable: retry with the same request id
		return self.follow.retry_launch(task_id, link, now)
	except SeamRefused as refusal:                 # row launch-refused: project_not_allowed, above_ceiling, refused, gone, ...
		self.journal.fail(task_id, refusal.code)
		return Execution(task_id, Run.FAILED, now, None, finished_at=now,
		                 result_summary=refusal.spoken(), mode="t3")
	cursor = None if launched.replayed else launched.sequence     # None means read first, even after a crash
	link = self.journal.started(task_id, launched.thread_id, cursor=cursor)
	self.follow.start(task_id, link)                               # rows launch-fresh and launch-replayed
	return Execution(task_id, Run.RUNNING, now, None, mode="t3", t3=link,
	                 result_summary=f"Started a thread in {pick.project_name}.")
```

`self.follow` runs the table in "The watch lifecycle". Every watch starts and ends through it, so no caller handles a refusal or a `watch_ended` on its own. A crash after `journal.begin` and before `journal.started` leaves a journal entry with a request id and no thread. On restart, Python calls `launch` again with the same request id, gets `replayed: true`, and resumes from a read (rows `restart-unstarted` and `launch-replayed`). A thread the voice launched therefore never goes unreported.

Twin B is read-only, so the gate never holds it:

```python
def _ask_about(self, task_id, record, now):
	listing = self.seam.list_threads()             # every active thread in allowed projects, up to 200
	pick = self.picker.pick_thread(record["instruction"], listing.threads, self._my_threads())
	if pick.kind == "none":
		line = (f"I don't see it among your {len(listing.threads)} most recent threads." if listing.truncated
		        else "I don't see an active thread like that.")
		return self._done(task_id, line, [listing.as_of_sequence], now)
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

`layerMakeRoutes` (`server.ts:837-908`) merges every route layer, and the services the seam needs are already in context there (measured for two of three: `ws.ts:1440-1457` yields `ThreadManagementService` and `OrchestrationEventStore` directly, and names `ThreadLaunchService` only in the type of the ambient context it captures). `CommandReceiptStoreV2` is not in that context, because `runtimeLayer.ts:75-84` provides it to the stores rather than merging it out (inferred). The seam provides its own copy, `CommandReceiptStore.layer`, which is `layerFromApplicationReceipts` over `OrchestrationCommandReceipts.layer` and needs only the shared `SqlClient`. It reads receipts and never writes them (measured: the server typecheck resolves the route with it, and the launch tests read receipts the orchestrator wrote). The precedent is `speechStreamRouteLayer` (`apps/server/src/speech/stream.ts:92-112`). It is a `Layer.unwrap` that authenticates the upgrade, checks a scope, and runs a plain-JSON socket. The lines around `server.ts:857-861` changed once in 30 days, when upstream added that speech route on 2026-11-05 (measured). The global readiness middleware (`server.ts:829-835`) holds the route until orchestration accepts commands (measured).

The voice-owned files:

```text
apps/server/src/voiceSeam/
	route.ts            GET /voice-seam: authenticate, upgrade, race the socket against revocation. Decode, call one method, map errors.
	VoiceSeam.ts        the service: hello, listThreads, readThread, launch, send, interrupt, watch
	watch.ts            the prune checks, the replay, the live follow, and the resubscribe loop
	events.ts           pure distiller from a stored event to a signal
	outcome.ts          the settle read, the pending join, and the run result, shared by watch and readThread
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
  | "working" // activityRunStatus is preparing, starting, or running, or it is waiting and nothing below asks Mason
  | "waiting_on_you" // the shell's pendingRuntimeRequest, unless its kind is dynamic_tool_call or auth_refresh (ProjectionStore.ts:1726-1811)
  | "waiting_on_background" // the turn ended, and awaited background work holds completion
  | "queued"
  | "idle" // no run yet
  | "finished"
  | "failed"
  | "interrupted"
  | "cancelled"
  | "rolled_back"; // the latest run is terminal and settled
// A run in `waiting` counts as an activity run (isActivityRunForShell, ProjectionStore.ts:1993). With no
// pendingRuntimeRequest, or with a dynamic_tool_call or auth_refresh one, nothing asks Mason, so it reads as "working".

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
  /** Every active thread with listed lineage in an allowed project, newest activity first, cut to `limit`. */
  readonly threads: ReadonlyArray<VoiceThreadSummary>;
  /** How many threads matched before the cut. */
  readonly total: number;
  /** total > threads.length. When true, "no such thread" is not a claim the voice may make. */
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
  readonly run: {
    readonly runId: RunId;
    readonly status: VoiceRunStatus;
    readonly startedAt: string | null;
  } | null;
  readonly pending: VoicePending | null;
  /** For a settled terminal run, that run's result. Otherwise the shell's latestVisibleMessage when it is an assistant's. */
  readonly lastReply: (VoiceRunResult & { readonly at: string }) | null;
  /** This thread's event sequence, read in the same read-only transaction as the records. */
  readonly asOfSequence: number;
}

export interface VoicePending {
  readonly requestId: string;
  /** approval and question can be relayed. in_app means only the app can act on it. */
  readonly kind: "approval" | "question" | "in_app";
  readonly prompt: string;
}

/** All ten run statuses from packages/contracts/src/orchestrationV2.ts:643-654, by name. */
export type VoiceRunStatus =
  | "preparing"
  | "queued"
  | "starting"
  | "running"
  | "waiting"
  | "completed"
  | "interrupted"
  | "failed"
  | "cancelled"
  | "rolled_back";

export interface VoiceLaunchInput {
  readonly requestId: VoiceRequestId;
  readonly projectId: ProjectId;
  readonly instruction: string;
}
export interface VoiceLaunchResult {
  readonly threadId: ThreadId;
  readonly runId: RunId | null;
  readonly runtimeMode: RuntimeMode; // what it started with, after the clamp
  readonly model: string; // display name, for "started on Opus"
  /** A fresh launch: the launch's last accepted write. A replay: the thread's current event sequence. */
  readonly sequence: number;
  readonly replayed: boolean;
}

export interface VoiceSendInput {
  readonly requestId: VoiceRequestId;
  readonly threadId: ThreadId;
  readonly text: string;
}
export interface VoiceSendResult {
  readonly threadId: ThreadId;
  readonly messageId: MessageId; // fixed by the request id
  readonly runId: RunId; // the run the message started or queued; a watch anchors on it
  readonly delivery: "started" | "queued";
  /** A fresh send: its accepted write. A replay: the thread's current event sequence. */
  readonly sequence: number;
  readonly replayed: boolean;
}

export interface VoiceInterruptInput {
  readonly requestId: VoiceRequestId;
  readonly threadId: ThreadId;
}
export type VoiceInterruptResult =
  | { readonly kind: "requested"; readonly runId: RunId; readonly sequence: number }
  | { readonly kind: "nothing_running" }; // no active run, or this request id already interrupted one

/** The five statuses `isTerminalRunStatus` accepts (ThreadManagementService.ts:143-146, :549-558). */
export type VoiceTerminalStatus =
  | "completed"
  | "failed"
  | "interrupted"
  | "cancelled"
  | "rolled_back";

/**
 * Cursor events: each comes from exactly one stored event, and `sequence` is that event's global sequence.
 * `done` comes from a settle read. Its `runId` is `resultRun.id`, or the anchor's when the watch names a run, and `status` and `result` are that run's own. Its `sequence` is the stored event that triggered the read, or the replay's
 * high-water for the check that follows a subscribe's replay, and
 * `asOfSequence` is the thread's sequence, read in the same transaction as the result. Neither moves Python's cursor.
 */
export type VoiceEvent =
  | {
      readonly type: "run";
      readonly threadId: ThreadId;
      readonly runId: RunId;
      readonly status: VoiceRunStatus;
      readonly sequence: number;
    }
  | {
      readonly type: "reply";
      readonly threadId: ThreadId;
      readonly runId: RunId | null;
      readonly messageId: MessageId;
      readonly text: string;
      readonly sequence: number;
    }
  | ({
      readonly type: "needs_you";
      readonly threadId: ThreadId;
      readonly sequence: number;
    } & VoicePending)
  | {
      readonly type: "resolved";
      readonly threadId: ThreadId;
      readonly requestId: string;
      readonly status: "resolved" | "expired" | "cancelled";
      readonly sequence: number;
    }
  | {
      readonly type: "done";
      readonly threadId: ThreadId;
      readonly runId: RunId;
      readonly status: VoiceTerminalStatus;
      readonly result: VoiceRunResult;
      readonly sequence: number;
      readonly asOfSequence: number;
    }
  | { readonly type: "gone"; readonly threadId: ThreadId; readonly sequence: number };

export class VoiceSeamRefusal extends Schema.TaggedError<VoiceSeamRefusal>()("VoiceSeamRefusal", {
  code: Schema.Literals([
    "operation_not_allowed",
    "project_not_allowed",
    "above_ceiling",
    "thread_busy",
    "not_found",
    "gone",
    "refused",
    "too_many_watches",
    "no_default_model",
    "invalid_request",
    "unavailable",
  ]),
  /** Short text the voice may speak. Never built from a cause. */
  message: Schema.String,
  /** For "refused" only: T3's recorded rejection, the receipt's `error`, cut to 200 characters. Absent when `error` is null (CommandReceiptStore.ts:50). */
  reason: Schema.optional(Schema.String),
  /** For "refused" only: the command whose receipt was read, so the claim traces to it. */
  commandId: Schema.optional(CommandId),
  /** For "refused" after an accepted launch: the thread that exists although its first message was refused. */
  threadId: Schema.optional(ThreadId),
  cause: Schema.optional(Schema.Defect()),
}) {}
```

The seam has one error class with a `code`, because its only consumer is the wire. Python maps each `code` to a spoken line. `refused` means T3 recorded a rejected receipt for the command, and Python speaks it as "T3 refused it", followed by `reason`. `gone` means the command was accepted and its thread has since been deleted. The two never stand in for each other. Voice send has no mode in milestone 1 and never steers. It queues behind an active run only where T3's queue holds the ceiling. Elsewhere a send to a busy thread answers `thread_busy` (see Access and ceiling and the open questions).

### The service

```ts
export class VoiceSeam extends Context.Service<
  VoiceSeam,
  {
    readonly hello: (caller: VoiceCaller) => Effect.Effect<VoiceHello, VoiceSeamRefusal>;
    readonly listThreads: (
      caller: VoiceCaller,
      input: { readonly projectId?: ProjectId; readonly limit?: number },
    ) => Effect.Effect<VoiceThreadList, VoiceSeamRefusal>;
    readonly readThread: (
      caller: VoiceCaller,
      input: { readonly threadId: ThreadId },
    ) => Effect.Effect<VoiceThreadState, VoiceSeamRefusal>;
    readonly launch: (
      caller: VoiceCaller,
      input: VoiceLaunchInput,
    ) => Effect.Effect<VoiceLaunchResult, VoiceSeamRefusal>;
    readonly send: (
      caller: VoiceCaller,
      input: VoiceSendInput,
    ) => Effect.Effect<VoiceSendResult, VoiceSeamRefusal>;
    readonly interrupt: (
      caller: VoiceCaller,
      input: VoiceInterruptInput,
    ) => Effect.Effect<VoiceInterruptResult, VoiceSeamRefusal>;
    /** Replays after `afterSequence`, then follows live. Ends with a `watch_ended` reason, on a refusal, or on interruption. */
    readonly watch: (
      caller: VoiceCaller,
      input: { readonly threadId: ThreadId; readonly afterSequence: number },
    ) => Stream.Stream<VoiceEvent | WatchEnded, VoiceSeamRefusal>;
  }
>()("t3/voiceSeam/VoiceSeam") {}

const make = Effect.gen(function* () {
  const sql = yield* SqlClient.SqlClient;
  const threads = yield* ThreadManagementService.ThreadManagementService;
  const launches = yield* ThreadLaunch.ThreadLaunchService;
  const eventStore = yield* OrchestrationEventStore.OrchestrationEventStore;
  const projects = yield* ProjectStore.ProjectStoreV2; // in the route context, as ws.ts:1451 yields it
  const config = yield* Ceiling.VoiceSeamConfigFile;

  const listThreads = Effect.fn("VoiceSeam.listThreads")(function* (caller, input) {
    // TODO
    // require "list"
    // loadSubscriberShellSnapshot({ sql, listShellIndex: threads.listActiveShellIndex(),
    //   readThreads: ids => threads.readShellSnapshot({ location: "active", threadIds: ids }),
    //   listProjects: projects.listShells(),   // ProjectStore.ts:300, as ws.ts:1183 passes it
    //   latestSequence: eventStore.latestApplicationSequence,
    //   options: { omitIdleSubagents: true, omitOlderSettled: false } })   // one read-only transaction, as http.ts:88-98
    //   // omitOlderSettled: false. The opt-in keeps only the 25 newest settled top-level threads and
    //   // reports the rest as a per-project count (ShellStream.ts:199, :330-345). The seam would drop
    //   // that count, so it reads every active shell, as a client that does not opt in does. Both opt-ins
    //   // default to false (ws.ts:1186-1187, flags declared at :1145-1146).
    // keep rows where isListedThreadLineage(row.lineage) && !isDelegatedAppThread(row), the rule of
    //   listProjectThreads (ThreadManagementService.ts:844-866), and row.projectId is allowed
    // map with deriveActivity; sort by lastActivityAt descending
    // total = the kept count; cut to min(limit ?? 200, 200); truncated = total > threads.length
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
    // pending = Outcome.pendingJoin(shell, records)               // the join described under "Events back to the voice"
    // lastReply = activity is terminal ? Outcome.runResult(records, run) : shell.latestVisibleMessage (assistant only)
    return yield* Effect.die("not implemented");
  });

  const launch = Effect.fn("VoiceSeam.launch")(function* (caller, input) {
    // TODO
    // require "launch" and an allowed project; requestId must decode as a ULID
    // commandId = voiceCommandId("launch", requestId); threadId = ThreadId.make(commandId); messageId = MessageId.make(commandId)
    //   // voiceCommandId(op, id) = `voice:${op}:` + Hex.encode(sha256(UTF-8 of JSON.stringify(["voice", op, id])))
    //   // e.g. "voice:launch:" + 64 lowercase hex characters, 77 in all. This is the MCP shape
    //   // `mcp:launch:<hex>` (mcp/toolkits/project/handlers.ts:97-105, :127-128) with a voice prefix.
    // launchReceipt = receipts.getByCommandId(commandId)          // never call launch on a receipt
    // messageReceipt = receipts.getByCommandId(`${commandId}:initial-message`)   // ThreadLaunchService.ts:1373
    // match:
    //   launchReceipt rejected  -> refusal "refused", reason = launchReceipt.error, commandId; no thread exists
    //   launchReceipt accepted, messageReceipt rejected -> refusal "refused", reason, commandId, threadId
    //   launchReceipt accepted  -> shell = threads.getThreadShell(threadId); missing or deletedAt set -> "gone"
    //     else return { threadId, runId: the initial message's run, sequence: threads.getThreadEventSequence(threadId), replayed: true, ... }
    //   no launchReceipt        -> launch, below
    // policy = resolveLaunchPolicy(projectId, ceiling)            // below
    // launches.launch({ commandId, threadId, projectId, title: firstLine(instruction),   // no generateTitle, as t3_thread_launch
    //   modelSelection, runtimeMode, interactionMode, workspaceStrategy,
    //   initialMessage: { messageId, text: instruction, attachments: [] },
    //   createdBy: "user", creationSource: "server" })
    return yield* Effect.die("not implemented");
  });

  const send = Effect.fn("VoiceSeam.send")(function* (caller, input) {
    // TODO
    // require "send"; a thread the caller cannot see -> not_found
    // commandId = voiceCommandId("send", requestId); messageId = MessageId.make(commandId)
    // receipt = receipts.getByCommandId(commandId)                 // never call sendToThread on a receipt
    //   another thread's -> invalid_request; rejected -> "refused", reason = receipt.error, commandId
    //   accepted -> answer from the message's projection, replayed: true (gone if the thread was deleted)
    // preflight: exceededDispatchModeLimit(ceiling, shell) -> above_ceiling
    //   // before sendToThread, which restores the worktree before its own mode check (ThreadManagementService.ts:908-924)
    // preflight: shell.activeRunId !== null and !queueHoldsCeiling -> thread_busy
    // threads.sendToThread({ commandId, messageId, ..., mode: "queue", createdBy: "user", creationSource: "server" })
    //   .pipe(Effect.provideService(DispatchModeLimit, ceiling))   // the recheck under the command lock
    // queued and !queueHoldsCeiling -> queued-run.cancel as `${commandId}:cancel`, then thread_busy
    return yield* Effect.die("not implemented");
  });

  const interrupt = Effect.fn("VoiceSeam.interrupt")(function* (caller, input) {
    // TODO
    // require "interrupt" and a visible thread; no DispatchModeLimit, so the voice can stop any thread it can see
    // commandId = voiceCommandId("interrupt", requestId)
    // receipt accepted -> nothing_running, without calling interruptThread; rejected -> "refused"
    // threads.interruptThread({ commandId, ... }): interrupt_requested -> requested, or nothing_running when replayed
    //   no_active_run -> record an accepted run.interrupt receipt (insertIfAbsent), then nothing_running
    return yield* Effect.die("not implemented");
  });

  const handlers = {
    hello,
    listThreads,
    readThread,
    launch,
    send,
    interrupt,
    watch: Watch.make(threads, eventStore),
  };
  return VoiceSeam.of(handlers);
});
```

**The launch policy.** A voice launch must start with the same model, runtime mode, interaction mode, and workspace that `t3_thread_launch` would choose for that project, for a caller whose limits equal the voice ceiling. That policy is not a service method. Its modes come from `McpToolAccess.startsThreads` (`apps/server/src/mcp/McpToolAccess.ts:254-272`), which runs `resolveRuntimeMode` and `resolveInteractionMode` (`apps/server/src/mcp/delegatedTaskModes.ts:36`, `:75`) against the caller's limits. The rest lives inline in the MCP launch handler (`apps/server/src/mcp/toolkits/project/handlers.ts:200-284`, measured). The seam calls the same exported pieces: `resolveProjectSettings` (`packages/shared/src/projectSettings.ts:55`), `resolveNewThreadModelSelection` (`packages/shared/src/usageLimits.ts:81`), both mode resolvers, and `ThreadLaunchService.checkWorktreeBase` (`ThreadLaunchService.ts:370`), all measured to exist. It copies only the composition, about 40 lines (inferred), into one private `resolveLaunchPolicy`. The parity test launches the same project through the seam and through the `t3_thread_launch` handler, as T3's own handler tests drive it through `McpToolAccess.HandlersLayer`, with an MCP client whose access equals the voice ceiling. It asserts that both pick the same model, runtime mode, interaction mode, and workspace. Drift then fails CI on the merge that causes it.

**The launch guard.** The orchestrator's `DispatchModeLimit` recheck reads the target thread's shell (`Orchestrator.ts:15420-15429`, measured). A `thread.create` has no shell yet, so the recheck does not cover a launch. The seam's clamp in `resolveLaunchPolicy` is the guard. The managed-policy ceiling still refuses a `thread.created` event above it (`DispatchModeLimit.ts:72-90`, measured).

**The receipt gate.** A launch replay inside `ThreadLaunchService` reads the original command's events by command id (`Orchestrator.ts:15366`). When the event log has pruned them, it looks for a `run.created` event that is gone, dispatches the initial message again, and fails with "Initial message was accepted without a durable run" (`ThreadLaunchService.ts:1369-1414`, inferred from the code). The seam therefore reads the receipt itself and never calls `launch` when one exists.

The gate reads the whole receipt, not `hasReceipt`. `hasReceipt` (`ThreadManagementService.ts:649-653`) answers true for a receipt whose `status` is `rejected` (`CommandReceiptStore.ts:40-51`, measured). The orchestrator records a rejection, with the error's message, for every refused command except a mode-limit or ceiling refusal, which it leaves unrecorded so that a retry can pass after Mason changes the mode (`Orchestrator.ts:15469-15499`, measured). A gate on `hasReceipt` would read the missing thread of a rejected launch and answer `gone`, and the voice would say "That thread was deleted" about a thread that never existed. So the seam reads `CommandReceiptStoreV2.getByCommandId` for the launch command and for its `:initial-message` command, and answers by status:

| Launch receipt | Initial-message receipt | Answer                                                                                                                                        |
| -------------- | ----------------------- | --------------------------------------------------------------------------------------------------------------------------------------------- |
| none           | (not read)              | call `launch`. A mode or ceiling refusal arrives live, and the seam maps it to `above_ceiling` (inferred). A retry runs the same check again. |
| rejected       | (not read)              | `refused`, with the recorded `error` as `reason`, or no `reason` when `error` is null. No thread exists.                                      |
| accepted       | rejected                | `refused`, with `reason` and `threadId`. The thread exists, and its first message was refused.                                                |
| accepted       | accepted or none        | read the shell. A missing or deleted shell is `gone`. Otherwise the replayed result from current state.                                       |

A launch that fails after T3 recorded its command reads both receipts again and answers by this table, so its retry and the failed call answer the same. A project with no entry in `workspace` launches in its root, as `t3_thread_launch` does without a strategy.

Send and interrupt apply the same rule to their own command ids, and neither calls `ThreadManagementService` on a receipt:

| Receipt            | Send                                                                                                                                           | Interrupt                                |
| ------------------ | ---------------------------------------------------------------------------------------------------------------------------------------------- | ---------------------------------------- |
| none               | preflight, then `sendToThread`                                                                                                                 | `interruptThread`                        |
| for another thread | `invalid_request`                                                                                                                              | `invalid_request`                        |
| rejected           | `refused`, with `reason` and `commandId`                                                                                                       | `refused`, with `reason` and `commandId` |
| accepted           | the message's run and delivery from its projection, `replayed: true`, or `gone` for a deleted thread. `thread_busy` when the seam cancelled it | `nothing_running`                        |

A send replay reads the message by its id, so it answers after the event log pruned the command's events. It also skips the worktree restore that `sendToThread` runs before its dispatch. An interrupt replay must not reach `interruptThread`. That method aims the command at whichever run is active now, and the orchestrator's receipt replay would then report a later run as interrupted (measured: with the gate removed, a retry after a second run started answered `requested` for that run). So an interrupt that finds nothing running records its own accepted receipt, and a copy that passed the gate before another copy dispatched answers `nothing_running` when its dispatch comes back replayed (measured: without either, a retry or a concurrent copy answered `requested` for a later run that nothing stopped). A rejected send receipt answers `refused`, so a retried send never reports success for a message T3 refused. A mode refusal records no receipt, so a send refused for its mode can pass on retry after Mason lowers the mode.

### Who calls the thread operations

Option 3 says the worker behind the task service becomes a frontier agent with T3's thread tools. In this design, that worker is the launched thread itself. Agents that T3 starts get T3's orchestrator tools, including `t3_thread_launch` and `t3_thread_list` (measured: the agent writing this doc runs in a T3 thread and has both, and the launch handler is at `mcp/toolkits/project/handlers.ts:113`). A request like "split these three bugs into threads" therefore runs as a visible thread that launches its own children.

Models make every judgment that needs one:

1. The decider decides that an utterance is T3 work, and the gate holds it as a side effect.
2. The picker resolves the thread for Twin B from what the seam returned. It answers with one id, a question, or none. A fast model picks only after the Twin B eval passes.
3. The picker resolves the project for Twin A with the same three answers. In milestone 1 it starts as a deterministic match on `spokenNames`. A fast model takes over only after its own eval passes (open question 2).
4. The frontier agent in the launched thread does the work.

Code performs every operation, and its result is the only record of what happened. The seam has no standing dispatcher thread. A dispatcher would add a frontier turn of several seconds before every launch, and its tool result is hidden from watchers. See "Alternatives considered".

### Events back to the voice

The watch has three parts: the event path, a pure distiller, and two reads. The distiller turns one stored event into at most one signal, and a signal may also ask for a settle check. The distiller keeps no state, so a replay from any cursor yields the same signals in the same order. The reads turn some signals into voice events against current state. The only state a watch holds is in `watch.ts`, per watch fiber: `lastSeen`, the `messageId`s it has emitted as `reply`, the settle check in flight, the flag that a trigger arrived while that check ran, the armed catch-up sequence from step 6, and the 60 s follow-up wait from step 3. That state survives the watch's own resubscribe after a `LiveStreamBufferError`. It dies with the watch, however the watch ends. The loss is safe only because every end leads to exactly one of three things: a new subscribe, whose check rebuilds what the dead watch knew (see "The subscribe check"), a terminal task state, or a spoken give-up. "The watch lifecycle" lists every start and end and the one it reaches. Python's `T3Link.replies` is the durable copy of the `reply` dedupe, and it survives reconnects and restarts.

| Stored event                                                                | Signal                                         | Voice event                                                                                            |
| --------------------------------------------------------------------------- | ---------------------------------------------- | ------------------------------------------------------------------------------------------------------ |
| `run.updated`                                                               | the run id and status, plus a settle check     | `run`, then `done` only if the settle read says so. Python drops a repeated pair of run id and status. |
| `subagent.updated` or `provider-thread.updated`                             | a settle check                                 | `done` only if the settle read says so                                                                 |
| `message.updated` with role `assistant` and `streaming: false`              | the message                                    | `reply`, unless this watch already emitted that `messageId`                                            |
| `runtime-request.updated` with status `pending`                             | the request id and kind                        | `needs_you`, only if the pending read finds the request still pending                                  |
| `runtime-request.updated` with status `resolved`, `expired`, or `cancelled` | the request id and status, plus a settle check | `resolved`, then `done` only if the settle read says so                                                |
| `turn-item.updated` with a waiting `secret_request` or `link_request`       | the item id                                    | `needs_you` with kind `in_app`, only if the pending read finds that the card still waits               |
| `turn-item.updated` with any other `secret_request` or `link_request`       | a settle check                                 | `done` only if the settle read says so                                                                 |
| `thread.deleted`                                                            | gone                                           | `gone`, and the watch ends with no settle check                                                        |
| everything else                                                             | nothing                                        | nothing                                                                                                |

The settle triggers start with the event types `waitForThread` subscribes to itself with `untilDelegatedWorkSettled` (`ThreadManagementService.ts:1077-1095`, measured): every `run.updated`, `subagent.updated`, and `provider-thread.updated`, plus `thread.deleted`, which the watch turns into `gone` instead. The seam adds the two events that end a need that the settle read holds `done` for (step 4 below): a request that leaves `pending`, and a card that stops waiting. A link card also stops asking when Mason writes in the thread again (`ProjectionStore.ts:1739-1744`, measured). That message starts or queues a run, so a `run.updated` follows (inferred). Any `run.updated` can settle the thread, not only a terminal one. A new run that replaces a cut run, and a run update that releases a parent's wait, are both non-terminal events that change the answer (inferred from `outcome` and `delegatedWorkResult` in `ThreadManagementService.ts:1022-1053`).

The interrogate pass counted 1,078 streaming message frames among 158,596 stored events in a read-only copy of real data (measured there, not re-run here). The distiller drops them. The same count found 3,096 messages with one final frame and 9 with two, so duplicate finals are real. The seam and Python both dedupe `reply` on `messageId`.

**The settle read decides "done".** A terminal `run.updated` from a replay can belong to a run that a later run has replaced. A replayed event cannot tell that, so the distiller never decides "done". On each settle check, `outcome.ts` reads fresh state:

1. It calls `threads.waitForThread({ projectId, threadId, timeoutMs: 1, untilDelegatedWorkSettled: true })`. The watch input carries only `threadId`, so `projectId` is the shell's, read once when the watch starts, in the same read that checks the allowed projects (ceiling rule 1). A missing or deleted shell at that read ends the watch at once with `watch_ended` reason `not_found`, before any replay. With no `runId`, `waitForThread` selects the thread's latest run, follows restart continuations, and waits for children, background tasks, and completion deliveries through `delegatedTaskResultPending` (`ThreadManagementService.ts:991-1125`, measured). A 1 ms timeout makes it a check, not a wait (inferred). If it fails with `ThreadManagementThreadNotFoundError` (`ThreadManagementService.ts:833-841`, measured), the thread was deleted or left its project. The watch then ends with `watch_ended` reason `gone`, not `unavailable`, and emits no `done`.
2. The result has two runs (`ThreadManagementWaitResult`, `ThreadManagementService.ts:191-197`, measured). `run` is the selected run after restart continuations. `resultRun`, present only once settled, is the run that holds the thread's final answer, and it can differ from `run` when delegated work finished in a later run. The check passes this step only when `timedOut` is false and `resultRun` is present.
3. If it did not pass, nothing is spoken. If `run` is terminal and the check still timed out, a restart continuation or delegated work is pending. A declined continuation stores no event (`ThreadManagementService.ts:1075-1076`, `:1107-1109`), so no trigger would arrive. The watch then forks one follow-up `waitForThread` with a 60 s timeout, which also wakes on `awaitRestartContinuation`. Its result counts as a settle read, and it repeats while the answer stays terminal and unsettled. A watch has at most one follow-up in flight, and `unwatch` interrupts it.
4. If it passed, the seam reads four things in one read-only transaction: the thread's event sequence S, the shell, `resultRun`'s records narrowed by its run id, and the pending runtime requests. It reads S first, so the step 6 gate holds even if the transaction is not a true snapshot. A need that commits after S and before the records are read is in the records, and the join sees it. A need that commits after the records are read makes the step 6 re-read return more than S. The seam then runs `pendingJoin` over them, the same join that `readThread` runs. `waitForThread` never looks at requests or cards (`ThreadManagementService.ts:1031-1033`, `:1064`, `Orchestrator.ts:16203-16206`, measured), so it can settle while T3 shows the thread as waiting on Mason. Two cases do that. A link card waits on the user after its run ends (`ProjectionStore.ts:1726-1737`), and the shell still reports it as `pendingRuntimeRequest` (`:1791-1811`). A pending `user_input` whose `responseCapability` is `message` does not hold its run. T3 finds such questions still pending after their run has ended, and dismisses them when a delegated task ends interrupted or cancelled (`Orchestrator.ts:14688-14693`). The shell reports one too (`ProjectionStore.ts:1773-1782`, with `blocking: false` at `:1708`). All four citations are measured. If the join finds a need, the check is not settled. Nothing is spoken, the watch stays open, and no follow-up wait is forked, because the need ends with a stored event that triggers the next check. The need's `needs_you` comes from its own stored event, which the watch emits when the stream reaches it. The read never emits a `needs_you` itself, because a read has no stored sequence to give it.
5. If no need is pending, the seam computes the result with `subagentResultForRun` over the records from step 4. That function orders by turn item, because providers can finalize two messages with the same timestamp. It also returns an error turn item for a failed run and honors stop notices (`subagentProjection.ts:254-330`, measured). The seam passes no `runs`, so the function never falls back to an earlier run's reply (inferred from `delegatedTaskReportRuns`, `subagentProjection.ts:224-249`).
6. The records from step 4 can miss a need whose stored event commits after S, even when it belongs to the run that just ended. So the seam gates the send. It reads `threads.getThreadEventSequence(threadId)` again, as the last read before it sends `done`, and calls the result S2. It sends `done` only if S2 equals S and the watch has already emitted every stored event through S, that is, `lastSeen >= S`. Otherwise it sends nothing and arms a catch-up check that runs once `lastSeen >= S2`. The test is not equality, because one batch can carry `lastSeen` past S2. The catch-up is an ordinary settle check, so it runs every step again on fresh state. By then the watch has emitted every event through S2, including the need's own `needs_you`. While the need is pending, the catch-up's join sees it and does not settle.
7. With no anchor (the paragraph below), the `done` event has `runId = resultRun.id`, `status = resultRun.status`, which is one of the five `VoiceTerminalStatus` values, and that run's result. Its `asOfSequence` is S, so it is exact for the result and the pending join, and every stored event at or below it has already gone to Python. Its `sequence` is the trigger's, or the high-water for the subscribe check.

**A watch anchored on a run.** The watch input may name the run the voice started or sent to, the anchor. When `resultRun` is a different run, `done` reports the anchor's own outcome and none of `resultRun`'s: `runId` is the anchor's, `status` is read from the anchor's own stored record, which must be terminal and is never inferred, and `result` is that run's own final message or error text, or empty text when it has none. T3's stand-in line for a run with no answer is not spoken. An anchor with no stored record, or a non-terminal one, sends nothing. T3's rule still decides when the thread has settled. A rolled-back run is reported only when the watch is anchored on it, because `resultRun` names a rolled-back run only when the thread rolled back past all its work (measured in `subagentProjection.ts`, PR 48 deviation 2).

The re-run in step 6 terminates. Each catch-up needs a commit on this thread during the check before it, and it waits on the stream, not a timer, so it never spins. A thread that keeps committing costs one check per batch of new events, and the first check that finds the thread quiet either sends `done` or finds a need or an unsettled run. A catch-up collapses with any other pending trigger, under the one-check-at-a-time rule below.

One window stays open. A need whose event commits after the step 6 re-read and before Python unwatches is not in any check. That event would have to commit after its own run's terminal `run.updated`, and after the read and re-read that follow it. Inside a turn, an approval, a question, and a link card all reach the store before the provider reports the turn's end, because each one comes from a request or a tool call that the turn makes before it ends (inferred). Recovery writes a message-capability question in the same commit as the run's cancellation, and before it (`ProviderRuntimeRecoveryService.ts:666-702`, one `commitCommand` at `:1067-1072`, measured). Unit 1's fixtures record the order for each need. The voice bounds the rest. After `done`, Python keeps the watch open for 10 s (guess) before it unwatches, and applies every cursor event in that time as usual. A late `needs_you` is therefore spoken, not dropped. A need that commits more than 10 s after its run's `done` reaches the voice only through a later read. The 10 s window lives only in the running voice server. A restart inside it ends the window, and the restarted server does not re-watch a task that is already terminal (row `restart-ended`). The subscribe check below does not cover that gap. It decides only `done`, and the task already has its `done`. A need that commits in that gap therefore also reaches the voice only through a later read.

Settle checks run one at a time per watch, on the watch fiber, and triggers that arrive while one runs collapse into a single next check. The 60 s follow-up wait in step 3 runs in its own fiber, but it hands its result to the watch fiber, which applies the step 6 gate. A live trigger's check runs after that event's own voice event is emitted. With the step 6 gate, `done` never precedes a stored event at or below its `asOfSequence`.

**The subscribe check.** Every subscribe and every resubscribe runs exactly one settle check after its replay is emitted, whether or not the replay held a trigger. Its `sequence` is the high-water from step 1 of the event path, and any triggers in the replay collapse into it. `lastSeen` starts at the cursor, because Python has already applied every stored event through it, or a read that covers them.

Without this check, a watch that ends before its pending check runs loses that check, and no later watch runs it. Checks otherwise run only from a trigger, and the per-watch state dies with the watch. A re-watch then starts from a cursor at or past the last trigger, its replay holds no trigger, `done` never arrives, and the task stays running, because Python completes a task only from `done`. Four rows of "The watch lifecycle" reach that state without it (inferred):

- A watch ends with a catch-up armed, on a `resync`, a 1013 close, or a voice-server restart, before `lastSeen` reaches S2.
- A `resync` skips the terminal `run.updated`, and Python re-watches from the read's `asOfSequence`.
- A replayed launch watches from `state.as_of_sequence` on a thread that has already finished.
- The voice server restarts while the 60 s follow-up wait from step 3 is in flight.

In each case the subscribe check reads fresh state. A settled thread with no pending need emits `done`, a pending need holds it, and a terminal but unsettled run forks a new follow-up wait under step 3. A check on a thread whose task already has its `done` costs one read, and Python drops the second `done` under the cursor rule.

**The cursor rule.** Python keeps one cursor per watched thread, saved in the task record. The rule covers every event type:

| Event                                           | Compared with the cursor              | Moves the cursor                           | Dedupe beyond the cursor                                                                                                                                            |
| ----------------------------------------------- | ------------------------------------- | ------------------------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `run`, `reply`, `needs_you`, `resolved`, `gone` | applied only when `sequence > cursor` | to `sequence`, saved before the next event | `run` on its pair of run id and status, `reply` on `messageId`, `needs_you` and `resolved` on `requestId`                                                           |
| `done`                                          | never                                 | never                                      | applied once per task. A second `done` for a completed task is dropped. When `result.messageId` is already in `T3Link.replies`, `done` speaks only its status line. |
| `watch_ended`                                   | never                                 | never                                      | none                                                                                                                                                                |

`done` is exempt because its `sequence` names the trigger or the subscribe high-water, not the state it reports. The settle read can see state past events the watch has not yet emitted: a reconnect replay emits through the high-water while the read sees the store now, and a live trigger's read sees events committed after it. A cursor moved to the read's sequence would skip those events, and a re-watch would never replay them. A pending `needs_you` could vanish that way. With the exemption, the cursor only ever reaches a sequence whose stored event Python has applied, so a re-watch from it misses nothing (inferred).

A separate applied watermark would also work. It would need a checkpoint frame after each stored event and an idempotent apply for every event type, because events would replay up to the last checkpoint. The exemption costs one rule for the one event that comes from a read, so the design takes it. Python never drops a stored event at or below `done.asOfSequence`, because the step 6 gate sends `done` only after the watch has emitted all of them. Every need at or below `asOfSequence` is also in the read, and the join found none of them pending. A need that commits after `asOfSequence` and before the re-read fails the gate, and the catch-up check sees it. A need that commits after the re-read falls in the window that step 6 bounds, and Python's 10 s watch after `done` still speaks its `needs_you`. A blocking request holds its run in `waiting`, which `waitForThread` already reads as unsettled. A link card and a message-capability question do not hold the run, so only the pending join and the gate catch them.

Python saves each applied request id in `T3Link.requests` with the cursor and any pending spoken line, including its evidence sequences. The request id catches a second event for the same request, such as a recovery rewrite at a new sequence. A `done` or give-up line is saved with the terminal state. If the save fails before a crash, the watch replay, subscribe check, or retry regenerates the line. If the save succeeds, the next turn takes the line from the journal even for a terminal task. Taking a line removes it from the journal before the turn hands it to speech, so a later restart does not replay it. A crash after the take can interrupt that turn's speech.

A `reply` at sequence N and a `done` at N+1 for the same message would speak the same result twice. The `done` includes its result text only if the journal has no reply for that message.

**The pending read builds `needs_you`.** A `runtime-request.updated` payload has an id, a kind, and a status, and no text (`packages/contracts/src/orchestrationV2.ts:1283-1307`, measured). The text lives on the turn item with the same request id: `approval_request` has `prompt` and `reason` (`:1811-1821`), and `user_input_request` has `questions[].question` (`:1766-1773`, `:1437-1440`). On a pending request, the seam reads that turn item with `getThreadRecords(threadId, ["turnItems"], { turnItemRequestIds: [id] })`. `readThread` uses the same join. The kinds map like this (the 5 provider kinds are at `packages/contracts/src/providerPolicy.ts:49-55`):

| Runtime request kind                                                                                            | `needs_you` kind | Prompt                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                            |
| --------------------------------------------------------------------------------------------------------------- | ---------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `command`, `file-read`, `file-change`, `permission`, `mcp-elicitation`                                          | `approval`       | the turn item's `prompt`, else its `reason`, else a fixed line per kind                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                           |
| `user_input`, with `responseCapability` `live` or `message`                                                     | `question`       | the first question, plus "and N more"                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                             |
| any kind, pending with `responseCapability` `not_resumable`                                                     | `in_app`         | a fixed line: "It's stuck on a request that can't be answered any more. You can stop it in the app." T3 refuses both an answer and a dismissal for it (`Orchestrator.ts:10801-10807`, `:11222-11227`, measured). The row is probably unreachable. All five writers of `not_resumable` set a status other than `pending` in the same payload: `cancelled` or `expired` at `ProviderSessionManager.ts:842-849`, `ProviderRuntimeRecoveryService.ts:683-690`, `Orchestrator.ts:12509-12511`, and `ThreadDeletion.ts:123-124`, and `resolved` or `cancelled` at `packages/provider-muse/src/server/adapter.ts:997-999` (measured: a search for `not_resumable` finds these five writers and two readers, `Orchestrator.ts:10801` and `RestartContinuation.ts:75`). It stays because it costs nothing. |
| `dynamic_tool_call`, `auth_refresh`                                                                             | none             | T3 does not count these as waiting on the user (`RestartContinuation.ts:68-72`, measured)                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                         |
| a secret or link card, which the shell reports as `user_input` with `userOnly` (`ProjectionStore.ts:1712-1723`) | `in_app`         | a fixed line: "It needs something from you in the app."                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                           |

A pending request whose turn item is not yet stored gets the fixed line for its kind. The recorded fixture shows which arrives first.

**The pending read checks that the need still waits.** A replay can carry a need's stored event long after Mason answered it, and the distiller has no state, so it cannot tell. The pending read therefore checks current state before it emits `needs_you`, in the same read that fetches the prompt. If the need no longer waits, the event yields nothing. The need's end has its own stored event, which yields `resolved` for a request and a settle check for a card.

- A runtime request must still be among `getThreadRecords(threadId, ["runtimeRequests"], { runtimeRequestStatuses: ["pending"] })`.
- A card must still pass T3's `waitsOnUser` rule (`ProjectionStore.ts:1726-1744`, measured) on its current turn item. Its status is still `waiting`. A `secret_request` also needs its run in `preparing`, `starting`, `running`, or `waiting`. A `link_request` also needs no user-authored message newer than the card's `startedAt`, so a card that Mason moved past by writing in the thread, by voice or in the app, does not replay.

The run check matters because the `secret_request.record` command checks only that the run exists and that the node is its root, not that the run is still live (`Orchestrator.ts:10444-10451`, measured). So a secret card can be stored after its run ended, and T3 never shows it as waiting. `waitsOnUser` and `linkCardStillAsks` are private, so the pending read copies their 13 lines. Unit 1's parity test runs the copy over every card fixture in which the card is the thread's only need, and asserts that it agrees with the shell's `userOnly` pick (`ProjectionStore.ts:1791-1811`). `pendingJoin` needs no change, because it reads that pick directly.

`pendingJoin(shell, records)` in `outcome.ts` is the one rule for "a need is pending", used by `readThread` and by step 4 of the settle read. A card counts when the shell's `pendingRuntimeRequest` has `userOnly`, so T3's own rule decides whether a link card still asks. Otherwise the need is the newest pending runtime request in `records` whose row in the table above maps to `approval`, `question`, or `in_app`. Its prompt comes from the turn item with the same request id.

The shell looks for a waiting card only when it has no pending request or its pick is a `user_input` (`ProjectionStore.ts:1791`). Its sort puts every other kind first (`:1773-1781`), so a pending `dynamic_tool_call` or `auth_refresh` hides a waiting card. The join does not use the pending read's copy of `waitsOnUser` to look past it. When the shell's pick is one of those two kinds, the join answers "blind". The settle read treats "blind" as unsettled and speaks nothing. The request's end stores a `runtime-request.updated`, which triggers the next check, and the shell then picks the card if one still waits. Session release and startup recovery end such a request (`ProviderSessionManager.ts:811-850`, `ProviderRuntimeRecoveryService.ts:676-692`), so the hold does not outlive the provider session (inferred). `readThread` in that state reports no `pending` and the activity `working`, as T3's own waiting rule does (`RestartContinuation.ts:68-72`). The watch still emits the card's `needs_you` from its own stored event.

**The event path.** `watch.ts` follows the order that `/ws` uses for `subscribeThread` (`ws.ts:1033-1123`, measured), on every subscribe and every resubscribe:

1. Read the thread's high-water with `eventStore.latestAgentSequence(threadId)` and the prune floor with `eventStore.prunedThroughSequence` (`apps/server/src/persistence/OrchestrationEventStore.ts:94`, `:101`).
2. If the cursor is below the high-water and below the floor, end the watch with `resync`. A cursor at or above the high-water missed nothing.
3. Measure the replay before reading it, with `eventStore.getAgentReplayStats({ threadId, afterSequence: cursor, throughSequence: highWater, maxEvents })` (`OrchestrationEventStore.ts:69-78`, `:595-623`, measured). End with `resync` if it holds more than 128 events or more than 1 MiB of stored payload JSON. Those are the limits `/ws` applies, `THREAD_RESUME_MAX_REPLAY_EVENTS` and `isThreadReplayRawPayloadSafe` (`orchestration-v2/ThreadStream.ts:18-27`, `ws.ts:1062-1085`, measured), and the seam imports both rather than copying the numbers. The byte budget bounds the memory of one replay read. A count limit alone would let 128 large tool-output events through.
4. Read the replay with `readAgentEvents({ threadId, afterSequence: cursor, throughSequence: highWater })`. A per-thread replay uses the `(stream_id, sequence)` index (`apps/server/src/persistence/Migrations/OrchestrationV2/ApplicationEventSequenceIndexes.ts:14-16`, measured).
5. Check the floor again. Pruning publishes its boundary before it deletes, so a pass during the read shows up here (`ws.ts:1100-1117`, measured). If the cursor is now below it, end with `resync`.
6. Emit the distilled replay, run the subscribe check, then follow `streamStoredEventsFrom({ threadId, afterSequence: highWater })` live. `lastSeen` starts at the cursor, and every scanned event moves it, whether or not it yields a voice event.
7. The live stream fails past 1,000 items or 8 MiB of backlog with `LiveStreamBufferError` (`LiveStreamBudget.ts:13-19`, measured), and that error arrives wrapped in `OrchestratorDomainEventStreamError` (`Orchestrator.ts:16416-16424`, measured). The watch unwraps the cause. On `LiveStreamBufferError` it goes back to step 1 with `cursor = lastSeen`. Any other error ends the watch with `unavailable`, and Python re-watches from its cursor (row `ended-unavailable` of the watch lifecycle).

The stream itself has no prune check, so steps 2 and 5 are the only guard against a silent gap (measured: `streamStoredEventsFrom` passes straight to `eventSink.stream`).

On `resync`, Python reads the thread first and watches again from the read's `asOfSequence` (row `ended-resync`). The new watch's subscribe check emits `done` if the thread has settled, even when the skipped events held the terminal `run.updated`. Sequences are global and increase monotonically (measured). Under the cursor rule above, dropping a cursor event at or below the cursor rules out duplicates, and re-watching from the cursor rules out loss (inferred). The server keeps no per-client cursor.

The outbound socket queue holds 256 frames, and `ws` may hold 1 MiB for one socket. When Python stops reading and either fills, the seam closes with code 1013. Python reconnects and re-watches from its cursors (row `close-1013`). Each socket may hold at most `maxWatches` watches, 16 by default.

Every spoken claim traces to evidence:

| Spoken claim                                              | Evidence                                                                                                                                                     |
| --------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| "Started a thread in t3-voice."                           | `VoiceLaunchResult.sequence`, and the task record                                                                                                            |
| "I've asked T3 to start it."                              | the journaled request id, before a launch result arrives                                                                                                     |
| "It's waiting for your approval to run the tests."        | `needs_you.sequence`                                                                                                                                         |
| "It finished. It said ..."                                | `done.asOfSequence` (the settle read), `done.sequence` (its trigger, or the subscribe high-water), `done.runId`, and `done.result.messageId` or `turnItemId` |
| "T3 refused to start it: ..."                             | the `refused` refusal's `commandId`, whose receipt holds the recorded `reason`                                                                               |
| "Stopping it."                                            | `VoiceInterruptResult.sequence`, saved in the stop task's record                                                                                             |
| "It isn't running anything right now."                    | the `nothing_running` result, saved in the stop task's record                                                                                                |
| "Do you mean the X thread?"                               | `VoiceThreadList.asOfSequence` of the listing the pick came from                                                                                             |
| "It's still working. I'll pass that on when it finishes." | the `thread_busy` refusal, and the held follow-up's journal record                                                                                           |
| "The PR thread is still working. Its last reply said ..." | `VoiceThreadState.asOfSequence`                                                                                                                              |
| "I don't see an active thread like that."                 | `VoiceThreadList.asOfSequence` with `truncated: false`, and the picker's logged decision                                                                     |
| "I don't see it among your 200 most recent threads."      | `VoiceThreadList.asOfSequence` with `truncated: true`, and the picker's logged decision                                                                      |

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

`route.ts` keeps one handler table typed `Record<WireOp, Handler>`, and `voice-seam.json` decodes its `operations` against `VOICE_OPERATIONS`. Adding an operation therefore fails the build until it is handled. Unit 4 mounts the table with all seven entries. `launch`, `send`, and `interrupt` answer `unavailable` there until units 5 and 6 replace those entries, so the order of units holds and the type check stays total. Each request runs in its own fiber, with up to 4 in flight, so a slow launch never blocks a read. Each watch is a forked fiber that writes to the bounded outbound queue, and `unwatch` interrupts it. Watch admission, replacement, and `unwatch` run one at a time per socket.

### Access and ceiling

The voice gets its own bearer session. It never uses the T3 Tots token. Mason mints it once, with the server stopped:

```text
t3 auth session issue --subject voice --label "Voice server (ceiling in voice-seam.json)" \
	--scope orchestration:read --ttl 30d --token-only > "$VOICE_HOME/state/t3-seam-token"
```

`issueSession` passes the subject through and marks the client `deviceType: "bot"` (`EnvironmentAuth.ts:1290-1317`, measured). The session appears in **Settings > Connections** with that label. Mason can revoke it there, with `t3 auth session revoke <id>`, or with `POST /api/auth/clients/revoke`. The speech route does not watch for revocation, so `route.ts` adds it. It reads the session once more before `hello` and races the socket against `SessionStore.awaitInvalidation`, as `ws.ts:3737-3751` does (measured for `/ws`, and a requirement for the seam that its route test checks). Mint the token with the server stopped, because CLI writes to a running server's SQLite are unverified.

The route admits only sessions with subject `voice` and the `orchestration:read` scope. A browser cookie, the T3 Tots token, and an MCP client session all fail that check (an MCP session fails earlier, at `EnvironmentAuth.ts:681-688`, measured).

The token gets `orchestration:read` and not `orchestration:operate`. Outside the seam it cannot launch threads or dispatch commands, because `/ws` requires `operate` for `launchThread` and `dispatchCommand` (`apps/server/src/auth/RpcAuthorization.ts:42`, `:60`, measured). Read scope can still trigger a provider refresh, a pull request invalidation, and VCS refreshes (`RpcAuthorization.ts:69`, `:151-152`, `:185`, `:189`, measured). None of those touch threads. Inside the seam the token can do only what `voice-seam.json` allows. The voice credential therefore cannot reach above the ceiling by going around the seam.

```jsonc
// <T3 home>/voice-seam.json. The seam is off (404) until this file exists. A new socket re-reads it.
{
  "maxRuntimeMode": "auto-accept-edits",
  "operations": ["list", "read", "watch", "launch", "send", "interrupt"],
  "projects": [{ "projectId": "proj_...", "spokenNames": ["voice", "the voice repo"] }],
  "workspace": { "proj_...": { "type": "worktree" } },
  "maxWatches": 16,
}
```

The seam enforces the ceiling in four places:

1. Every operation checks `operations` and the allowed projects. A thread outside them, or one with unlisted lineage, reads as `not_found`, so the voice cannot probe for it.
2. Launch clamps the runtime mode to `maxRuntimeMode` and the interaction mode to `default`, through the mode resolvers.
3. Send preflights the thread's modes against the ceiling before it calls `sendToThread`, so a refused send never restores a worktree. It then runs under a provided `DispatchModeLimit`, which the orchestrator rechecks under the command lock (`Orchestrator.ts:15420-15429`, measured). A thread Mason raised to `full-access` after the preflight is refused, not driven.
   That limit covers only the dispatch. T3 starts a queued run later, in the thread's mode at that time, and checks only the managed ceiling there (`Orchestrator.ts:2449`, measured). So a send to a busy thread queues only when `queueHoldsCeiling` is true: the managed ceiling is at or below `maxRuntimeMode`, or no mode is above it. Otherwise the send refuses with `thread_busy` before `sendToThread`, and a send that still queued, because the thread got busy after the preflight, is cancelled and answers `thread_busy`.
4. Interrupt runs with no `DispatchModeLimit`. The recheck refuses every command on a thread above the limit, and only the managed ceiling exempts commands that lower a mode or start no work (`DispatchModeLimit.ts:67-71`, measured). Without this exception the voice could not stop a thread Mason raised.

The managed-policy ceiling still applies on top of all four (`Orchestrator.ts:1558-1564`, measured).

### The watch lifecycle

Five review rounds each found one more way for a watch to start or end without Python knowing what to do next. In each, a task went silent, with no `done` and no `needs_you`. Each fix handled one path where it occurred, so the next path stayed invisible. This table replaces those scattered rules. It lists every way a watch starts and every way one ends, and what Python does next. `voice/t3/lifecycle.py` holds it as one dict keyed by row, and `sync`, `poll`, and the reconnect loop look up every start and end there instead of branching on their own.

Every row ends in exactly one of three ways:

- **Subscribe.** Python watches from a cursor, and the subscribe check then decides `done` from fresh state.
- **Terminal.** The task takes a terminal status from a seam result, a `done`, or a `gone` event.
- **Give up.** Python speaks the row's line, then saves the task as `Run.LOST`. `LOST` is a new terminal status. It says that the voice stopped following the thread, and nothing about the thread's outcome. Mason can still ask about the thread through Twin B.

Three terms recur in the table:

- **Watch** means `seam.watch(thread_id, after_sequence=link.cursor)`. A link with no cursor reads first instead, so a crash between a replayed launch or a send and its read still reads on restart. If Python already holds `hello.ceiling.maxWatches` watches, the task waits first (row `slot-wait`). The seam keeps one watch per thread, and a second watch on it replaces the first. A live task keeps its thread's watch, a held follow-up gives it up to a live task, and a task that has ended gives it up to any task.
- **Read first** means: call `read_thread` and save any pending need in `T3Link.requests` as `needs_you`. Save its spoken line and the read's `asOfSequence` cursor in the same write, then watch. The read covers every stored event at or below its sequence, so the cursor rule still holds.
- **The retry budget** is eight failures. After each of the first seven, Python waits 1, 2, 4, 8, 16, 30, and 30 s and tries again, and the eighth failure gives up, 91 s after the first. The waits are a guess. Each task has its own budget, and the socket has one. A budget resets once its watch or socket has stayed open for 60 s (guess), or, for a launch, a send, or a read, once that call succeeds. A watch call that succeeds does not reset it, so a watch that keeps ending with `unavailable` still gives up. A row that hands off names the row it hands to, and that row ends in one of the three. A row that retries re-enters the table at the row for the answer it finally gets, and its own ending is the one it reaches when the budget runs out.

Two lines recur. L1 is "I lost track of that thread. It's still in the app." L2 is "I can't see that thread any more." L2 makes no claim that the thread exists, because `not_found` also covers a deleted thread.

| Row                            | When                                                                                                                                                                                                                    | Python does next                                                                                                                                                                                                                                                                                                                                                                                                                               | Ends in                      | Spoken on give-up                                                                                               | Proof in `test_lifecycle.py`                                                                 |
| ------------------------------ | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ---------------------------- | --------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------- |
| `launch-fresh`                 | `launch` returns `replayed: false`                                                                                                                                                                                      | Save the cursor as `launched.sequence`, then watch.                                                                                                                                                                                                                                                                                                                                                                                            | subscribe                    |                                                                                                                 | the watch starts at `launched.sequence`                                                      |
| `launch-replayed`              | `launch` returns `replayed: true`, a retry after a crash                                                                                                                                                                | Journal the link with no cursor, then read first and watch.                                                                                                                                                                                                                                                                                                                                                                                    | subscribe                    |                                                                                                                 | a need pending in the read is spoken once, and the watch starts at the read's `asOfSequence` |
| `launch-refused`               | `launch` answers any refusal code except `unavailable`                                                                                                                                                                  | `FAILED`, with `refusal.spoken()`. `refused` is never spoken as deleted.                                                                                                                                                                                                                                                                                                                                                                       | terminal                     |                                                                                                                 | one case per code, with its status and line                                                  |
| `launch-unavailable`           | `launch` answers `unavailable`, or the socket closes before its result                                                                                                                                                  | After 3 s, say "I've asked T3 to start it", as now. Retry `launch` with the same request id, under the task's budget.                                                                                                                                                                                                                                                                                                                          | give up                      | "I asked T3 to start it, but I couldn't confirm that it started. Check the app."                                | every retry reuses one request id, and the line follows the eighth failure                   |
| `tell`                         | `send` returns, fresh or replayed (`t3_tell`)                                                                                                                                                                           | Journal the link with no cursor, then read first and watch. A watch from `VoiceSendResult.sequence` replays only events after the send, and the subscribe check never emits `needs_you`, so a need already pending would never be spoken. The settle read selects the highest-ordinal run (`ThreadManagementService.ts:561-565`, measured), which is the send's own run or a later one (inferred: a queued send's run takes the next ordinal). | subscribe                    |                                                                                                                 | a need pending before the send is spoken from the read                                       |
| `tell-refused`                 | `send` answers any refusal code except `unavailable`                                                                                                                                                                    | `FAILED`, with `refusal.spoken()`.                                                                                                                                                                                                                                                                                                                                                                                                             | terminal                     |                                                                                                                 | one case per code                                                                            |
| `tell-unavailable`             | `send` answers `unavailable`, or the socket closes before its result                                                                                                                                                    | Retry `send` with the same request id, under the task's budget.                                                                                                                                                                                                                                                                                                                                                                                | give up                      | "I asked T3 to pass that on, but I couldn't confirm that it arrived. Check the app."                            | every retry reuses one request id                                                            |
| `tell-busy` | `send` answers `thread_busy`: the thread is working and its queue cannot hold the ceiling | `HELD`, under a new request id, because `thread_busy` delivered nothing and the seam answers every later copy of a send it queued and then cancelled with `thread_busy` again (`send.ts`). Say "It's still working. I'll pass that on when it finishes." once. Watch the thread unless a live task watches it already. A held follow-up's watch speaks a need, and no run or reply. | subscribe | | nothing is sent again before the thread's `done` |
| `held-release` | A `done` on a thread that holds follow-ups, whichever task watches it | Send each held follow-up, oldest first, with its saved request id. The first one takes row `tell` and a watch anchored on its own run. A later one answers `thread_busy` and stays held for the next `done`. | the row for its answer | | each `done` sends each held follow-up once |
| `held-dropped` | Mason takes a held follow-up back, or stops its thread | `CANCELLED`, with "Okay, I won't pass that on." Hand the watch to another held follow-up on the thread, or unwatch. | terminal | | a later `done` sends nothing |
| `ask`                          | Twin B (`t3_ask`)                                                                                                                                                                                                       | Start no watch. The task completes from the list or the read, as `_ask_about` shows. A list or read that fails is `FAILED`, "I can't reach T3 right now."                                                                                                                                                                                                                                                                                      | terminal                     |                                                                                                                 | no watch call is made                                                                        |
| `restart-unstarted`            | On restart, the journal holds a request id with no thread                                                                                                                                                               | Call `launch` or `send` again with the journaled request id.                                                                                                                                                                                                                                                                                                                                                                                   | the row for its answer       |                                                                                                                 | the replay reaches `launch-replayed` or `tell`                                               |
| `restart-live`                 | On restart, the journal holds a started task that is neither terminal nor `LOST`                                                                                                                                        | Watch from the saved cursor, or read first when the link has none.                                                                                                                                                                                                                                                                                                                                                                             | subscribe                    |                                                                                                                 | each live task re-watches from its own cursor                                                |
| `restart-held` | On restart, the journal holds a held follow-up | Watch from the saved cursor, or read first when the link has none, unless a live task watches the thread. | subscribe | | its `done` sends with the saved request id |
| `restart-ended` | The journal holds a terminal or `LOST` task on restart | Keep its queued lines for the next turn and skip the watch. A crash inside the 10 s window after `done` ends that window. | terminal | | no watch call is made |
| `crash-before-save` | The server crashes before the journal saves an applied event or terminal state with its pending line | Resume from the previous saved state. The watch replay, subscribe check, or retry regenerates the line. | subscribe | | replay regenerates a cursor event, a `done`, and a give-up line |
| `crash-after-save` | The server crashes after the journal save | Resume from the saved task state. The next turn takes any pending line from the journal. A line already taken stays removed. | the row for the saved state | | an untaken line is delivered once and a taken line stays removed |
| `slot-wait`                    | A task needs a watch while Python holds `maxWatches` watches                                                                                                                                                            | Queue the task, first in first out. Say once, "I'm following N threads already. I'll pick this one up when one of them finishes." Watch when a slot frees.                                                                                                                                                                                                                                                                                     | subscribe                    |                                                                                                                 | the 17th task watches after the first watch ends                                             |
| `watch-retry`                  | The `watch` call answers `unavailable`, or `too_many_watches`, which means Python's count was off, as while old watches end after a reconnect                                                                           | Watch again under the task's budget.                                                                                                                                                                                                                                                                                                                                                                                                           | give up                      | L1                                                                                                              | the line follows the eighth failure                                                          |
| `watch-refused`                | The `watch` call answers any other code: `operation_not_allowed`, `project_not_allowed`, `not_found`, `gone`, `invalid_request`, or a code a watch never returns (`above_ceiling`, `refused`, `no_default_model`)       | Give up at once. A retry cannot change the answer.                                                                                                                                                                                                                                                                                                                                                                                             | give up                      | L2 for `project_not_allowed`, `not_found`, and `gone`. L1 for the rest.                                         | one case per code                                                                            |
| `ended-unavailable`            | `watch_ended` reason `unavailable`: a live-stream error other than `LiveStreamBufferError` (step 7 of the event path)                                                                                                   | Watch again from the cursor, under the task's budget. The cursor only ever names an applied stored event, so the new watch misses nothing.                                                                                                                                                                                                                                                                                                     | give up                      | L1                                                                                                              | the new watch starts at the saved cursor, and the line follows the eighth failure            |
| `ended-resync`                 | `watch_ended` reason `resync`, for a live task                                                                                                                                                                          | Read first, then watch.                                                                                                                                                                                                                                                                                                                                                                                                                        | subscribe                    |                                                                                                                 | the watch starts at the read's `asOfSequence`                                                |
| `read-failed`                  | The read in `launch-replayed`, `tell`, or `ended-resync` fails                                                                                                                                                          | `not_found` gives up at once. `unavailable` retries the read under the task's budget.                                                                                                                                                                                                                                                                                                                                                          | give up                      | L2 for `not_found`, L1 for `unavailable`                                                                        | both cases                                                                                   |
| `ended-not-found`              | `watch_ended` reason `not_found` or `gone`                                                                                                                                                                              | Give up at once.                                                                                                                                                                                                                                                                                                                                                                                                                               | give up                      | L2                                                                                                              | both reasons                                                                                 |
| `gone-event`                   | A `gone` event, from a stored `thread.deleted`                                                                                                                                                                          | `FAILED`, "That thread was deleted."                                                                                                                                                                                                                                                                                                                                                                                                           | terminal                     |                                                                                                                 | the `watch_ended` that follows changes nothing                                               |
| `done-event` | A `done` event for a live task | Save the status from `done.status` with its pending spoken line. Keep the watch for 10 s, then unwatch. Another live task on the thread, such as a follow-up that queued, takes the watch at once. | terminal | | a `needs_you` inside the 10 s is spoken, and the task stays terminal |
| `ended-after-terminal`         | Any end of a watch for a terminal or `LOST` task: any `watch_ended`, Python's own `unwatch`, or a socket close                                                                                                          | Nothing. It ends the 10 s window, with no read and no re-watch.                                                                                                                                                                                                                                                                                                                                                                                | terminal                     |                                                                                                                 | a `resync` inside the window makes no call                                                   |
| `close-1013`                   | The socket closes with 1013, because Python stopped reading                                                                                                                                                             | Reconnect at once. After `hello`, watch each live task from its cursor. A further close uses `close-other`.                                                                                                                                                                                                                                                                                                                                    | subscribe                    |                                                                                                                 | every live task re-watches from its own cursor                                               |
| `close-other`                  | The socket closes with any other code or drops, or an upgrade fails with a connection error or a 5xx. A server restart lands here, and the readiness middleware holds the upgrade until orchestration accepts commands. | Reconnect under the socket's budget. After `hello`, watch each live task from its cursor.                                                                                                                                                                                                                                                                                                                                                      | give up, for every live task | "I've lost my connection to T3, so I've stopped following your threads. They're still in the app.", spoken once | a server restart inside the budget loses nothing, and one line follows the eighth failure    |
| `upgrade-401` or `upgrade-403` | An upgrade answers 401 for a missing or invalid credential, or 403 for a session that lacks the voice subject or read scope. Revocation closes the live socket first.                                                   | Give up at once, for every live task.                                                                                                                                                                                                                                                                                                                                                                                                          | give up                      | "T3 no longer accepts my access, so I've stopped following your threads. They're still in the app."             | a revocation closes the socket, and no second upgrade is tried                               |
| `upgrade-404`                  | An upgrade answers 404, because `voice-seam.json` is gone                                                                                                                                                               | Give up at once, for every live task.                                                                                                                                                                                                                                                                                                                                                                                                          | give up                      | "The voice seam is off, so I've stopped following your threads. They're still in the app."                      | no second upgrade is tried                                                                   |

The table has 31 rows. A second test in `test_lifecycle.py` reads `voice/t3/seam.schema.json` and fails when a `watch_ended` reason or a refusal code has no entry in `lifecycle.py`. A new reason on the server then fails the Python build until someone gives it a row.

### What changes in `voice/`

```text
voice/t3/seam.py           T3Seam client: one socket on a background thread, typed calls, drain() for events
voice/t3/types.py          Hello, Project, ThreadSummary, ThreadList, ThreadState, Launched, Sent, VoiceEvent
voice/t3/seam.schema.json  written by write_seam_schema.ts, compared by the server's golden test; a Python test checks types.py against it
voice/t3/write_seam_schema.ts  regenerates seam.schema.json from protocol.ts
voice/t3/journal.py        T3Journal: request ids, thread links, cursors, and interrupt results, saved before each call
voice/t3/lifecycle.py      the watch lifecycle table as one dict keyed by row, and Follow, which runs it
voice/t3/pick/             the Pick type, the prefilter, and the model picker, from the Twin B eval
voice/live/t3_tasks.py     T3TaskService(MockTaskService)
```

`T3TaskService` keeps the `MockTaskService` interface (`submit_delegate`, `sync`, `poll`, `next_due`, `snapshot`, `take_updates`, `executions`), as `voice/bot/root_tasks.py` already does for the root thread (measured). Tasks that are not T3 work keep the parent's behavior.

Request ids change shape. Today the live path builds a conversation id from the turn's clip name, `conv_{stem}` (`voice/prototype/run.py:862`, with `stem` set per turn at `voice/live/server.py:212`), and task ids count up per service (`voice/prototype/tasks.py:223`). The bot spike does the same (`voice/bot/seam_replay.py:39`). Two turns can therefore mint the same `conv_x:task_3`, and the seam would replay an old launch while the voice said "started" for different work (inferred). The seam takes only ULIDs, and Python mints one per write.

```python
@dataclass
class T3Link:
	request_id: str             # the launch's or the send's ULID, journaled before the call
	project_id: str
	thread_id: str | None = None
	cursor: int | None = None   # the last applied stored-event sequence; None until a launch result or a read sets it
	evidence: list[int] = field(default_factory=list)
	run_id: str | None = None
	replies: dict[str, int] = field(default_factory=dict)   # messageId -> sequence, for dedupe
	requests: dict[str, str] = field(default_factory=dict)  # requestId -> last applied type, "needs_you" or "resolved"
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
		# Every watch start and end, every refusal of launch, send, or watch, and every socket close goes to
		#   self.follow, which runs voice/t3/lifecycle.py. poll branches on none of them itself.
		# Cursor events (run, reply, needs_you, resolved, gone): skip one if event.sequence <= link.cursor,
		#   else apply it, then advance and save the cursor.
		# done and watch_ended never touch the cursor.
		# reply: skip a messageId already in link.replies.
		# needs_you: a one-time update "It needs you: <prompt>"; in_app says to open the app.
		# needs_you and resolved: skip a requestId that link.requests already holds with that event type.
		#   Save it to link.requests in the same write as the cursor.
		# resolved: speak it only if link.requests holds that requestId as needs_you. Otherwise save it
		#   unspoken: the need ended before the voice announced it.
		# done: skip it if the task is already terminal. Else, by done.status:
		#   completed   -> COMPLETED, with the first two sentences of done.result.text
		#   failed      -> FAILED, "It failed.", plus the first sentence of done.result.text if any
		#   interrupted -> CANCELLED, "It was stopped."
		#   cancelled   -> CANCELLED, "It was cancelled."
		#   rolled_back -> CANCELLED, "Its work was rolled back."
		#   If done.result.messageId is in link.replies, speak only the status line, not the text again.
		#   Speak it, then save the terminal state.
		#   Keep the watch for 10 s, applying cursor events as usual, then unwatch. A restart ends the window.
		#   A needs_you in that time is spoken as "It also needs you: <prompt>", and the task stays terminal.
		# run: progress only. A terminal run event never completes a task.
		# gone (event): FAILED, "That thread was deleted." Only a stored thread.deleted produces it.
		# A launch refusal "refused" is FAILED, "T3 refused to start it: <reason>", or "T3 refused to start it."
		#   when reason is absent. Never spoken as deleted.
```

A T3 task record links to its thread through `Execution.t3`, a `T3Link`, which `T3Journal` saves so a restarted voice server re-watches each thread from its cursor. `take_updates` and `build_snapshot` gain one field per entry, `evidence: {thread_id, sequences}`. Snapshot status still comes from the `Execution` alone, so T3 adds no state that the voice model can read around the task record.

The gate and the end-of-utterance turn treat a launch as a side effect:

1. `t3_start` and `t3_tell` are held by `needs_hold` until the gated end-of-utterance turn runs them with `run_task`. The voice session never calls tools, as `docs/voice/eou-handoff.md` prescribes.
2. The project pick runs inside `sync`, before the launch. An ambiguous pick becomes a question and starts nothing.
3. The snapshot shows the task as pending until the launch result returns. `sync` waits up to 3 s (guess). After that the voice says "I've asked T3 to start it", which is true from the journal alone. The started acknowledgement arrives later as a `task_update`. A retry is safe because the request id replays.
4. The voice says "started" only from a `VoiceLaunchResult` and "done" only from a `done` event.

### How to measure the Twin B pick before trusting it

The PM's Twin B eval brief runs this eval on branch `voice/twinb-pick`, before the seam exists. It follows the evaluation rules in the lane's common worker rules. Labels are written before any picker run, known failures come first, every output is saved before scoring, and Mason reads 10 scored outputs, with their reasons, before anyone trusts the result. Mason reads results. He does not label.

**The snapshots** are four frozen `VoiceThreadList` values, built from a read-only copy of the real `statev2.sqlite` with varied windows and project subsets. Each applies the seam's listing rule exactly: every active shell, read with `omitOlderSettled: false`, filtered to listed lineage and allowed projects, sorted newest first, and cut at 200 with `total` and `truncated` set. Subagent children and delegated threads therefore never appear. A snapshot built from the trimmed shell, which keeps only the 25 newest settled top-level threads, would hide the case below and pass a picker that fails it. They include the cases the slices need: shared title prefixes, several threads in one project, two threads that finished recently, and at least one snapshot with more than 25 settled threads whose gold thread is older than the 25th. One snapshot is cut, with `truncated: true`, so its nonexistent-thread cases check that the voice says "not among your most recent threads" and never "no such thread". Snapshots, labels, and raw outputs stay local. Only the harness, the prefilter, its test on a synthetic fixture, and an aggregate results doc with no real titles go into the repo.

**The labels** come from an Opus labeler that sees the snapshots and the slice table and never sees a picker output. The known-failure cases are written first, by hand, from the list below. The labels are committed to the local eval directory before the first picker run, and the PM reads a sample of them first.

| Slice                                                           | Count | Gold label                                  |
| --------------------------------------------------------------- | ----- | ------------------------------------------- |
| Exact or near-exact title                                       | 30    | one id                                      |
| Paraphrase or partial ("the flaky test one")                    | 40    | one id                                      |
| By project ("the unity thread")                                 | 15    | one id, or ask when the project has several |
| By state or recency ("the one waiting on me")                   | 20    | one id                                      |
| Back-reference to a voice-launched task ("how's that going")    | 15    | one id, resolved from the task record       |
| Ambiguous, two or more plausible                                | 25    | ask, with the candidate set                 |
| A thread that does not exist                                    | 15    | none, worded by `truncated`                 |
| A settled thread older than the 25 newest settled               | 10    | one id                                      |
| No subject at all ("how's it going?") with no recent voice task | 10    | ask                                         |
| A same-title thread outside the allowed projects                | 10    | none, or the allowed one                    |

**The metric** is a per-slice outcome matrix with six outcomes: correct, wrong thread named, asked when one was right, picked when it should have asked, said none when the thread exists, and invalid id. An invalid id cannot reach the voice, because `Pick` turns an id that is not in the list into a question. The eval asserts that this conversion still happens. Each case runs 3 times at the deployed settings and passes only if all 3 pass. The eval also tracks p95 pick latency.

**The pass bar** (guess at every number):

- Zero wrong-thread picks in the ambiguous, nonexistent, and outside-project slices.
- At most 2 wrong-thread picks across all 190 cases.
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
- An older settled thread, past the 25 newest settled, must be found, not answered as none.
- A back-reference after a reconnect must resolve through the task record, not through the model.

**What the metric cannot see.** It scores the pick on frozen lists, not the spoken summary that follows, and not live lists that change while Mason talks. It uses typed utterances, so ASR errors on thread titles are out of scope.

A separate 30-utterance set over `hello.projects` measures Twin A's project pick with the same three outcomes and the same arms. It gates open question 2.

## Synthesis decision

The arena ran two runners on the same task and grounding, each assigned one pole of the "who calls the thread operations" question so the candidates would differ in structure. Seat 1 ran on Claude Opus 5.5 at high effort. Seat 2 ran on DeepSeek V4.1 Flash Fast at max effort, through Command Code, which is the fallback for that seat because Codex is off this lane. The cross-judge was skipped, because the brief allows only the explorer and runner subagents. I scored the candidates alone against the rubric below. A three-seat interrogate pass then reviewed the result and returned "accept with changes". This revision designs in each of its eight blocking changes.

| Candidate       | Who performs operations                                                                                                                                  | Upstream files touched                                                               | Transport                  |
| --------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------ | -------------------------- |
| 1. Service seam | seam code calls `ThreadLaunchService` and `ThreadManagementService`                                                                                      | 1 (`server.ts`, 2 lines)                                                             | voice-owned JSON WebSocket |
| 2. Agent seam   | a standing frontier dispatcher thread launches, a standing fast-model "lookout" thread picks, and the seam makes every other call through in-process MCP | 1 (`server.ts`, 2 lines), and it imports MCP internals that churn 36 commits a month | voice-owned JSON WebSocket |

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
- We accept a spoken give-up after about two minutes of failed retries, in exchange for a task that never goes silent. A given-up task says only that the voice stopped following the thread, and Twin B can still read the thread.
- We accept an extractive result summary, the first two sentences of T3's own result for the settled run, over a model-written one. It is less fluent, and it cannot invent.
- We accept a settle read after every `run.updated`, `subagent.updated`, and `provider-thread.updated`, and after each request or card that stops waiting, coalesced to one in flight per watch, plus a 60 s follow-up wait while a terminal run is unsettled, plus a catch-up check whenever the thread commits during a check, plus one check after every subscribe's replay. That costs a few reads per run, in exchange for never saying "done" from a stale event and never missing a settle that stores no event (inferred cost).
- We accept reading every active shell for each Twin B listing, more than the trimmed snapshot that T3's own clients read, in exchange for a list from which "no such thread" is true. Unit 3 measures the read on a copy of real data.
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
4. **Should voice ever steer a running thread?** Recommendation: not in milestone 1. A voice follow-up queues behind the active run, or refuses where the queue cannot hold the ceiling, so it never changes a turn mid-flight. Steering can come later behind an explicit phrase.
5. **Is a read-scoped token that writes only through the seam acceptable?** Recommendation: yes, because it is the only shape in which the credential cannot launch above the ceiling over `/ws`. The other option is an `operate` token, which shows "operate" in Settings and can launch at `full-access` outside the seam.

## Risks

- **Event order within a turn is inferred.** Whether an approval's turn item is stored before its runtime request decides whether `needs_you` gets the real prompt or the fixed line. The settle read no longer depends on where a need's event falls against its run's terminal `run.updated`, because the step 6 gate re-runs the check when anything commits after its snapshot. The order still sizes one window. A need that commits after the gate's re-read is missed by every check, and only Python's 10 s watch after `done` speaks it, if the voice server does not restart inside that window. The first unit records, for each need in its fixtures, whether its event precedes the terminal `run.updated`. If one follows it, the PM decides before unit 2 starts whether 10 s is enough.
- **The launch-policy copy can drift.** The parity test catches the drift, and the fallback is in-process MCP for launch.
- **Reads can write.** `getThreadRecords` first runs `ensureLegacyTranscript`, which can import a v1 transcript (`ThreadManagementService.ts:655-677`, `:1742-1746`, measured). `readThread` calls it once before its read-only transaction. If the second call inside `getThreadRecords` tries to write within that transaction, the unit test fails and `readThread` moves the records read out of the transaction, reading the sequence first.
- **The settle read uses `waitForThread` with a 1 ms timeout.** That still opens and closes event subscriptions on each check (inferred from `ThreadManagementService.ts:1077-1116`). Unit 2 counts the reads per run, and if they cost too much it calls the same evaluation without the wait.
- **The CLI mint and a running server share SQLite.** Mint with the server stopped until concurrent writes are verified.

## Next implementation steps

Each unit is one small PR into `voice/main` that ends in a check. A unit starts only after the units it depends on have merged. Units 1 to 6 live in `apps/server/src/voiceSeam/` and do not mount the route until unit 4, so `server.ts` stays untouched until then.

1. **The distiller and the event path.** Write `events.ts`, the pure map from a stored event to a signal, with a settle-check flag on every `run.updated`, `subagent.updated`, and `provider-thread.updated`. Write `watch.ts`, with the two prune checks, the replay measured against both the count and the payload-byte budgets before it is read, the live follow from the high-water, the resubscribe loop that unwraps `LiveStreamBufferError`, the per-watch `reply` dedupe on `messageId`, and a settle hook that runs one check at a time on the watch fiber, coalesces triggers, and runs exactly once after every subscribe's and resubscribe's replay, trigger or not, with the high-water as its `sequence`. `lastSeen` starts at the cursor. A check answers with nothing, with a `done` and its `asOfSequence`, or with a catch-up at a sequence N. The watch sends a `done` only while `lastSeen >= asOfSequence`, and otherwise turns it into a catch-up at that sequence. It runs a catch-up check once `lastSeen >= N`, coalesced with any other pending trigger. A result from a forked follow-up wait goes through the same rule on the watch fiber. Until unit 2, the hook emits nothing. The watch starts with one shell read, which ends it with `watch_ended` reason `not_found` for a missing, deleted, or disallowed thread, and which keeps the shell's `projectId` for the settle read. The distiller also flags a settle check on a `runtime-request.updated` that leaves `pending`, and on a `secret_request` or `link_request` `turn-item.updated` that is no longer waiting. The watch emits a `needs_you` only after the pending read finds that the need still waits now: a runtime request still pending, or a card that still passes the pending read's copy of `waitsOnUser`. Record stored events from Replay (dev) turns into `fixtures/`: a plain reply, an approval, a user-input question, an interrupted run, a failed run, and a run that starts a subagent. Add two fixtures that Replay (dev) may not produce. If it cannot, record them from the orchestrator in a test. The first is a link card, the `link_request.record` command that `PeerLinkRequests.ts:124` sends, asked during a run that then completes, and then answered. The second is a `user_input` with `responseCapability` `message`, asked and then answered. Codex sends one for `responseMode: "message"` (`Adapters/CodexAdapterV2.ts:4801-4806`), and recovery writes one for a question that outlived its session (`ProviderRuntimeRecoveryService.ts:666-673`). Proof: `events.test.ts` is a table over those fixtures. Its first rows record the event order the risks name, including whether each need's event precedes its run's terminal `run.updated`. `watch.test.ts` covers a cursor below the floor that resyncs before the replay, a prune that lands during the replay read and resyncs after it, a wrapped buffer error that resubscribes from `lastSeen` with no duplicate and no gap, a replay over 1 MiB of payload that resyncs, a cursor at the high-water that replays nothing and still calls the settle hook once with the high-water as its `sequence`, a replay with no trigger that calls the hook once, a resubscribe that calls it again, and a `secret_request` recorded after its run ended, through the orchestrator in a test, that yields no `needs_you`. Three more cases cover a stale need in a replay. A replay that holds a request's `pending` event and its resolution, and one that holds a card's waiting event and its answer, yield no `needs_you`. The link-card fixture replayed after a user-authored message in the thread yields none. A parity test runs the copy of `waitsOnUser` over every card fixture in which the card is the thread's only need, and asserts that it agrees with the shell's `userOnly` pick. Depends on nothing.
2. **The settle read and the run result.** Write `outcome.ts`: the `waitForThread` check, `pendingJoin`, the 60 s follow-up wait, and `subagentResultForRun` over `resultRun`'s records. Wire it into the settle hook as the only source of `done`. Proof: `outcome.test.ts` over fixtures shows an old run's terminal event replayed while a newer run is active emits no `done`, a parent emits `done` only after its subagent settles, with `runId` equal to `resultRun.id` when the watch has no anchor and the anchor's own outcome when it has one, a failed run's result is its error text, a watch anchored on a rolled-back run emits `done` with status `rolled_back`, and a message with two finals yields one `reply`. A settle check that fails with `ThreadManagementThreadNotFoundError` ends the watch with `gone`, not `unavailable`. `watch.test.ts` gains two pending cases, one for the link card and one for the message-capability question. In each, the run settles with the need still pending, and the need's stored event lands after the last emitted event and at or below the read's sequence. The watch emits no `done` and stays open, then emits that `needs_you` from its stored event. It emits `done` only after the settle check that the need's end triggers, with the trigger's `sequence` and an `asOfSequence` read with the result. The test asserts that `done` never precedes that `needs_you`. Two more cases cover the step 6 gate, one with a link card and one with a message-capability question. In each, the need's stored event commits after the run's terminal `run.updated` and after the check's step 4 snapshot, injected by a test layer between the snapshot and the re-read. The check emits no `done`, the watch emits the need's `needs_you` with that event's sequence, and the catch-up check that follows also emits no `done`. A third gate case commits an unrelated event after the snapshot and asserts that the catch-up check emits `done`, with an `asOfSequence` at or above that event. A pending `dynamic_tool_call` over a waiting link card holds `done` while the request is pending, and still holds it after the request resolves, because the shell then picks the card. Two cases cover the subscribe check. A watch started at the high-water of a thread that has already settled emits `done`, with `sequence` equal to that high-water. A watch whose cursor is past the terminal `run.updated`, as after a `resync` re-watch from a read's `asOfSequence`, emits `done` although its replay holds no trigger. Depends on 1.
3. **List and read.** Write `listThreads` and `readThread`, with the one read-only transaction, the lineage and project filter, `deriveActivity` from the shell row, and `pendingJoin` from unit 2. Proof: `VoiceSeam.test.ts` on a seeded projection shows a subagent child and a delegated thread are absent from the list and read as `not_found`, a 26th-newest settled thread is listed, a cut list sets `total` and `truncated`, every runtime request kind maps as the table says, a link card after its completed run and a pending message-capability question both read as `waiting_on_you` with a `pending`, and a watch from a read's `asOfSequence` misses no event committed after the read. Depends on 1 and 2.
4. **The route, the protocol, and the mount.** Write `route.ts`, `protocol.ts`, `ceiling.ts`, and the two `server.ts` lines, and add the `server.ts` entry to `docs/voice/seam.md`. The handler table has all seven entries, with `launch`, `send`, and `interrupt` answering `unavailable`. Proof: `route.test.ts` shows the seam answers 404 with no `voice-seam.json`, refuses a session without subject `voice`, and closes the socket when the session is revoked. `protocol.test.ts` fails when `voice/t3/seam.schema.json` is stale. `voice/ci/check_seam.py` passes. Depends on 3.
5. **Launch.** Write `resolveLaunchPolicy`, the clamp, the seam's own `CommandReceiptStoreV2` layer, and the receipt gate, and replace the `launch` stub. Proof: the parity test against `t3_thread_launch` compares model, runtime mode, interaction mode, and workspace. A launch whose receipt exists never calls `ThreadLaunchService.launch`, even with its events pruned. Each row of the receipt table answers as it says: a rejected launch receipt answers `refused` with the recorded error, a rejected initial message answers `refused` with the thread id, and only an accepted launch whose thread was deleted answers `gone`. Depends on 4.
6. **Send and interrupt.** Write the send receipt gate, the preflight, and `DispatchModeLimit`, and interrupt without it, replacing the last two stubs. Proof: on a thread raised above the ceiling, send refuses with `above_ceiling` and no worktree restore runs, while interrupt succeeds. A send retried with the same request id returns the same `messageId` and run, a rejected send receipt answers `refused`, and an accepted send replayed with its events pruned still answers. A second interrupt with the same request id answers `nothing_running`. Depends on 5.
7. **The Python client.** Write `voice/t3/seam.py` and `voice/t3/types.py`. The client reports typed socket ends, including `close-1011` for startup or session-check failures and `upgrade-403` for a session that lacks the voice subject or read scope. A drain yields queued events before the socket end. The Python tests compare the frame fields with the golden schema and drive the client against a local fake server. Depends on 4.
8. **The task service, the journal, and the lifecycle.** Write `voice/t3/journal.py`, `voice/t3/lifecycle.py`, and `voice/live/t3_tasks.py`, with ULID request ids, the journal-before-call order, cursors, reply dedupe, `Run.LOST`, and evidence on every update. Proof: `test_lifecycle.py` is a table test with one case per row of "The watch lifecycle", each asserting the row's proof against a fake seam and a fake clock. Its `ended-unavailable` case ends a watch with reason `unavailable`, asserts that the next watch starts from the saved cursor after a 1 s wait, and after the eighth failure asserts the line "I lost track of that thread. It's still in the app." and the status `LOST`. Its `watch-retry` and `watch-refused` cases do the same for a refused `watch` call. A second test fails when `seam.schema.json` holds a `watch_ended` reason or a refusal code with no entry in `lifecycle.py`. Other unit tests show that two turns never share a request id, "done" comes only from a `done` event, a `done` never moves the cursor, an applied request id survives a restart in `T3Link.requests`, a `resolved` for a request the voice never announced is saved unspoken, and the saved interrupt result survives a retry. Depends on 7.
9. **The pickers.** Wire in `voice/t3/pick/` from the Twin B eval PR, and run the 30-utterance Twin A project-pick eval. Proof: the Twin B results doc, `docs/voice/twinb-pick-eval.md`, and a matching Twin A results table. Until Twin A passes, `pick_project` uses only the `spokenNames` match. Depends on 8 and on the Twin B eval.
10. **One integrated pass.** Run the fork's dev server with its own T3 home and the Replay (dev) provider, and drive both twins with synthetic audio through the live voice server. Proof: a run transcript where each spoken line carries its evidence sequence and the threads appear in the web client. Mason's live call then closes the milestone. Depends on 1 to 9.
