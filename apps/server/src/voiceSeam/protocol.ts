import {
  CommandId,
  EnvironmentId,
  MessageId,
  NonNegativeInt,
  OrchestrationV2RunStatus,
  OrchestrationV2RuntimeRequest,
  PositiveInt,
  ProjectId,
  RunId,
  RuntimeMode,
  ThreadId,
  TrimmedNonEmptyString,
  TurnItemId,
} from "@t3tools/contracts";
import * as Schema from "effect/Schema";

import { VoiceOperation, VoiceSeamConfig } from "./ceiling.ts";

/** The only wire definition. Every frame the route sends is encoded through these schemas. */

/** A stored event's global sequence, or the sequence a read was true as of. */
const Sequence = NonNegativeInt;

/** The most threads a list carries; Python's picker reads at most this many. */
export const LIST_LIMIT = 200;

export const RefusalCode = Schema.Literals([
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
]).annotate({ identifier: "RefusalCode" });

export const WatchEndReason = Schema.Literals([
  "gone",
  "resync",
  "not_found",
  "unavailable",
]).annotate({ identifier: "WatchEndReason" });

export const ThreadActivity = Schema.Literals([
  "working",
  "waiting_on_you",
  "waiting_on_background",
  "queued",
  "idle",
  "finished",
  "failed",
  "interrupted",
  "cancelled",
  "rolled_back",
]).annotate({ identifier: "ThreadActivity" });

/** The five statuses `isTerminalRunStatus` accepts. */
export const VoiceTerminalStatus = Schema.Literals([
  "completed",
  "failed",
  "interrupted",
  "cancelled",
  "rolled_back",
]).annotate({ identifier: "VoiceTerminalStatus" });

/** A ULID that Python mints once per write and journals before the call. */
const VoiceRequestId = Schema.String.check(Schema.isPattern(/^[0-7][0-9A-HJKMNP-TV-Z]{25}$/));

/** One option of a spoken question. The description is context for the voice's choice, never read out. */
const VoiceQuestionOption = Schema.Struct({ label: Schema.String, description: Schema.String });

/**
 * One question of a request the voice may answer. Only a request that fits the bounds in pending.ts and asks only
 * single-choice questions carries them; any other question is answered in the app.
 */
export const VoiceQuestion = Schema.Struct({
  id: Schema.String,
  question: Schema.String,
  options: Schema.Array(VoiceQuestionOption),
  /** Whether the caller's own words are an answer when no option fits. */
  ownWords: Schema.Boolean,
}).annotate({ identifier: "VoiceQuestion" });
export type VoiceQuestion = typeof VoiceQuestion.Type;

const VoicePending = Schema.Struct({
  requestId: Schema.String,
  /** approval and question can be relayed. in_app means only the app can act on it. */
  kind: Schema.Literals(["approval", "question", "in_app"]),
  /** What raised it: the runtime request's kind, a link or secret card, or `card` when only the shell names it. */
  source: Schema.Union([
    OrchestrationV2RuntimeRequest.fields.kind,
    Schema.Literals(["link_request", "secret_request", "card"]),
  ]),
  /** Context for the voice's own words, never read out. */
  prompt: Schema.String,
  /** What a question asks, for the voice to say and answer. Absent for any request the voice cannot answer. */
  questions: Schema.optionalKey(Schema.Array(VoiceQuestion)),
});

/** T3's own result for a run. Empty text when the run stored no message and no result item. */
const VoiceRunResult = Schema.Struct({
  text: Schema.String,
  messageId: Schema.NullOr(MessageId),
  turnItemId: Schema.NullOr(TurnItemId),
});

const VoiceProject = Schema.Struct({
  projectId: ProjectId,
  name: Schema.String,
  spokenNames: Schema.Array(Schema.String),
});

const VoiceThreadSummary = Schema.Struct({
  threadId: ThreadId,
  projectId: ProjectId,
  title: Schema.String,
  activity: ThreadActivity,
  lastActivityAt: Schema.String,
  pinned: Schema.Boolean,
});

export const VoiceThreadList = Schema.Struct({
  threads: Schema.Array(VoiceThreadSummary),
  total: NonNegativeInt,
  truncated: Schema.Boolean,
  asOfSequence: Sequence,
}).annotate({ identifier: "VoiceThreadList" });
export type VoiceThreadList = typeof VoiceThreadList.Type;

