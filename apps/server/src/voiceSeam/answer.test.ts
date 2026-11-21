import { assert, it } from "@effect/vitest";
import {
  type OrchestrationV2RuntimeRequest,
  type OrchestrationV2StoredEvent,
  type OrchestrationV2TurnItem,
  ThreadId,
} from "@t3tools/contracts";
import * as Digest from "effect/crypto/Digest";
import * as Effect from "effect/Effect";
import * as Option from "effect/Option";

import * as NodeCrypto from "@effect/platform-node/NodeCrypto";
import { type AnswerSources, make } from "./answer.ts";
import type { VoiceSeamConfig } from "./ceiling.ts";
import { voiceCommandId } from "./launch.ts";
import { requestNeed } from "./pending.ts";
import { FIXTURES } from "./testFixtures.ts";

const THREAD = ThreadId.make("thread:answer");
const OTHER = ThreadId.make("thread:other");
const REQUEST_ID = "01J0000000000000000000000A";

const pendingRequestOf = (events: ReadonlyArray<OrchestrationV2StoredEvent>) =>
  events.flatMap((stored) =>
    stored.event.type === "runtime-request.updated" && stored.event.payload.status === "pending"
      ? [stored.event.payload]
      : [],
  )[0]!;

const promptOf = (events: ReadonlyArray<OrchestrationV2StoredEvent>) =>
  events.flatMap((stored) =>
    stored.event.type === "turn-item.updated" && stored.event.payload.type === "user_input_request"
      ? [stored.event.payload]
      : [],
  )[0]!;

const QUESTION = pendingRequestOf(FIXTURES.question);
const APPROVAL = pendingRequestOf(FIXTURES.approval);
const FIXTURE_ITEM = promptOf(FIXTURES.question);

type Asked = (typeof FIXTURE_ITEM)["questions"][number];

const option = (label: string, value?: string) => ({
  label,
  description: `${label} in more words`,
  ...(value === undefined ? {} : { value }),
});

const DATABASE: Asked = {
  id: "database",
  header: "Database",
  question: "Which database should it use?",
  options: [option("Postgres", "pg"), option("SQLite")],
  allowCustomAnswer: false,
};
const NAME: Asked = {
  id: "name",
  header: "Name",
  question: "What should the table be called?",
  options: [option("users"), option("accounts")],
};

/** The question's turn item with other questions. */
const itemOf = (questions: ReadonlyArray<Asked>) =>
  ({ ...FIXTURE_ITEM, questions }) as OrchestrationV2TurnItem;

const CONFIG = {
  maxRuntimeMode: "approval-required",
  operations: ["answer"],
  projects: [],
  workspace: {},
  maxWatches: 16,
} as unknown as VoiceSeamConfig;
const SHELL = { runtimeMode: "approval-required", interactionMode: "default" } as const;

interface Rig {
  readonly dispatched: Array<Record<string, unknown>>;
  readonly sources: AnswerSources;
}

type Stored = Record<string, unknown> | undefined;

/**
 * The seam's services, scripted: the thread's pending requests, the ones T3 resolved (with the answers it stored), and
 * the receipt for the voice's command. A dispatch resolves the request with the answers it was sent, unless `records`
 * says T3 stored others, and one that finds the command already recorded answers `replayed` as the orchestrator does.
 */
