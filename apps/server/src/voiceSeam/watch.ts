import type {
  MessageId,
  OrchestrationV2StoredEvent,
  OrchestrationV2ThreadShell,
  ProjectId,
  RunId,
  ThreadId,
  TurnItemId,
} from "@t3tools/contracts";
import type * as Cause from "effect/Cause";
import * as Effect from "effect/Effect";
import * as Queue from "effect/Queue";
import * as Result from "effect/Result";
import * as Schema from "effect/Schema";
import type * as SqlClient from "effect/sql/SqlClient";
import * as Stream from "effect/Stream";

import { LiveStreamBufferError } from "../orchestration-v2/LiveStreamBudget.ts";
import {
  isThreadReplayRawPayloadSafe,
  THREAD_RESUME_MAX_REPLAY_EVENTS,
} from "../orchestration-v2/ThreadStream.ts";
import type * as ThreadManagementService from "../orchestration-v2/ThreadManagementService.ts";
import type * as OrchestrationEventStore from "../persistence/OrchestrationEventStore.ts";
import { distill, type VoiceSignal } from "./events.ts";
import { makeSettleCheck } from "./outcome.ts";
import { readNeed, type VoicePending } from "./pending.ts";

export type WatchEndReason = "gone" | "resync" | "not_found" | "unavailable";

export interface WatchEnded {
  readonly type: "watch_ended";
  readonly threadId: ThreadId;
  readonly reason: WatchEndReason;
}

/** The five statuses `isTerminalRunStatus` accepts. */
export type VoiceTerminalStatus =
  | "completed"
  | "failed"
  | "interrupted"
  | "cancelled"
  | "rolled_back";

export interface VoiceRunResult {
  readonly text: string;
  readonly messageId: MessageId | null;
  readonly turnItemId: TurnItemId | null;
}

/** From a settle read, not a stored event, so it never moves Python's cursor. */
export interface WatchDone {
  readonly type: "done";
  readonly threadId: ThreadId;
  readonly runId: RunId;
  readonly status: VoiceTerminalStatus;
  readonly result: VoiceRunResult;
  /** The stored event that triggered the check. */
  readonly sequence: number;
  /** The thread's sequence at the read; every stored event through it has gone out first. */
  readonly asOfSequence: number;
}

/** A need from one stored event that the pending read found still waiting. */
export interface WatchNeedsYou extends VoicePending {
  readonly type: "needs_you";
  readonly threadId: ThreadId;
  readonly sequence: number;
}

/** What the watch sends. A need goes out only as `needs_you`, after the pending read. */
export type WatchItem =
  | Exclude<VoiceSignal, { readonly type: "pending" | "secret_card" }>
  | WatchNeedsYou
  | WatchDone
  | WatchEnded;

export interface SettleTrigger {
  readonly threadId: ThreadId;
  readonly projectId: ProjectId;
  /** The stored event that asked for the check. */
  readonly sequence: number;
  /** The run the watch reports on. `done` carries this run's own outcome, never another run's text. */
  readonly anchorRunId?: RunId;
}

export type SettleAnswer =
  | { readonly kind: "nothing" }
  | { readonly kind: "done"; readonly done: WatchDone }
  /** Run the check again once the watch has emitted every stored event through `atSequence`. */
  | { readonly kind: "catch_up"; readonly atSequence: number }
  /** A wait the watch forks; its answer comes back to the watch fiber under the same rules. */
  | { readonly kind: "follow_up"; readonly wait: Effect.Effect<SettleAnswer> }
  | { readonly kind: "gone" }
  | { readonly kind: "unavailable"; readonly cause: unknown };

export type SettleCheck = (trigger: SettleTrigger) => Effect.Effect<SettleAnswer>;