export const VoiceThreadState = Schema.Struct({
  threadId: ThreadId,
  projectId: ProjectId,
  title: Schema.String,
  activity: ThreadActivity,
  run: Schema.NullOr(
    Schema.Struct({
      runId: RunId,
      status: OrchestrationV2RunStatus,
      startedAt: Schema.NullOr(Schema.String),
    }),
  ),
  pending: Schema.NullOr(VoicePending),
  /**
   * Whether T3 stored what the pending request asks (a prompt, a reason or a question). When false its prompt is a fixed
   * line about the kind of request, and the voice cannot tell the person what this one would do.
   */
  pendingDetailed: Schema.optionalKey(Schema.Boolean),
  /** `runId` is the run the reply came from, which is not `run` when the latest run has not replied yet. */
  lastReply: Schema.NullOr(
    Schema.Struct({ ...VoiceRunResult.fields, runId: RunId, at: Schema.String }),
  ),
  asOfSequence: Sequence,
  /** The thread is archived. Older servers leave it out. */
  archived: Schema.optionalKey(Schema.Boolean),
}).annotate({ identifier: "VoiceThreadState" });
export type VoiceThreadState = typeof VoiceThreadState.Type;

/** Every event but `done` comes from one stored event, and only those move Python's cursor. */
export const VoiceEvent = Schema.Union([
  Schema.Struct({
    type: Schema.Literal("run"),
    threadId: ThreadId,
    runId: RunId,
    status: OrchestrationV2RunStatus,
    sequence: Sequence,
  }),
  Schema.Struct({
    type: Schema.Literal("reply"),
    threadId: ThreadId,
    runId: Schema.NullOr(RunId),
    messageId: MessageId,
    text: Schema.String,
    sequence: Sequence,
  }),
  Schema.Struct({
    type: Schema.Literal("needs_you"),
    threadId: ThreadId,
    sequence: Sequence,
    ...VoicePending.fields,
  }),
  Schema.Struct({
    type: Schema.Literal("resolved"),
    threadId: ThreadId,
    requestId: Schema.String,
    status: Schema.Literals(["resolved", "expired", "cancelled"]),
    sequence: Sequence,
  }),
  Schema.Struct({
    type: Schema.Literal("done"),
    threadId: ThreadId,
    runId: RunId,
    status: VoiceTerminalStatus,
    result: VoiceRunResult,
    /** The stored event that triggered the settle read, or the replay's high-water. */
    sequence: Sequence,
    /** The thread's sequence at the settle read. */
    asOfSequence: Sequence,
  }),
  Schema.Struct({ type: Schema.Literal("gone"), threadId: ThreadId, sequence: Sequence }),
]).annotate({ identifier: "VoiceEvent" });

const WatchStarted = Schema.Struct({ threadId: ThreadId });

export const VoiceLaunchResult = Schema.Struct({
  threadId: ThreadId,
  /** The run the launch's first message started, which a watch can anchor on. */
  runId: Schema.NullOr(RunId),
  /** What the thread started with, after the clamp. */
  runtimeMode: RuntimeMode,
  /** The model's display name, for "started on Opus". */
  model: Schema.String,
  /** A fresh launch: its last accepted write. A replay: the thread's sequence now. */
  sequence: Sequence,
  /** True when the request id had already launched. Python reads before it watches. */
  replayed: Schema.Boolean,
}).annotate({ identifier: "VoiceLaunchResult" });
export type VoiceLaunchResult = typeof VoiceLaunchResult.Type;

export const VoiceSendResult = Schema.Struct({
  threadId: ThreadId,
  /** Fixed by the request id, so a retry names the same message. */
  messageId: MessageId,
  /** The run the message started or queued, which a watch can anchor on. */
  runId: RunId,
  /** A voice send never steers: it starts a turn on an idle thread or queues behind the active one. */
  delivery: Schema.Literals(["started", "queued"]),
  /** A fresh send: its accepted write. A replay: the thread's sequence now. */
  sequence: Sequence,
  /** True when the request id had already sent. */
  replayed: Schema.Boolean,
}).annotate({ identifier: "VoiceSendResult" });
export type VoiceSendResult = typeof VoiceSendResult.Type;

export const VoiceInterruptResult = Schema.Union([
  Schema.Struct({
    kind: Schema.Literal("requested"),
    runId: RunId,
    /** The interrupt's accepted write. */
    sequence: Sequence,
  }),
  /** No run was active, or this request id already interrupted one. */
  Schema.Struct({ kind: Schema.Literal("nothing_running") }),
]).annotate({ identifier: "VoiceInterruptResult" });
export type VoiceInterruptResult = typeof VoiceInterruptResult.Type;