const rig = (
  pending: ReadonlyArray<OrchestrationV2RuntimeRequest>,
  item: OrchestrationV2TurnItem,
  options: {
    readonly receipt?: { readonly threadId: ThreadId; readonly status: "accepted" | "rejected" };
    readonly resolved?: ReadonlyArray<OrchestrationV2RuntimeRequest>;
    readonly records?: (sent: Stored) => Stored;
    readonly fails?: { readonly _tag: string };
  } = {},
): Effect.Effect<Rig> =>
  Effect.gen(function* () {
    const digest = yield* Digest.Digest;
    const dispatched: Array<Record<string, unknown>> = [];
    const answered = [...(options.resolved ?? [])];
    let recorded = options.receipt !== undefined;
    const receipt = options.receipt;
    const sources = {
      digest,
      threads: {
        getThreadRecords: (
          _thread: ThreadId,
          _kinds: unknown,
          filter: { runtimeRequestStatuses?: string[] },
        ) =>
          Effect.succeed({
            runtimeRequests: filter.runtimeRequestStatuses?.includes("resolved")
              ? answered
              : pending,
            turnItems: [item],
          }),
        getThreadEventSequence: () => Effect.succeed(77),
        dispatch: (command: Record<string, unknown>) => {
          dispatched.push(command);
          if (options.fails !== undefined) return Effect.fail(options.fails);
          const replayed = recorded;
          if (!recorded) {
            recorded = true;
            const sent = command.answers as Record<string, unknown>;
            const stored = options.records === undefined ? sent : options.records(sent);
            answered.push({
              ...pending.find((request) => request.id === command.requestId)!,
              status: "resolved",
              ...(stored === undefined ? {} : { answers: stored }),
            } as OrchestrationV2RuntimeRequest);
          }
          return Effect.succeed({ sequence: 78, replayed });
        },
      },
      receipts: {
        getByCommandId: () =>
          Effect.succeed(
            receipt === undefined
              ? Option.none()
              : Option.some({ ...receipt, error: receipt.status === "rejected" ? "no" : null }),
          ),
      },
    } as unknown as AnswerSources;
    return { dispatched, sources };
  }).pipe(Effect.provide(NodeCrypto.layer));

const resolvedWith = (request: OrchestrationV2RuntimeRequest, answers: Record<string, unknown>) =>
  ({ ...request, status: "resolved", answers }) as OrchestrationV2RuntimeRequest;

const option_ = (questionId: string, text: string) => ({
  questionId,
  kind: "option" as const,
  text,
});
const words = (questionId: string, text: string) => ({
  questionId,
  kind: "own_words" as const,
  text,
});

const input = (
  answers: ReadonlyArray<ReturnType<typeof option_> | ReturnType<typeof words>>,
  pendingRequestId: string = QUESTION.id,
) => ({ requestId: REQUEST_ID, threadId: THREAD, pendingRequestId, answers });

const outcome = <A>(effect: Effect.Effect<A, { readonly code: string }>) =>
  effect.pipe(
    Effect.result,
    Effect.map((result) => (result._tag === "Failure" ? result.failure.code : "answered")),
  );

it.effect("answers a question with the option's value, or its label when it has none", () =>
  Effect.gen(function* () {
    const { dispatched, sources } = yield* rig([QUESTION], itemOf([DATABASE, NAME]));
    const answered = yield* make(sources).answer(
      CONFIG,
      SHELL,
      input([option_("database", "Postgres"), option_("name", "accounts")]),
    );
    assert.deepEqual(answered, {
      threadId: THREAD,
      pendingRequestId: QUESTION.id,
      answers: [
        { questionId: "database", answer: "pg" },
        { questionId: "name", answer: "accounts" },
      ],
      sequence: 78,
      replayed: false,
    });
    assert.lengthOf(dispatched, 1, "every question goes in one command");
    assert.deepInclude(dispatched[0]!, {
      type: "runtime-request.respond",
      threadId: THREAD,
      requestId: QUESTION.id,
      answers: { database: "pg", name: "accounts" },
    });
    const digest = yield* Digest.Digest;
    assert.strictEqual(
      dispatched[0]!.commandId,
      yield* voiceCommandId(digest, "answer", REQUEST_ID),
      "the voice's request id fixes the command",
    );
  }).pipe(Effect.provide(NodeCrypto.layer)),
);

it.effect("sends the caller's own words only to a question that takes them", () =>
  Effect.gen(function* () {
    const open = yield* rig([QUESTION], itemOf([NAME]));
    const answered = yield* make(open.sources).answer(
      CONFIG,
      SHELL,
      input([words("name", "orders")]),
    );
    assert.deepEqual(answered.answers, [{ questionId: "name", answer: "orders" }]);
    assert.deepInclude(open.dispatched[0]!, { answers: { name: "orders" } });

    const closed = yield* rig([QUESTION], itemOf([DATABASE]));
    assert.strictEqual(
      yield* outcome(
        make(closed.sources).answer(CONFIG, SHELL, input([words("database", "Mongo")])),
      ),
      "invalid_request",
    );
    const long = yield* rig([QUESTION], itemOf([NAME]));
    assert.strictEqual(
      yield* outcome(
        make(long.sources).answer(CONFIG, SHELL, input([words("name", "x".repeat(501))])),
      ),
      "invalid_request",
    );
    assert.lengthOf([...closed.dispatched, ...long.dispatched], 0);
  }).pipe(Effect.provide(NodeCrypto.layer)),
);

