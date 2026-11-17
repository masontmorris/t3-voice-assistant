import { assert, it } from "@effect/vitest";
import {
  EnvironmentId,
  EventId,
  type OrchestrationV2DomainEvent,
  type OrchestrationV2RuntimeRequest,
  type OrchestrationV2StoredEvent,
  ProjectId,
  RuntimeRequestId,
  ThreadId,
} from "@t3tools/contracts";
import * as DateTime from "effect/DateTime";
import * as Effect from "effect/Effect";
import * as Layer from "effect/Layer";
import * as Ref from "effect/Ref";
import * as SqlClient from "effect/sql/SqlClient";
import * as Stream from "effect/Stream";

import * as EventSink from "../orchestration-v2/EventSink.ts";
import * as ThreadManagementService from "../orchestration-v2/ThreadManagementService.ts";
import * as OrchestrationEventStore from "../persistence/OrchestrationEventStore.ts";
import type { VoicePending } from "./pending.ts";
import { FIXTURES } from "./testFixtures.ts";
import { checksOnLiveClock, seamLayer, write } from "./testLayer.ts";
import * as VoiceSeam from "./VoiceSeam.ts";
import * as Watch from "./watch.ts";

const PROJECT = ProjectId.make("project:voice");
const OUTSIDE = ProjectId.make("project:outside");
const BUSY = ProjectId.make("project:busy");

const createdOf = (events: ReadonlyArray<OrchestrationV2StoredEvent>) =>
  events.flatMap((stored) => (stored.event.type === "thread.created" ? [stored.event] : []))[0]!;

const ALLOWED = new Set([
  PROJECT,
  BUSY,
  ...Object.values(FIXTURES).map((events) => createdOf(events).payload.projectId),
]);

const provideSeam = Effect.provide(
  VoiceSeam.layer({ allowedProjects: ALLOWED }).pipe(Layer.provideMerge(seamLayer)),
);

const CREATED = createdOf(FIXTURES.plainReply);
const START = DateTime.makeUnsafe("2026-11-01T00:00:00.000Z");

/** A thread with no runs, created `minutes` after START, so a larger number is newer. */
const thread = (input: {
  readonly id: string;
  readonly minutes: number;
  readonly projectId?: ProjectId;
  readonly settled?: boolean;
  readonly lineage?: Partial<(typeof CREATED)["payload"]["lineage"]>;
  readonly delegated?: boolean;
}): OrchestrationV2DomainEvent => {
  const id = ThreadId.make(input.id);
  const at = DateTime.add(START, { minutes: input.minutes });
  return {
    ...CREATED,
    id: EventId.make(`created:${input.id}`),
    threadId: id,
    occurredAt: at,
    payload: {
      ...CREATED.payload,
      id,
      projectId: input.projectId ?? PROJECT,
      title: `thread ${input.id}`,
      lineage: {
        parentThreadId: null,
        relationshipToParent: null,
        rootThreadId: id,
        ...input.lineage,
      },
      ...(input.delegated === true
        ? {
            delegatedFrom: {
              environmentId: EnvironmentId.make("environment:elsewhere"),
              threadId: ThreadId.make("elsewhere"),
              title: "elsewhere",
            },
          }
        : {}),
      createdAt: at,
      updatedAt: at,
      settledOverride: input.settled === true ? "settled" : null,
      settledAt: input.settled === true ? at : null,
    },
  };
};

const listThreads = (input: { readonly projectId?: ProjectId; readonly limit?: number } = {}) =>
  VoiceSeam.VoiceSeam.pipe(Effect.flatMap((seam) => seam.listThreads(input)));

const readThread = (threadId: ThreadId) =>
  VoiceSeam.VoiceSeam.pipe(Effect.flatMap((seam) => seam.readThread({ threadId })));

const refusalOf = (threadId: ThreadId) =>
  readThread(threadId).pipe(
    Effect.flip,
    Effect.map((refusal) => refusal.code),
  );

