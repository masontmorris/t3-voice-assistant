import type {
  MessageId,
  OrchestrationV2StoredEvent,
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
import * as Stream from "effect/Stream";

import { LiveStreamBufferError } from "../orchestration-v2/LiveStreamBudget.ts";
import {
  isThreadReplayRawPayloadSafe,
  THREAD_RESUME_MAX_REPLAY_EVENTS,
} from "../orchestration-v2/ThreadStream.ts";
import type * as ThreadManagementService from "../orchestration-v2/ThreadManagementService.ts";
import type * as OrchestrationEventStore from "../persistence/OrchestrationEventStore.ts";
import { distill, type VoiceSignal } from "./events.ts";

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

export type WatchItem = VoiceSignal | WatchDone | WatchEnded;

export interface SettleTrigger {
  readonly threadId: ThreadId;
  readonly projectId: ProjectId;
  /** The stored event that asked for the check. */
  readonly sequence: number;
}

export type SettleAnswer =
  | { readonly kind: "nothing" }
  | { readonly kind: "done"; readonly done: WatchDone }
  /** Run the check again once the watch has emitted every stored event through `atSequence`. */
  | { readonly kind: "catch_up"; readonly atSequence: number }
  /** A wait the watch forks; its answer comes back to the watch fiber under the same rules. */
  | { readonly kind: "follow_up"; readonly wait: Effect.Effect<SettleAnswer> }
  | { readonly kind: "gone" };

export type SettleCheck = (trigger: SettleTrigger) => Effect.Effect<SettleAnswer>;

export interface WatchSources {
  readonly threads: Pick<
    ThreadManagementService.ThreadManagementService["Service"],
    "streamStoredEventsFrom" | "getThreadShell"
  >;
  readonly eventStore: Pick<
    OrchestrationEventStore.OrchestrationEventStore["Service"],
    "latestAgentSequence" | "prunedThroughSequence" | "getAgentReplayStats" | "readAgentEvents"
  >;
  readonly isProjectAllowed: (projectId: ProjectId) => boolean;
  readonly settle: SettleCheck;
}

export interface WatchInput {
  readonly threadId: ThreadId;
  readonly afterSequence: number;
}

type Subscription =
  | { readonly kind: "ended"; readonly reason: WatchEndReason }
  | { readonly kind: "resubscribe"; readonly cursor: number };

type Input =
  | { readonly kind: "events"; readonly events: ReadonlyArray<OrchestrationV2StoredEvent> }
  | { readonly kind: "follow_up"; readonly trigger: SettleTrigger; readonly answer: SettleAnswer };

const OUTBOUND_CAPACITY = 16;

const isLiveStreamBufferError = Schema.is(LiveStreamBufferError);

/** Until the settle read lands, a check answers nothing. */
export const settleNothing: SettleCheck = () => Effect.succeed({ kind: "nothing" });

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
        let lastSeen = input.afterSequence;
        let pending: SettleTrigger | null = null;
        let catchUp: { readonly atSequence: number; readonly trigger: SettleTrigger } | null = null;
        let followUpInFlight = false;

        const emit = (item: WatchItem) => {
          if (item.type === "reply") {
            if (replied.has(item.messageId)) return Effect.void;
            replied.add(item.messageId);
          }
          return Queue.offer(out, item).pipe(Effect.asVoid);
        };
        const unavailable = (cause: unknown) =>
          Effect.logWarning("voice seam watch is unavailable", { threadId, cause }).pipe(
            Effect.as<Subscription>({ kind: "ended", reason: "unavailable" }),
          );

        /** Applies one answer. Reports whether it ended the watch. */
        const apply = Effect.fn("VoiceSeam.watch.applySettle")(function* (
          trigger: SettleTrigger,
          answer: SettleAnswer,
        ) {
          switch (answer.kind) {
            case "nothing":
              return false;
            case "gone":
              return true;
            case "done":
              if (lastSeen >= answer.done.asOfSequence) yield* emit(answer.done);
              else catchUp = { atSequence: answer.done.asOfSequence, trigger };
              return false;
            case "catch_up":
              catchUp = { atSequence: answer.atSequence, trigger };
              return false;
            case "follow_up":
              if (followUpInFlight) return false;
              followUpInFlight = true;
              yield* answer.wait.pipe(
                Effect.flatMap((next) =>
                  Queue.offer(followUps, { kind: "follow_up", trigger, answer: next }),
                ),
                Effect.forkScoped,
              );
              return false;
          }
        });

        /** Runs the checks that are due, one at a time, on this fiber. Reports whether one ended the watch. */
        const settleDue = Effect.fn("VoiceSeam.watch.settleDue")(function* () {
          while (true) {
            if (catchUp !== null && lastSeen >= catchUp.atSequence) {
              pending ??= catchUp.trigger;
              catchUp = null;
            }
            if (pending === null) return false;
            const trigger: SettleTrigger = pending;
            pending = null;
            if (yield* apply(trigger, yield* sources.settle(trigger))) return true;
          }
        });

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
            if (distilled.signal !== null) {
              yield* emit(distilled.signal);
              if (distilled.signal.type === "gone") return true;
            }
            if (distilled.settleCheck)
              pending = { threadId, projectId, sequence: distilled.sequence };
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
          // The replay is one batch, so it runs at most one check, after all of it is emitted.
          const { events, highWater } = replayed.success;
          if (yield* scan(events, projectId)) return { kind: "ended", reason: "gone" } as const;
          lastSeen = Math.max(lastSeen, highWater);
          if (yield* settleDue()) return { kind: "ended", reason: "gone" } as const;

          let gone = false;
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
                    Effect.flatMap((ended) => (ended ? Effect.succeed(true) : settleDue())),
                  )
                : Effect.gen(function* () {
                    followUpInFlight = false;
                    if (yield* apply(next.trigger, next.answer)) return true;
                    return yield* settleDue();
                  }),
            ),
            Stream.takeUntil((ended) => {
              gone = ended;
              return ended;
            }),
            Stream.runDrain,
            Effect.result,
          );
          if (gone) return { kind: "ended", reason: "gone" } as const;
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
          if (shell.success === null || !sources.isProjectAllowed(shell.success.projectId)) {
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