export const VoiceRespondResult = Schema.Struct({
  threadId: ThreadId,
  /** The runtime request that was answered. */
  pendingRequestId: Schema.String,
  decision: Schema.Literals(["approve", "deny"]),
  /** A fresh answer: its accepted write. A replay: the thread's sequence now. */
  sequence: Sequence,
  /** True when the request id had already answered. */
  replayed: Schema.Boolean,
}).annotate({ identifier: "VoiceRespondResult" });
export type VoiceRespondResult = typeof VoiceRespondResult.Type;

export const VoiceAnswerResult = Schema.Struct({
  threadId: ThreadId,
  /** The runtime request that was answered. */
  pendingRequestId: Schema.String,
  /** Each question's answer as T3 recorded it, in the request's order. */
  answers: Schema.Array(Schema.Struct({ questionId: Schema.String, answer: Schema.String })),
  /** A fresh answer: its accepted write. A replay: the thread's sequence now. */
  sequence: Sequence,
  /** True when the request id had already answered. */
  replayed: Schema.Boolean,
}).annotate({ identifier: "VoiceAnswerResult" });
export type VoiceAnswerResult = typeof VoiceAnswerResult.Type;

export const VoiceRecordResult = Schema.Struct({
  threadId: ThreadId,
  /** The transcript card on the thread. */
  turnItemId: TurnItemId,
  /** Lines recorded, after the bounds. */
  lines: NonNegativeInt,
  /** Lines or text past the bounds were left out. */
  truncated: Schema.Boolean,
  /** A fresh record: its accepted write. A replay: the thread's sequence now. */
  sequence: Sequence,
  /** True when the request id had already recorded. */
  replayed: Schema.Boolean,
}).annotate({ identifier: "VoiceRecordResult" });
export type VoiceRecordResult = typeof VoiceRecordResult.Type;

/** One line of a call as the voice server kept it: who said it, its text, and for a checked line its act and evidence. */
const VoiceCallLineInput = Schema.Struct({
  speaker: Schema.Literals(["caller", "voice"]),
  text: Schema.String,
  relay: Schema.Boolean,
  interrupted: Schema.optionalKey(Schema.Boolean),
  act: Schema.optionalKey(TrimmedNonEmptyString),
  /** The thread events the checked line rests on. */
  sequences: Schema.optionalKey(Schema.Array(Sequence)),
});

/** Each op, its input, and the value an accepted request answers with. */
export const REQUESTS = {
  list_threads: {
    input: Schema.Struct({
      projectId: Schema.optionalKey(ProjectId),
      limit: Schema.optionalKey(
        Schema.Int.check(Schema.isBetween({ minimum: 1, maximum: LIST_LIMIT })),
      ),
    }),
    output: VoiceThreadList,
  },
  read_thread: { input: Schema.Struct({ threadId: ThreadId }), output: VoiceThreadState },
  watch: {
    input: Schema.Struct({
      threadId: ThreadId,
      afterSequence: Sequence,
      /** The run the voice started or sent to. `done` then reports that run's own outcome. */
      runId: Schema.optionalKey(RunId),
    }),
    output: WatchStarted,
  },
  unwatch: { input: Schema.Struct({ threadId: ThreadId }), output: WatchStarted },
  launch: {
    input: Schema.Struct({
      requestId: VoiceRequestId,
      projectId: ProjectId,
      instruction: TrimmedNonEmptyString,
    }),
    output: VoiceLaunchResult,
  },
  send: {
    input: Schema.Struct({
      requestId: VoiceRequestId,
      threadId: ThreadId,
      text: TrimmedNonEmptyString,
    }),
    output: VoiceSendResult,
  },
  interrupt: {
    input: Schema.Struct({ requestId: VoiceRequestId, threadId: ThreadId }),
    output: VoiceInterruptResult,
  },
  respond: {
    input: Schema.Struct({
      requestId: VoiceRequestId,
      threadId: ThreadId,
      /** The pending approval to answer: `pending.requestId` of a read of the thread. */
      pendingRequestId: Schema.String,
      decision: Schema.Literals(["approve", "deny"]),
    }),
    output: VoiceRespondResult,
  },
  answer_question: {
    input: Schema.Struct({
      requestId: VoiceRequestId,
      threadId: ThreadId,
      /** The pending question to answer: `pending.requestId` of a read of the thread. */
      pendingRequestId: Schema.String,
      /** One per question of the request. `option` names an offered label, `own_words` is the caller's own text. */
      answers: Schema.Array(
        Schema.Struct({
          questionId: Schema.String,
          kind: Schema.Literals(["option", "own_words"]),
          text: TrimmedNonEmptyString,
        }),
      ),
    }),
    output: VoiceAnswerResult,
  },
  record_call: {
    input: Schema.Struct({
      requestId: VoiceRequestId,
      threadId: ThreadId,
      callId: TrimmedNonEmptyString.check(Schema.isMaxLength(128)),
      startedAt: Schema.DateTimeUtcFromString,
      endedAt: Schema.DateTimeUtcFromString,
      /** Past the bounds in VOICE_CALL_LIMITS, the call is recorded cut and marked truncated, never refused. */
      lines: Schema.Array(VoiceCallLineInput),
    }),
    output: VoiceRecordResult,
  },
} as const;