it.effect(
  "lists only listed, undelegated threads in allowed projects, and reads the rest as not_found",
  () =>
    Effect.gen(function* () {
      const parent = ThreadId.make("parent");
      yield* write([
        thread({ id: "parent", minutes: 1 }),
        thread({
          id: "subagent-child",
          minutes: 2,
          lineage: {
            parentThreadId: parent,
            relationshipToParent: "subagent",
            rootThreadId: parent,
          },
        }),
        thread({ id: "delegated", minutes: 3, delegated: true }),
        thread({
          id: "side-chat",
          minutes: 4,
          lineage: { parentThreadId: parent, rootThreadId: parent, sideChat: true },
        }),
        thread({ id: "outside", minutes: 5, projectId: OUTSIDE }),
      ]);

      const list = yield* listThreads();

      assert.deepStrictEqual(
        list.threads.map((summary) => summary.threadId),
        [parent],
      );
      assert.deepInclude(list, { total: 1, truncated: false });
      assert.deepStrictEqual(list.threads[0], {
        threadId: parent,
        projectId: PROJECT,
        title: "thread parent",
        activity: "idle",
        lastActivityAt: "2026-11-01T00:01:00.000Z",
        pinned: false,
      });
      assert.deepStrictEqual(
        yield* Effect.forEach(
          ["subagent-child", "delegated", "side-chat", "outside", "never-created"],
          (id) => refusalOf(ThreadId.make(id)),
        ),
        ["not_found", "not_found", "not_found", "not_found", "not_found"],
      );
      assert.deepInclude(yield* readThread(parent), {
        activity: "idle",
        run: null,
        pending: null,
        lastReply: null,
      });
    }).pipe(provideSeam),
);

it.effect("lists the 26th-newest settled thread and every older one", () =>
  Effect.gen(function* () {
    yield* write(
      Array.from({ length: 30 }, (_, index) =>
        thread({ id: `settled-${index}`, minutes: index, settled: true }),
      ),
    );

    const list = yield* listThreads();

    assert.deepStrictEqual(
      list.threads.map((summary) => summary.threadId),
      Array.from({ length: 30 }, (_, index) => ThreadId.make(`settled-${29 - index}`)),
    );
    assert.deepInclude(list, { total: 30, truncated: false });
  }).pipe(provideSeam),
);

it.effect("cuts the newest-first list at its limit and at 200, and counts what it cut", () =>
  Effect.gen(function* () {
    yield* write(
      Array.from({ length: 205 }, (_, index) => thread({ id: `t-${index}`, minutes: index })),
    );
    const newest = (count: number) =>
      Array.from({ length: count }, (_, index) => ThreadId.make(`t-${204 - index}`));
    const ids = (list: VoiceSeam.VoiceThreadList) => list.threads.map((t) => t.threadId);

    const unlimited = yield* listThreads();
    const three = yield* listThreads({ limit: 3 });

    assert.deepStrictEqual(ids(unlimited), newest(200));
    assert.deepInclude(unlimited, { total: 205, truncated: true });
    assert.deepStrictEqual(ids(three), newest(3));
    assert.deepInclude(three, { total: 205, truncated: true });
    assert.strictEqual(
      unlimited.asOfSequence,
      yield* (yield* OrchestrationEventStore.OrchestrationEventStore).latestApplicationSequence,
    );
  }).pipe(provideSeam),
);

it.effect.each(Object.entries(FIXTURES).map(([name, events]) => ({ name, events })))(
  "lists the same activity as it reads for the $name fixture",
  ({ events }) =>
    Effect.gen(function* () {
      yield* write(events.map((stored) => stored.event));

      const list = yield* listThreads();
      const read = yield* Effect.forEach(list.threads, (summary) => readThread(summary.threadId));

      assert.isAbove(list.threads.length, 0);
      assert.deepStrictEqual(
        list.threads.map((summary) => summary.activity),
        read.map((state) => state.activity),
      );
    }).pipe(provideSeam),
);

it.effect("filters by project before the cut, so total and truncated describe that project", () =>
  Effect.gen(function* () {
    yield* write([
      ...Array.from({ length: 205 }, (_, index) =>
        thread({ id: `busy-${index}`, minutes: 100 + index, projectId: BUSY }),
      ),
      ...Array.from({ length: 3 }, (_, index) => thread({ id: `quiet-${index}`, minutes: index })),
    ]);

    const everything = yield* listThreads();
    const quiet = yield* listThreads({ projectId: PROJECT });
    const cut = yield* listThreads({ projectId: PROJECT, limit: 2 });
    const refusal = yield* listThreads({ projectId: OUTSIDE }).pipe(Effect.flip);

    assert.isFalse(everything.threads.some((summary) => summary.projectId === PROJECT));
    assert.deepStrictEqual(
      quiet.threads.map((summary) => summary.threadId),
      [2, 1, 0].map((index) => ThreadId.make(`quiet-${index}`)),
    );
    assert.deepInclude(quiet, { total: 3, truncated: false });
    assert.deepInclude(cut, { total: 3, truncated: true });
    assert.strictEqual(cut.threads.length, 2);
    assert.strictEqual(refusal.code, "project_not_allowed");
  }).pipe(provideSeam),
);

