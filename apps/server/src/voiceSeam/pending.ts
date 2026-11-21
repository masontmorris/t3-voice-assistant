import {
  idleCompactionOf,
  RuntimeRequestId,
  type OrchestrationV2ConversationMessage,
  type OrchestrationV2Run,
  type OrchestrationV2RuntimeRequest,
  type OrchestrationV2TurnItem,
  type RunId,
} from "@t3tools/contracts";
import * as DateTime from "effect/DateTime";
import * as Effect from "effect/Effect";

import type * as ThreadManagementService from "../orchestration-v2/ThreadManagementService.ts";
import {
  pendingKindOf,
  type VoicePendingKind,
  type VoicePendingSource,
  type VoiceSignal,
} from "./events.ts";
import type { VoiceQuestion } from "./protocol.ts";

/**
 * What raised a need: a runtime request kind, a link or secret card, or `card` when only the shell's
 * user-only pick names it.
 */
export type VoiceNeedSource = VoicePendingSource | "secret_request" | "card";

/** A need the voice may relay. The prompt is context for the voice's own words, never read out. */
export interface VoicePending {
  readonly requestId: string;
  /** approval and question can be relayed. in_app means only the app can act on it. */
  readonly kind: VoicePendingKind;
  readonly source: VoiceNeedSource;
  readonly prompt: string;
  /** What a question asks, when the voice can answer it. See `answerableQuestions`. */
  readonly questions?: ReadonlyArray<VoiceQuestion>;
}

/** What a question may carry to be told and answered by voice. Past any of these it is answered in the app. */
export const VOICE_QUESTION_LIMITS = {
  questions: 4,
  options: 6,
  questionChars: 300,
  labelChars: 80,
  /** A description is context for the voice's choice, so it is cut rather than refused. */
  descriptionChars: 160,
  /** The caller's own words as an answer. */
  ownWordsChars: 500,
} as const;

type UserInputQuestion = Extract<
  OrchestrationV2TurnItem,
  { readonly type: "user_input_request" }
>["questions"][number];

/**
 * The questions of a user-input request when the voice can ask and answer all of them well, else undefined. A multi-select
 * question is never answerable: one spoken reply picks one option or says its own words, and a set of options read
 * from speech has no choice to score, so it would be a guess. A question that can be answered with neither an option
 * nor words, a duplicate or empty label, and anything past `VOICE_QUESTION_LIMITS` are the app's too.
 */
export const answerableQuestions = (
  item: OrchestrationV2TurnItem | undefined,
): ReadonlyArray<UserInputQuestion> | undefined => {
  if (item?.type !== "user_input_request") return undefined;
  const { questions } = item;
  const limits = VOICE_QUESTION_LIMITS;
  if (questions.length === 0 || questions.length > limits.questions) return undefined;
  const fits = questions.every((question) => {
    const labels = question.options.map((option) => option.label.trim());
    return (
      question.multiSelect !== true &&
      question.question.length <= limits.questionChars &&
      question.options.length <= limits.options &&
      (question.options.length > 0 || question.allowCustomAnswer !== false) &&
      labels.every((label) => label.length > 0 && label.length <= limits.labelChars) &&
      new Set(labels.map((label) => label.toLowerCase())).size === labels.length
    );
  });
  const ids = new Set(questions.map((question) => question.id));
  return fits && ids.size === questions.length ? questions : undefined;
};

/** The questions as the voice is told them. */
const voiceQuestionsOf = (
  questions: ReadonlyArray<UserInputQuestion>,
): ReadonlyArray<VoiceQuestion> =>
  questions.map((question) => ({
    id: question.id,
    question: question.question.trim(),
    options: question.options.map((option) => ({
      label: option.label.trim(),
      description: option.description.trim().slice(0, VOICE_QUESTION_LIMITS.descriptionChars),
    })),
    ownWords: question.allowCustomAnswer !== false,
  }));

/** A signal that asks Mason for something, before the pending read checks that it still waits. */
export type NeedSignal = Extract<VoiceSignal, { readonly type: "pending" | "secret_card" }>;

const NOT_RESUMABLE_LINE =
  "It's stuck on a request that can't be answered any more. You can stop it in the app.";
const CARD_LINE = "It needs something from you in the app.";

/** The prompt when the request's turn item is not stored yet or carries no text. Context, never speech. */
const FIXED_LINE: Record<OrchestrationV2RuntimeRequest["kind"], string> = {
  command: "It wants your approval to run a command.",
  "file-read": "It wants your approval to read a file.",
  "file-change": "It wants your approval to change a file.",
  permission: "It wants your permission to go on.",
  "mcp-elicitation": "A tool it's using wants your input.",
  user_input: "It has a question for you.",
  dynamic_tool_call: "It's waiting on a tool call.",
  auth_refresh: "It's waiting to sign in again.",
};

const LIVE_RUN_STATUSES: ReadonlySet<OrchestrationV2Run["status"]> = new Set([
  "preparing",
  "starting",
  "running",
  "waiting",
]);

/**
 * `waitsOnUser` and `linkCardStillAsks` from ProjectionStore.ts, copied because both are private
 * there. A secret card asks only while its run is live. A link card asks after its run, until the
 * user writes in the thread again.
 */
const waitsOnUser = (
  item: OrchestrationV2TurnItem,
  liveRunIds: ReadonlySet<RunId>,
  latestUserAuthoredMessageAt: DateTime.Utc | null,
) =>
  (item.type === "link_request" && linkCardStillAsks(item, latestUserAuthoredMessageAt)) ||
  (item.type === "secret_request" && item.runId !== null && liveRunIds.has(item.runId));