export type WireOp = keyof typeof REQUESTS;
const WIRE_OPS = Object.keys(REQUESTS) as ReadonlyArray<WireOp>;
export type RequestInput<Op extends WireOp> = (typeof REQUESTS)[Op]["input"]["Type"];
export type RequestOutput<Op extends WireOp> = (typeof REQUESTS)[Op]["output"]["Type"];

/** Read first, so a frame with a bad input still gets an answer under its own id. */
export const ClientEnvelope = Schema.Struct({
  id: NonNegativeInt,
  op: Schema.String,
  input: Schema.Unknown,
});

/** What the client sends, per op. The route decodes the envelope, then the op's input. */
const ClientFrame = Schema.Union(
  WIRE_OPS.map((op) =>
    Schema.Struct({ id: NonNegativeInt, op: Schema.Literal(op), input: REQUESTS[op].input }),
  ),
).annotate({ identifier: "ClientFrame" });

export const HelloFrame = Schema.Struct({
  type: Schema.Literal("hello"),
  ceiling: Schema.Struct({
    maxRuntimeMode: RuntimeMode,
    interactionMode: Schema.Literal("default"),
    operations: Schema.Array(VoiceOperation),
    maxWatches: PositiveInt,
  }),
  /** Allowed projects that exist. */
  projects: Schema.Array(VoiceProject),
  latestSequence: Sequence,
  /** The environment this seam serves, so a client can refuse a thread id from another one. */
  environmentId: Schema.optional(EnvironmentId),
}).annotate({ identifier: "HelloFrame" });
export type VoiceHello = typeof HelloFrame.Type;

export const ResultErrorFrame = Schema.Struct({
  type: Schema.Literal("result"),
  id: NonNegativeInt,
  ok: Schema.Literal(false),
  error: Schema.Struct({
    code: RefusalCode,
    /** Short text the voice may speak. Never built from a cause. */
    message: Schema.String,
    /** For "refused" only: T3's recorded rejection, cut to 200 characters. */
    reason: Schema.optionalKey(Schema.String),
    /** For "refused" only: the command whose receipt was read. */
    commandId: Schema.optionalKey(CommandId),
    /** For "refused" after an accepted launch: the thread that exists although its first message was refused. */
    threadId: Schema.optionalKey(ThreadId),
  }),
});

/** An accepted request's answer. */
export const resultFrame = <Op extends WireOp>(op: Op) =>
  Schema.Struct({
    type: Schema.Literal("result"),
    id: NonNegativeInt,
    ok: Schema.Literal(true),
    value: REQUESTS[op].output,
  });

const ResultFrame = Schema.Union([...WIRE_OPS.map(resultFrame), ResultErrorFrame]);

export const EventFrame = Schema.Struct({ type: Schema.Literal("event"), event: VoiceEvent });

export const WatchEndedFrame = Schema.Struct({
  type: Schema.Literal("watch_ended"),
  threadId: ThreadId,
  reason: WatchEndReason,
});

const ServerFrame = Schema.Union([HelloFrame, ResultFrame, EventFrame, WatchEndedFrame]).annotate({
  identifier: "ServerFrame",
});

/** The golden schema Python checks its types against: both directions, and the config file. */
export const seamJsonSchema = (): Record<string, unknown> => {
  const documents = { ClientFrame, ServerFrame, VoiceSeamConfig };
  const defs: Record<string, unknown> = {};
  for (const [name, schema] of Object.entries(documents)) {
    const document = Schema.toJsonSchemaDocument(schema);
    Object.assign(defs, document.definitions);
    defs[name] ??= document.schema;
  }
  return {
    $schema: "https://json-schema.org/draft/2020-12/schema",
    title: "T3 voice seam protocol",
    $defs: Object.fromEntries(Object.entries(defs).toSorted(([a], [b]) => a.localeCompare(b))),
  };
};