it.effect.each([0, -1, 201, 1.5, Number.NaN])("refuses a list limit of %s", (limit) =>
  Effect.gen(function* () {
    yield* write([thread({ id: "only", minutes: 1 })]);

    const refusal = yield* listThreads({ limit }).pipe(Effect.flip);

    assert.strictEqual(refusal.code, "invalid_request");
  }).pipe(provideSeam),
);

it.effect.each([1, 200])("lists with a limit of %s", (limit) =>
  Effect.gen(function* () {
    yield* write([thread({ id: "only", minutes: 1 })]);

    const list = yield* listThreads({ limit });

    assert.deepInclude(list, { total: 1, truncated: false });
  }).pipe(provideSeam),
);

const APPROVAL = FIXTURES.approval;
const QUESTION = FIXTURES.question;

const requestOf = (events: ReadonlyArray<OrchestrationV2StoredEvent>) =>
  events.flatMap((stored) =>
    stored.event.type === "runtime-request.updated" && stored.event.payload.status === "pending"
      ? [stored.event]
      : [],
  )[0]!;

const promptItemOf = (events: ReadonlyArray<OrchestrationV2StoredEvent>) =>
  events.flatMap((stored) =>
    stored.event.type === "turn-item.updated" &&
    (stored.event.payload.type === "approval_request" ||
      stored.event.payload.type === "user_input_request")
      ? [stored.event]
      : [],
  )[0]!;

/** A fixture up to its pending request, with the request's turn item, or before both. */
const askedBy = (events: ReadonlyArray<OrchestrationV2StoredEvent>, withPrompt: boolean) => {
  const cut = withPrompt
    ? events.findIndex((stored) => stored.event === promptItemOf(events)) + 1
    : events.findIndex((stored) => stored.event === requestOf(events));
  return events.slice(0, cut).map((stored) => stored.event);
};

const askAs = (
  events: ReadonlyArray<OrchestrationV2StoredEvent>,
  change: Pick<OrchestrationV2RuntimeRequest, "kind" | "responseCapability">,
): OrchestrationV2DomainEvent => {
  const request = requestOf(events);
  return {
    ...request,
    id: EventId.make(`${request.id}:as:${change.kind}:${change.responseCapability.type}`),
    payload: { ...request.payload, ...change },
  };
};

/** The question fixture's card, asking two questions. */
const twoQuestions = (): OrchestrationV2DomainEvent => {
  const item = promptItemOf(QUESTION);
  if (item.payload.type !== "user_input_request") throw new Error("fixture has no question card");
  const [first] = item.payload.questions;
  return {
    ...item,
    id: EventId.make("two-questions"),
    payload: {
      ...item.payload,
      questions: [first!, { ...first!, id: "second", question: "And which branch?" }],
    },
  };
};

const live = requestOf(APPROVAL).payload.responseCapability;
const APPROVAL_ID = requestOf(APPROVAL).payload.id;
const QUESTION_ID = requestOf(QUESTION).payload.id;
const approvalPrompt = "printf 'codex app-server approval fixture' > .codex-probe-write-action.txt";

const REQUEST_KINDS: ReadonlyArray<{
  readonly case: string;
  readonly events: ReadonlyArray<OrchestrationV2DomainEvent>;
  readonly pending: VoicePending | null;
}> = [
  ...(["command", "file-read", "file-change", "permission", "mcp-elicitation"] as const).map(
    (kind) => ({
      case: `${kind} with its turn item`,
      events: [...askedBy(APPROVAL, true), askAs(APPROVAL, { kind, responseCapability: live })],
      pending: { requestId: APPROVAL_ID, kind: "approval" as const, prompt: approvalPrompt },
    }),
  ),
  {
    case: "command before its turn item is stored",
    events: [
      ...askedBy(APPROVAL, false),
      askAs(APPROVAL, { kind: "command", responseCapability: live }),
    ],
    pending: {
      requestId: APPROVAL_ID,
      kind: "approval",
      prompt: "It wants your approval to run a command.",
    },
  },
  ...(["live", "message"] as const).map((type) => ({
    case: `user_input answered ${type}`,
    events: [
      ...askedBy(QUESTION, false),
      twoQuestions(),
      askAs(QUESTION, {
        kind: "user_input",
        responseCapability:
          type === "live" ? requestOf(QUESTION).payload.responseCapability : { type },
      }),
    ],
    pending: {
      requestId: QUESTION_ID,
      kind: "question" as const,
      prompt: "Should this fixture prefer strict schemas or UI flexibility? And 1 more.",
    },
  })),
  {
    case: "request that can no longer be resumed",
    events: [
      ...askedBy(APPROVAL, true),
      askAs(APPROVAL, {
        kind: "command",
        responseCapability: { type: "not_resumable", reason: "session ended" },
      }),
    ],
    pending: {
      requestId: APPROVAL_ID,
      kind: "in_app",
      prompt:
        "It's stuck on a request that can't be answered any more. You can stop it in the app.",
    },
  },
  ...(["dynamic_tool_call", "auth_refresh"] as const).map((kind) => ({
    case: `${kind}, which asks no one`,
    events: [...askedBy(APPROVAL, true), askAs(APPROVAL, { kind, responseCapability: live })],
    pending: null,
  })),
];