const linkCardStillAsks = (
  item: OrchestrationV2TurnItem,
  latestUserAuthoredMessageAt: DateTime.Utc | null,
) =>
  latestUserAuthoredMessageAt === null ||
  !DateTime.isGreaterThan(latestUserAuthoredMessageAt, item.startedAt ?? item.updatedAt);

/**
 * The shell's live runs, by the statuses its `isActivityRunForShell` accepts. The shell also drops
 * T3's own idle-compaction runs first; a compaction never records a secret card.
 */
const liveRunIdsOf = (runs: ReadonlyArray<OrchestrationV2Run>): ReadonlySet<RunId> =>
  new Set(runs.filter((run) => LIVE_RUN_STATUSES.has(run.status)).map((run) => run.id));

/** The newest message a user wrote, leaving out T3's own idle compactions, as the shell reads it. */
const latestUserAuthoredMessageAt = (
  messages: ReadonlyArray<OrchestrationV2ConversationMessage>,
): DateTime.Utc | null =>
  messages
    .filter(
      (message) =>
        message.role === "user" &&
        message.createdBy === "user" &&
        idleCompactionOf(message) === undefined,
    )
    .reduce<DateTime.Utc | null>(
      (latest, message) =>
        latest === null || DateTime.isGreaterThan(message.updatedAt, latest)
          ? message.updatedAt
          : latest,
      null,
    );

const nonEmpty = (text: string | undefined) =>
  text === undefined || text.trim().length === 0 ? undefined : text.trim();

const promptFor = (
  request: OrchestrationV2RuntimeRequest,
  kind: VoicePendingKind,
  item: OrchestrationV2TurnItem | undefined,
) => {
  if (kind === "in_app") return NOT_RESUMABLE_LINE;
  if (item?.type === "user_input_request") {
    const [first, ...rest] = item.questions;
    if (first !== undefined) {
      return rest.length === 0 ? first.question : `${first.question} And ${rest.length} more.`;
    }
  }
  if (item?.type === "approval_request") {
    const text = nonEmpty(item.prompt) ?? nonEmpty(item.reason);
    if (text !== undefined) return text;
  }
  return FIXED_LINE[request.kind];
};

const isPromptOf = (requestId: string) => (item: OrchestrationV2TurnItem) =>
  (item.type === "approval_request" || item.type === "user_input_request") &&
  item.requestId === requestId;

/**
 * Whether T3 stored anything for this request that says what it asks: its prompt or reason, or its question. Without
 * it the prompt is a fixed line about the kind of request, which cannot tell one command from another.
 */
export const requestIsDetailed = (
  request: OrchestrationV2RuntimeRequest,
  turnItems: ReadonlyArray<OrchestrationV2TurnItem>,
): boolean => {
  const item = turnItems.find(isPromptOf(request.id));
  if (item?.type === "user_input_request") return item.questions.length > 0;
  return (
    item?.type === "approval_request" &&
    (nonEmpty(item.prompt) ?? nonEmpty(item.reason)) !== undefined
  );
};

/** A pending request as the voice relays it, with its prompt from the turn item of the same id. */
export const requestNeed = (
  request: OrchestrationV2RuntimeRequest,
  turnItems: ReadonlyArray<OrchestrationV2TurnItem>,
): VoicePending | null => {
  const kind = pendingKindOf(request);
  if (kind === null) return null;
  const item = turnItems.find(isPromptOf(request.id));
  const questions = kind === "question" ? answerableQuestions(item) : undefined;
  return {
    requestId: request.id,
    kind,
    source: request.kind,
    prompt: promptFor(request, kind, item),
    ...(questions === undefined ? {} : { questions: voiceQuestionsOf(questions) }),
  };
};

export const cardNeed = (
  cardId: string,
  source: "link_request" | "secret_request" | "card",
): VoicePending => ({
  requestId: cardId,
  kind: "in_app",
  source,
  prompt: CARD_LINE,
});

export type PendingReadSource = Pick<
  ThreadManagementService.ThreadManagementService["Service"],
  "getThreadRecords"
>;

/**
 * Checks current state for a need a stored event asked. A replay can carry that event long after
 * Mason answered it, so the need counts only if it still waits now.
 */
export const readNeed = Effect.fn("VoiceSeam.readNeed")(function* (
  threads: PendingReadSource,
  signal: NeedSignal,
) {
  const { threadId } = signal;
  if (signal.type === "pending" && signal.source !== "link_request") {
    const records = yield* threads.getThreadRecords(threadId, ["runtimeRequests", "turnItems"], {
      runtimeRequestStatuses: ["pending"],
      turnItemRequestIds: [RuntimeRequestId.make(signal.requestId)],
    });
    const request = records.runtimeRequests.find((candidate) => candidate.id === signal.requestId);
    return request === undefined ? null : requestNeed(request, records.turnItems);
  }
  if (signal.type === "secret_card") {
    const records = yield* threads.getThreadRecords(threadId, ["turnItems", "runs"], {
      turnItemTypes: ["secret_request"],
      turnItemStatuses: ["waiting"],
      runIds: signal.runId === null ? [] : [signal.runId],
    });
    const card = records.turnItems.find((item) => item.id === signal.requestId);
    return card !== undefined && waitsOnUser(card, liveRunIdsOf(records.runs), null)
      ? cardNeed(signal.requestId, "secret_request")
      : null;
  }
  const records = yield* threads.getThreadRecords(threadId, ["turnItems", "messages"], {
    turnItemTypes: ["link_request"],
    turnItemStatuses: ["waiting"],
    messageRoles: ["user"],
  });
  const card = records.turnItems.find((item) => item.id === signal.requestId);
  return card !== undefined &&
    waitsOnUser(card, new Set(), latestUserAuthoredMessageAt(records.messages))
    ? cardNeed(signal.requestId, "link_request")
    : null;
});