export interface WatchSources {
  readonly threads: Pick<
    ThreadManagementService.ThreadManagementService["Service"],
    "streamStoredEventsFrom" | "getThreadShell" | "getThreadRecords"
  >;
  readonly eventStore: Pick<
    OrchestrationEventStore.OrchestrationEventStore["Service"],
    "latestAgentSequence" | "prunedThroughSequence" | "getAgentReplayStats" | "readAgentEvents"
  >;
  /** The seam's visibility rule: listed lineage, not delegated, in an allowed project. */
  readonly isVisible: (shell: OrchestrationV2ThreadShell) => boolean;
  readonly settle: SettleCheck;
}

export interface WatchInput {
  readonly threadId: ThreadId;
  readonly afterSequence: number;
  /**
   * The run the voice started or sent to. `done` reports that run's own status and final message,
   * even when T3's result run is another. Without it, `done` reports T3's result run.
   */
  readonly runId?: RunId;
}

type Subscription =
  | { readonly kind: "ended"; readonly reason: WatchEndReason }
  | { readonly kind: "resubscribe"; readonly cursor: number };

type Input =
  | { readonly kind: "events"; readonly events: ReadonlyArray<OrchestrationV2StoredEvent> }
  | {
      readonly kind: "follow_up";
      readonly trigger: SettleTrigger;
      readonly answer: SettleAnswer;
      /** The count of settle triggers scanned when the wait was forked. */
      readonly triggersAtFork: number;
    };

const OUTBOUND_CAPACITY = 16;

const isLiveStreamBufferError = Schema.is(LiveStreamBufferError);