it.effect.each([
  ["an option that is not offered", [option_("database", "MySQL")]],
  [
    "a question the request does not ask",
    [option_("database", "Postgres"), option_("colour", "red")],
  ],
  ["a question answered twice", [option_("database", "Postgres"), option_("database", "SQLite")]],
  ["an option offered to another question", [option_("database", "users")]],
] as const)("refuses %s, and sends nothing", ([, answers]) =>
  Effect.gen(function* () {
    const { dispatched, sources } = yield* rig([QUESTION], itemOf([DATABASE]));
    assert.strictEqual(
      yield* outcome(make(sources).answer(CONFIG, SHELL, input(answers))),
      "invalid_request",
    );
    assert.lengthOf(dispatched, 0);
  }).pipe(Effect.provide(NodeCrypto.layer)),
);

it.effect("a question left out of a request with two is refused", () =>
  Effect.gen(function* () {
    const { dispatched, sources } = yield* rig([QUESTION], itemOf([DATABASE, NAME]));
    assert.strictEqual(
      yield* outcome(make(sources).answer(CONFIG, SHELL, input([option_("database", "Postgres")]))),
      "invalid_request",
    );
    assert.lengthOf(dispatched, 0);
  }).pipe(Effect.provide(NodeCrypto.layer)),
);

it.effect.each([
  ["a multi-select question", { ...DATABASE, multiSelect: true }],
  [
    "more options than a spoken question takes",
    { ...DATABASE, options: Array.from({ length: 7 }, (_, n) => option(`Option ${n}`)) },
  ],
  ["a question too long to say", { ...DATABASE, question: "Which database? ".repeat(30) }],
  ["a duplicate label", { ...DATABASE, options: [option("Postgres"), option("postgres")] }],
  ["a question with neither options nor words", { ...DATABASE, options: [] }],
] as const)(
  "%s is answered in the app: it is not told to the voice and not answered by it",
  ([, asked]) =>
    Effect.gen(function* () {
      const item = itemOf([asked as Asked]);
      assert.isUndefined(requestNeed(QUESTION, [item])?.questions, "no questions cross");
      assert.strictEqual(
        requestNeed(QUESTION, [item])?.kind,
        "question",
        "the voice still relays it",
      );
      const { dispatched, sources } = yield* rig([QUESTION], item);
      assert.strictEqual(
        yield* outcome(
          make(sources).answer(CONFIG, SHELL, input([option_("database", "Postgres")])),
        ),
        "invalid_request",
      );
      assert.lengthOf(dispatched, 0);
    }).pipe(Effect.provide(NodeCrypto.layer)),
);

it.effect("a request with more questions than a spoken one takes is answered in the app", () =>
  Effect.gen(function* () {
    const many = Array.from({ length: 5 }, (_, n) => ({ ...NAME, id: `q${n}` }));
    const item = itemOf(many);
    assert.isUndefined(requestNeed(QUESTION, [item])?.questions);
    const { dispatched, sources } = yield* rig([QUESTION], item);
    assert.strictEqual(
      yield* outcome(
        make(sources).answer(CONFIG, SHELL, input(many.map((asked) => option_(asked.id, "users")))),
      ),
      "invalid_request",
    );
    assert.lengthOf(dispatched, 0);
  }).pipe(Effect.provide(NodeCrypto.layer)),
);

it("the voice is told each question's options and whether it takes the caller's words", () => {
  const need = requestNeed(QUESTION, [itemOf([DATABASE, NAME])]);
  assert.deepEqual(need?.questions, [
    {
      id: "database",
      question: "Which database should it use?",
      options: [
        { label: "Postgres", description: "Postgres in more words" },
        { label: "SQLite", description: "SQLite in more words" },
      ],
      ownWords: false,
    },
    {
      id: "name",
      question: "What should the table be called?",
      options: [
        { label: "users", description: "users in more words" },
        { label: "accounts", description: "accounts in more words" },
      ],
      ownWords: true,
    },
  ]);
});