it.effect.each(REQUEST_KINDS)(
  "reads a pending $case as the design's table says",
  ({ events, pending }) =>
    Effect.gen(function* () {
      yield* write(events);
      const threadId = events[0]!.threadId;

      const state = yield* readThread(threadId);
      const listed = (yield* listThreads()).threads.find((t) => t.threadId === threadId);

      const activity = pending === null ? "working" : "waiting_on_you";
      assert.deepStrictEqual(state.pending, pending);
      assert.strictEqual(state.activity, activity);
      assert.strictEqual(listed?.activity, activity);
      assert.deepInclude(state.run, { status: "running" });
    }).pipe(provideSeam),
);

it.effect("lists and reads an approval hidden behind a newer request that asks no one alike", () =>
  Effect.gen(function* () {
    const approval = askAs(APPROVAL, { kind: "command", responseCapability: live });
    const later = DateTime.add(requestOf(APPROVAL).payload.createdAt, { seconds: 1 });
    const toolCall: OrchestrationV2DomainEvent = {
      ...requestOf(APPROVAL),
      id: EventId.make("newer-dynamic-tool-call"),
      payload: {
        ...requestOf(APPROVAL).payload,
        id: RuntimeRequestId.make("newer-dynamic-tool-call"),
        kind: "dynamic_tool_call",
        createdAt: later,
      },
    };
    yield* write([...askedBy(APPROVAL, true), approval, toolCall]);
    const threadId = approval.threadId;

    const state = yield* readThread(threadId);
    const listed = (yield* listThreads()).threads.find((t) => t.threadId === threadId);

    // The shell picks the newer request, so only the join sees the approval, and the list runs it.
    assert.strictEqual(listed?.activity, "waiting_on_you");
    assert.strictEqual(state.activity, listed?.activity);
    assert.deepStrictEqual(state.pending, {
      requestId: APPROVAL_ID,
      kind: "approval",
      prompt: approvalPrompt,
    });
  }).pipe(provideSeam),
);

const indexOf = (
  events: ReadonlyArray<OrchestrationV2StoredEvent>,
  matches: (event: OrchestrationV2DomainEvent) => boolean,
) => events.findIndex((stored) => matches(stored.event));

/** Each fixture cut where its run has completed and the need still waits, then where it ended. */
const NEEDS_AFTER_RUN = [
  {
    case: "a link card after its completed run",
    events: FIXTURES.linkCard,
    waiting: indexOf(
      FIXTURES.linkCard,
      (event) =>
        event.type === "turn-item.updated" &&
        event.payload.type === "link_request" &&
        event.payload.status !== "waiting",
    ),
    pending: (events: ReadonlyArray<OrchestrationV2StoredEvent>) => ({
      requestId: events.flatMap((stored) =>
        stored.event.type === "turn-item.updated" && stored.event.payload.type === "link_request"
          ? [stored.event.payload.id]
          : [],
      )[0]!,
      kind: "in_app" as const,
      prompt: "It needs something from you in the app.",
    }),
    reply: "Created the requested probe file.",
  },
  {
    case: "a pending message-capability question after its run",
    events: FIXTURES.messageQuestion,
    waiting: indexOf(
      FIXTURES.messageQuestion,
      (event) => event.type === "runtime-request.updated" && event.payload.status === "resolved",
    ),
    pending: (events: ReadonlyArray<OrchestrationV2StoredEvent>) => ({
      requestId: requestOf(events).payload.id,
      kind: "question" as const,
      prompt: "Which fixture turn should run next?",
    }),
    reply: null,
  },
];