export const make =
  (sources: WatchSources) =>
  (input: WatchInput): Stream.Stream<WatchItem> =>
    Stream.unwrap(
      Effect.gen(function* () {
        const { threadId } = input;
        const out = yield* Queue.bounded<WatchItem, Cause.Done>(OUTBOUND_CAPACITY);
        const followUps = yield* Queue.unbounded<Extract<Input, { kind: "follow_up" }>>();

        // Watch state. Only the watch fiber touches it, and it survives resubscribes.
        const replied = new Set<MessageId>();
        // A later check of a settled thread reads the same answer again.
        const reported = new Set<string>();
        let lastSeen = input.afterSequence;
        let pending: SettleTrigger | null = null;
        // A follow-up's answer describes the thread as of its read. A trigger scanned after the
        // fork means the thread moved on, so the answer is dropped and a fresh check runs.
        let triggersScanned = 0;
        let latestTrigger: SettleTrigger | null = null;
        let catchUp: { readonly atSequence: number; readonly trigger: SettleTrigger } | null = null;
        let followUpInFlight = false;

        const emit = (item: WatchItem) => {
          if (item.type === "reply") {
            if (replied.has(item.messageId)) return Effect.void;
            replied.add(item.messageId);
          }
          if (item.type === "done") {
            const key = `${item.runId}:${item.status}`;
            if (reported.has(key)) return Effect.void;
            reported.add(key);
          }
          return Queue.offer(out, item).pipe(Effect.asVoid);
        };
        const unavailable = (cause: unknown) =>
          Effect.logWarning("voice seam watch is unavailable", { threadId, cause }).pipe(
            Effect.as<Subscription>({ kind: "ended", reason: "unavailable" }),
          );

        /** Applies one answer. Returns the reason it ended the watch, if it did. */
        const apply = Effect.fn("VoiceSeam.watch.applySettle")(function* (
          trigger: SettleTrigger,
          answer: SettleAnswer,
        ) {
          switch (answer.kind) {
            case "nothing":
              return null;
            case "gone":
              return "gone" as const;
            case "unavailable":
              yield* Effect.logWarning("voice seam settle read is unavailable", {
                threadId,
                cause: answer.cause,
              });
              return "unavailable" as const;
            case "done":
              if (lastSeen >= answer.done.asOfSequence) yield* emit(answer.done);
              else catchUp = { atSequence: answer.done.asOfSequence, trigger };
              return null;
            case "catch_up":
              catchUp = { atSequence: answer.atSequence, trigger };
              return null;
            case "follow_up":
              if (followUpInFlight) return null;
              followUpInFlight = true;
              const triggersAtFork = triggersScanned;
              yield* answer.wait.pipe(
                // A wait that dies would leave the watch thinking it still runs.
                Effect.catchCause((cause) =>
                  Effect.succeed<SettleAnswer>({ kind: "unavailable", cause }),
                ),
                Effect.flatMap((next) =>
                  Queue.offer(followUps, {
                    kind: "follow_up",
                    trigger,
                    answer: next,
                    triggersAtFork,
                  }),
                ),
                Effect.forkScoped,
              );
              return null;
          }
        });

        /** Runs the checks that are due, one at a time, on this fiber. Returns the reason one ended the watch. */
        const settleDue = Effect.fn("VoiceSeam.watch.settleDue")(function* () {
          while (true) {
            if (catchUp !== null && lastSeen >= catchUp.atSequence) {
              pending ??= catchUp.trigger;
              catchUp = null;
            }
            if (pending === null) return null;
            const trigger: SettleTrigger = pending;
            pending = null;
            const ended = yield* apply(trigger, yield* sources.settle(trigger));
            if (ended !== null) return ended;
          }
        });

        const arm = (projectId: ProjectId, sequence: number) => {
          triggersScanned += 1;
          latestTrigger = {
            threadId,
            projectId,
            sequence,
            ...(input.runId === undefined ? {} : { anchorRunId: input.runId }),
          };
          pending = latestTrigger;
        };

        /** Emits a batch of stored events and keeps its last trigger. Reports whether it ended the watch. */
        const scan = Effect.fn("VoiceSeam.watch.scan")(function* (
          events: ReadonlyArray<OrchestrationV2StoredEvent>,
          projectId: ProjectId,
        ) {
          for (const stored of events) {
            if (stored.sequence <= lastSeen) continue;
            lastSeen = stored.sequence;
            const distilled = distill(stored);
            if (distilled === null) continue;
            const signal = distilled.signal;
            if (signal?.type === "pending" || signal?.type === "secret_card") {
              const need = yield* readNeed(sources.threads, signal);
              if (need !== null) {
                yield* emit({ ...need, type: "needs_you", threadId, sequence: signal.sequence });
              }
            } else if (signal !== null) {
              yield* emit(signal);
              if (signal.type === "gone") return true;
            }
            if (distilled.settleCheck) arm(projectId, distilled.sequence);
          }
          return false;
        });

        const replay = Effect.fn("VoiceSeam.watch.replay")(function* (cursor: number) {
          const highWater = yield* sources.eventStore.latestAgentSequence(threadId);
          const pruned = (floor: number) => cursor < highWater && cursor < floor;
          if (pruned(yield* sources.eventStore.prunedThroughSequence)) return null;
          if (cursor >= highWater) return { highWater, events: [] };
          const stats = yield* sources.eventStore.getAgentReplayStats({
            threadId,
            afterSequence: cursor,
            throughSequence: highWater,
            maxEvents: THREAD_RESUME_MAX_REPLAY_EVENTS,
          });
          if (
            stats.eventCount > THREAD_RESUME_MAX_REPLAY_EVENTS ||
            !isThreadReplayRawPayloadSafe(stats.rawPayloadBytes)
          ) {
            return null;
          }
          const events = yield* sources.eventStore
            .readAgentEvents({
              threadId,
              afterSequence: cursor,
              throughSequence: highWater,
              skipUnknownEventTypes: true,
            })
            .pipe(Stream.runCollect);
          // Pruning publishes its boundary before it deletes, so a pass during the read shows here.
          if (pruned(yield* sources.eventStore.prunedThroughSequence)) return null;
          return { highWater, events: Array.from(events) };
        });

        const subscribe = Effect.fn("VoiceSeam.watch.subscribe")(function* (
          cursor: number,
          projectId: ProjectId,
        ) {
          const replayed = yield* replay(cursor).pipe(Effect.result);
          if (Result.isFailure(replayed)) return yield* unavailable(replayed.failure);
          if (replayed.success === null) return { kind: "ended", reason: "resync" } as const;
          const { events, highWater } = replayed.success;
          const scanned = yield* scan(events, projectId).pipe(Effect.result);
          if (Result.isFailure(scanned)) return yield* unavailable(scanned.failure);
          if (scanned.success) return { kind: "ended", reason: "gone" } as const;
          lastSeen = Math.max(lastSeen, highWater);
          // One check after every replay, even an empty one: a catch-up armed by an earlier watch
          // died with it, and a thread that settled while no one watched stores nothing more.
          arm(projectId, highWater);
          const settled = yield* settleDue();
          if (settled !== null) return { kind: "ended", reason: settled } as const;

          let ended: WatchEndReason | null = null;
          const live = sources.threads
            .streamStoredEventsFrom({ threadId, afterSequence: lastSeen })
            .pipe(
              Stream.chunks,
              Stream.map((events): Input => ({ kind: "events", events })),
            );
          const handled = yield* Stream.merge(live, Stream.fromQueue(followUps), {
            haltStrategy: "left",
          }).pipe(
            Stream.mapEffect((next) =>
              next.kind === "events"
                ? scan(next.events, projectId).pipe(
                    Effect.flatMap((gone) =>
                      gone ? Effect.succeed<WatchEndReason>("gone") : settleDue(),
                    ),
                  )
                : Effect.gen(function* () {
                    followUpInFlight = false;
                    if (next.triggersAtFork !== triggersScanned) pending ??= latestTrigger;
                    else {
                      const ended = yield* apply(next.trigger, next.answer);
                      if (ended !== null) return ended;
                    }
                    return yield* settleDue();
                  }),
            ),
            Stream.takeUntil((reason) => {
              ended = reason;
              return reason !== null;
            }),
            Stream.runDrain,
            Effect.result,
          );
          if (ended !== null) return { kind: "ended", reason: ended } as const;
          if (Result.isSuccess(handled)) return yield* unavailable("the live event stream ended");
          const error = handled.failure;
          if (
            error._tag === "OrchestratorDomainEventStreamError" &&
            isLiveStreamBufferError(error.cause)
          ) {
            return { kind: "resubscribe", cursor: lastSeen } as const;
          }
          return yield* unavailable(error);
        });

        const end = (reason: WatchEndReason) =>
          Queue.offer(out, { type: "watch_ended", threadId, reason }).pipe(
            Effect.andThen(Queue.end(out)),
          );

        yield* Effect.gen(function* () {
          const shell = yield* sources.threads.getThreadShell(threadId).pipe(Effect.result);
          if (Result.isFailure(shell)) {
            yield* unavailable(shell.failure);
            return yield* end("unavailable");
          }
          // The shell read leaves out a deleted thread, so null covers both missing and deleted.
          if (shell.success === null || !sources.isVisible(shell.success)) {
            return yield* end("not_found");
          }
          const { projectId } = shell.success;
          let cursor = input.afterSequence;
          while (true) {
            const next: Subscription = yield* subscribe(cursor, projectId);
            if (next.kind === "ended") return yield* end(next.reason);
            cursor = next.cursor;
          }
        }).pipe(Effect.forkScoped);

        return Stream.fromQueue(out);
      }),
    );

/** The watch the seam serves: T3's own services, with the settle read as the only source of `done`. */
export const fromServices = (services: {
  readonly sql: SqlClient.SqlClient;
  readonly threads: ThreadManagementService.ThreadManagementService["Service"];
  readonly eventStore: OrchestrationEventStore.OrchestrationEventStore["Service"];
  readonly isVisible: (shell: OrchestrationV2ThreadShell) => boolean;
}) =>
  make({
    threads: services.threads,
    eventStore: services.eventStore,
    isVisible: services.isVisible,
    settle: makeSettleCheck({ sql: services.sql, threads: services.threads }),
  });