it.effect("answers nothing for a request that is no longer pending, or is an approval", () =>
  Effect.gen(function* () {
    const gone = yield* rig([], itemOf([DATABASE]));
    assert.strictEqual(
      yield* outcome(
        make(gone.sources).answer(CONFIG, SHELL, input([option_("database", "Postgres")])),
      ),
      "invalid_request",
    );
    const approval = yield* rig([APPROVAL], itemOf([DATABASE]));
    assert.strictEqual(
      yield* outcome(
        make(approval.sources).answer(
          CONFIG,
          SHELL,
          input([option_("database", "Postgres")], APPROVAL.id),
        ),
      ),
      "invalid_request",
    );
    const stuck = yield* rig(
      [
        {
          ...QUESTION,
          responseCapability: { type: "not_resumable", reason: "session ended" },
        } as OrchestrationV2RuntimeRequest,
      ],
      itemOf([DATABASE]),
    );
    assert.strictEqual(
      yield* outcome(
        make(stuck.sources).answer(CONFIG, SHELL, input([option_("database", "Postgres")])),
      ),
      "invalid_request",
    );
    assert.lengthOf([...gone.dispatched, ...approval.dispatched, ...stuck.dispatched], 0);
  }).pipe(Effect.provide(NodeCrypto.layer)),
);

it.effect(
  "a thread above the ceiling is not answered, and a dispatch the orchestrator refuses for it is not either",
  () =>
    Effect.gen(function* () {
      const above = yield* rig([QUESTION], itemOf([DATABASE]));
      assert.strictEqual(
        yield* outcome(
          make(above.sources).answer(
            CONFIG,
            { runtimeMode: "full-access", interactionMode: "default" },
            input([option_("database", "Postgres")]),
          ),
        ),
        "above_ceiling",
      );
      assert.lengthOf(above.dispatched, 0, "refused before any command");
      const raised = yield* rig([QUESTION], itemOf([DATABASE]), {
        fails: { _tag: "OrchestratorThreadAboveModeLimitError" },
      });
      assert.strictEqual(
        yield* outcome(
          make(raised.sources).answer(CONFIG, SHELL, input([option_("database", "Postgres")])),
        ),
        "above_ceiling",
      );
    }).pipe(Effect.provide(NodeCrypto.layer)),
);

it.effect(
  "a request id that already answered replays what T3 recorded and never answers again",
  () =>
    Effect.gen(function* () {
      const { dispatched, sources } = yield* rig([], itemOf([DATABASE]), {
        receipt: { threadId: THREAD, status: "accepted" },
        resolved: [resolvedWith(QUESTION, { database: "pg" })],
      });
      const answered = yield* make(sources).answer(
        CONFIG,
        SHELL,
        input([option_("database", "Postgres")]),
      );
      assert.deepInclude(answered, { sequence: 77, replayed: true });
      assert.deepEqual(answered.answers, [{ questionId: "database", answer: "pg" }]);
      assert.lengthOf(dispatched, 0);
      // The ceiling gates a new answer, not the report of one T3 already took.
      const above = yield* make(sources).answer(
        CONFIG,
        { runtimeMode: "full-access", interactionMode: "default" },
        input([option_("database", "Postgres")]),
      );
      assert.strictEqual(above.replayed, true);
    }).pipe(Effect.provide(NodeCrypto.layer)),
);

it.effect(
  "a replay with other answers or another request is refused, not reported as accepted",
  () =>
    Effect.gen(function* () {
      const receipt = { threadId: THREAD, status: "accepted" } as const;
      const resolved = [resolvedWith(QUESTION, { database: "pg" })];
      const other = yield* rig([], itemOf([DATABASE]), { receipt, resolved });
      assert.strictEqual(
        yield* outcome(
          make(other.sources).answer(CONFIG, SHELL, input([option_("database", "SQLite")])),
        ),
        "invalid_request",
      );
      const elsewhere = yield* rig([QUESTION], itemOf([DATABASE]), { receipt, resolved });
      assert.strictEqual(
        yield* outcome(
          make(elsewhere.sources).answer(
            CONFIG,
            SHELL,
            input([option_("database", "Postgres")], "another-request"),
          ),
        ),
        "invalid_request",
      );
      assert.lengthOf(
        [...other.dispatched, ...elsewhere.dispatched],
        0,
        "no second command was sent",
      );
    }).pipe(Effect.provide(NodeCrypto.layer)),
);