it.effect.each(NEEDS_AFTER_RUN)("reads $case as waiting_on_you with a pending", (need) =>
  Effect.gen(function* () {
    const threadId = need.events[0]!.event.threadId;
    yield* write(need.events.slice(0, need.waiting).map((stored) => stored.event));

    const waiting = yield* readThread(threadId);
    const listed = (yield* listThreads()).threads.find((t) => t.threadId === threadId);

    assert.strictEqual(waiting.activity, "waiting_on_you");
    assert.strictEqual(listed?.activity, "waiting_on_you");
    assert.deepStrictEqual(waiting.pending, need.pending(need.events));
    assert.deepInclude(waiting.run, { status: "completed" });
    assert.strictEqual(waiting.lastReply?.text ?? null, need.reply);

    // The need's end: the card is answered, or the question resolved.
    yield* write([need.events[need.waiting]!.event]);
    const answered = yield* readThread(threadId);

    assert.deepInclude(answered, { activity: "finished", pending: null });
    assert.strictEqual(
      answered.lastReply?.text,
      need.reply ?? "Child task completed without an assistant result.",
    );
  }).pipe(provideSeam),
);

const SUBAGENT_ROOT = FIXTURES.subagentAfterRoot;
/** The subagent's end, on its record and on its turn item. */
const isSubagentEnd = ({ event }: OrchestrationV2StoredEvent) =>
  (event.type === "subagent.updated" && event.payload.status === "completed") ||
  (event.type === "turn-item.updated" &&
    event.payload.type === "subagent" &&
    event.payload.status === "completed");
const ROOT_DONE = indexOf(
  SUBAGENT_ROOT,
  (event) => event.type === "run.updated" && event.payload.status === "completed",
);
const FOLLOW_UP_RUN = SUBAGENT_ROOT.flatMap((stored) =>
  stored.event.type === "run.created" && stored.event.payload.status === "queued"
    ? [stored.event.payload.id]
    : [],
)[0]!;
/** The fixture's queued follow-up: its run and what was created with it. */
const isFollowUp = (stored: OrchestrationV2StoredEvent) =>
  JSON.stringify(stored.event.payload).includes(FOLLOW_UP_RUN);

it.effect.each([
  { case: "a completed run", events: FIXTURES.plainReply, activity: "finished" },
  { case: "a failed run", events: FIXTURES.failedRun, activity: "failed" },
  { case: "an interrupted run", events: FIXTURES.interrupted, activity: "interrupted" },
  { case: "a rolled-back run", events: FIXTURES.rolledBack, activity: "rolled_back" },
  {
    case: "a cancelled follow-up behind a starting run",
    events: FIXTURES.queuedCancelled.slice(
      0,
      indexOf(
        FIXTURES.queuedCancelled,
        (event) => event.type === "run.updated" && event.payload.status === "cancelled",
      ) + 1,
    ),
    activity: "working",
  },
  {
    case: "a follow-up queued behind a root run that ended",
    events: SUBAGENT_ROOT.slice(0, ROOT_DONE + 1),
    activity: "queued",
  },
  {
    case: "a root run that ended while its subagent works",
    events: SUBAGENT_ROOT.slice(0, ROOT_DONE + 1).filter(
      (stored) => !isSubagentEnd(stored) && !isFollowUp(stored),
    ),
    activity: "waiting_on_background",
  },
] as const)("reads and lists $case as $activity", ({ events, activity }) =>
  Effect.gen(function* () {
    const threadId = events[0]!.event.threadId;
    yield* write(events.map((stored) => stored.event));

    const state = yield* readThread(threadId);
    const listed = (yield* listThreads()).threads.find((t) => t.threadId === threadId);

    assert.strictEqual(state.activity, activity);
    assert.strictEqual(listed?.activity, activity);
  }).pipe(provideSeam),
);

const PLAIN = FIXTURES.plainReply;
const PLAIN_THREAD = PLAIN[0]!.event.threadId;
const BEFORE_REPLY = indexOf(
  PLAIN,
  (event) => event.type === "message.updated" && event.payload.role === "assistant",
);