it.effect(
  "two concurrent copies of one request id answer once, and a copy with other answers is refused",
  () =>
    Effect.gen(function* () {
      const same = yield* rig([QUESTION], itemOf([DATABASE]));
      const copies = yield* Effect.all(
        [
          make(same.sources).answer(CONFIG, SHELL, input([option_("database", "Postgres")])),
          make(same.sources).answer(CONFIG, SHELL, input([option_("database", "Postgres")])),
        ],
        { concurrency: 2 },
      );
      assert.deepEqual(copies.map((answered) => answered.replayed).sort(), [false, true]);
      const split = yield* rig([QUESTION], itemOf([DATABASE]));
      const mixed = yield* Effect.all(
        [
          outcome(
            make(split.sources).answer(CONFIG, SHELL, input([option_("database", "Postgres")])),
          ),
          outcome(
            make(split.sources).answer(CONFIG, SHELL, input([option_("database", "SQLite")])),
          ),
        ],
        { concurrency: 2 },
      );
      assert.deepEqual([...mixed].sort(), ["answered", "invalid_request"]);
    }).pipe(Effect.provide(NodeCrypto.layer)),
);

it.effect("a receipt that belongs to another thread or that T3 rejected is refused", () =>
  Effect.gen(function* () {
    const elsewhere = yield* rig([QUESTION], itemOf([DATABASE]), {
      receipt: { threadId: OTHER, status: "accepted" },
    });
    assert.strictEqual(
      yield* outcome(
        make(elsewhere.sources).answer(CONFIG, SHELL, input([option_("database", "Postgres")])),
      ),
      "invalid_request",
    );
    const rejected = yield* rig([QUESTION], itemOf([DATABASE]), {
      receipt: { threadId: THREAD, status: "rejected" },
    });
    assert.strictEqual(
      yield* outcome(
        make(rejected.sources).answer(CONFIG, SHELL, input([option_("database", "Postgres")])),
      ),
      "refused",
    );
    assert.lengthOf([...elsewhere.dispatched, ...rejected.dispatched], 0);
  }).pipe(Effect.provide(NodeCrypto.layer)),
);

it.effect(
  "what is reported comes from T3's record: an answer it did not record as sent is not reported given",
  () =>
    Effect.gen(function* () {
      const rewritten = yield* rig([QUESTION], itemOf([DATABASE]), {
        records: () => ({ database: "sqlite" }),
      });
      assert.strictEqual(
        yield* outcome(
          make(rewritten.sources).answer(CONFIG, SHELL, input([option_("database", "Postgres")])),
        ),
        "unavailable",
      );
      const missing = yield* rig([QUESTION], itemOf([DATABASE]), { records: () => undefined });
      assert.strictEqual(
        yield* outcome(
          make(missing.sources).answer(CONFIG, SHELL, input([option_("database", "Postgres")])),
        ),
        "unavailable",
      );
    }).pipe(Effect.provide(NodeCrypto.layer)),
);

it.effect("an unreadable thread is unavailable, and nothing is answered", () =>
  Effect.gen(function* () {
    const { dispatched, sources } = yield* rig([QUESTION], itemOf([DATABASE]));
    const broken = {
      ...sources,
      threads: {
        ...sources.threads,
        getThreadRecords: () => Effect.fail({ _tag: "TestDatabaseError" as const }),
      },
    } as unknown as AnswerSources;
    assert.strictEqual(
      yield* outcome(make(broken).answer(CONFIG, SHELL, input([option_("database", "Postgres")]))),
      "unavailable",
    );
    assert.lengthOf(dispatched, 0);
  }).pipe(Effect.provide(NodeCrypto.layer)),
);