/** The production watch from `afterSequence`, run to its first `done`. */
const watchUntilDone = Effect.fn("test.watchUntilDone")(function* (afterSequence: number) {
  const watch = Watch.fromServices({
    sql: yield* SqlClient.SqlClient,
    threads: checksOnLiveClock(yield* ThreadManagementService.ThreadManagementService),
    eventStore: yield* OrchestrationEventStore.OrchestrationEventStore,
    isProjectAllowed: (projectId) => ALLOWED.has(projectId),
  });
  return Array.from(
    yield* watch({ threadId: PLAIN_THREAD, afterSequence }).pipe(
      Stream.takeUntil((item) => item.type === "done" || item.type === "watch_ended"),
      Stream.runCollect,
    ),
  );
});

/** Cursor items name their stored event's sequence; `done` names its trigger. */
const cursorItems = (items: ReadonlyArray<Watch.WatchItem>) =>
  items.flatMap((item) =>
    item.type === "done" || item.type === "watch_ended" ? [] : [`${item.type} ${item.sequence}`],
  );

/** The assistant reply while it still streams. */
const streamingReply = (): OrchestrationV2DomainEvent => {
  const reply = PLAIN[BEFORE_REPLY]!.event;
  if (reply.type !== "message.updated") throw new Error("fixture reply is not a message");
  return {
    ...reply,
    id: EventId.make("streaming-reply"),
    payload: { ...reply.payload, streaming: true, text: "first fixture" },
  };
};

it.effect("a watch from a read's asOfSequence misses no event committed after the read", () =>
  Effect.gen(function* () {
    const before = yield* write([
      ...PLAIN.slice(0, BEFORE_REPLY).map((stored) => stored.event),
      streamingReply(),
    ]);
    const state = yield* readThread(PLAIN_THREAD);
    const after = yield* write(PLAIN.slice(BEFORE_REPLY).map((stored) => stored.event));

    const items = yield* watchUntilDone(state.asOfSequence);

    assert.strictEqual(state.asOfSequence, before.at(-1)!.sequence);
    assert.deepInclude(state, { activity: "working", lastReply: null });
    assert.deepStrictEqual(
      cursorItems(items),
      after.flatMap((stored) => {
        if (stored.event.type === "run.updated") return [`run ${stored.sequence}`];
        if (stored.event.type === "message.updated" && stored.event.payload.role === "assistant") {
          return [`reply ${stored.sequence}`];
        }
        return [];
      }),
    );
    assert.deepInclude(items.at(-1), { type: "done", status: "completed" });
  }).pipe(provideSeam),
);

/** Commits `events` after the read starts and before its first sequence read. */
const commitOnSequenceRead = (events: ReadonlyArray<OrchestrationV2DomainEvent>) =>
  Layer.effect(
    ThreadManagementService.ThreadManagementService,
    Effect.gen(function* () {
      const real = yield* ThreadManagementService.ThreadManagementService;
      const sink = yield* EventSink.EventSinkV2;
      const armed = yield* Ref.make(false);
      return {
        ...real,
        ensureLegacyTranscript: (threadId) =>
          real.ensureLegacyTranscript(threadId).pipe(Effect.andThen(Ref.set(armed, true))),
        getThreadEventSequence: (threadId) =>
          Ref.getAndSet(armed, false).pipe(
            Effect.flatMap((commit) =>
              commit ? sink.write({ events }).pipe(Effect.orDie) : Effect.void,
            ),
            Effect.andThen(real.getThreadEventSequence(threadId)),
          ),
      };
    }),
  );

it.effect("a need that commits as the read starts is in the read or after its asOfSequence", () => {
  const request = requestOf(APPROVAL);
  const cut = indexOf(APPROVAL, (event) => event === request);
  return Effect.gen(function* () {
    yield* write(APPROVAL.slice(0, cut).map((stored) => stored.event));
    const state = yield* readThread(request.threadId);
    const eventStore = yield* OrchestrationEventStore.OrchestrationEventStore;
    const stored = yield* eventStore
      .readAgentEvents({
        threadId: request.threadId,
        afterSequence: 0,
        throughSequence: yield* eventStore.latestAgentSequence(request.threadId),
      })
      .pipe(Stream.runCollect);
    const asked = Array.from(stored).find((event) => event.event.id === request.id)!;

    // A watch from asOfSequence replays every stored event after it, so either covers the need.
    assert.isTrue(
      state.pending?.requestId === request.payload.id || asked.sequence > state.asOfSequence,
    );
  }).pipe(
    Effect.provide(
      VoiceSeam.layer({ allowedProjects: ALLOWED }).pipe(
        Layer.provide(
          commitOnSequenceRead(APPROVAL.slice(cut, cut + 2).map((event) => event.event)),
        ),
        Layer.provideMerge(seamLayer),
      ),
    ),
  );
});
