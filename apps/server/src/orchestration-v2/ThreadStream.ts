import type {
  OrchestrationV2ThreadHistoryPage,
  OrchestrationV2ThreadProjection,
  OrchestrationV2ThreadStreamItem,
  ThreadId,
} from "@t3tools/contracts";

import { omitLocalVisibleTurnItems } from "@t3tools/shared/orchestrationV2BoundedSnapshot";
import { dedupeSnapshotBodies } from "@t3tools/shared/orchestrationV2DedupedBodies";

import {
  buildBoundedThreadProjection,
  type BoundedProjectionResult,
} from "./threadHistoryPaging.ts";
import {
  capProjectionCheckpointFilesForWire,
  projectThreadProjectionForWire,
} from "./WireProjection.ts";

export const THREAD_RESUME_MAX_REPLAY_EVENTS = 128;

export const THREAD_RESUME_MAX_REPLAY_ENCODED_BYTES = 1_048_576;

/** Decoded objects and temporary strings exceed stored JSON size, even before wire slimming. */
export const THREAD_RESUME_MAX_RAW_PAYLOAD_BYTES = 1_048_576;

export function isThreadReplayRawPayloadSafe(rawPayloadBytes: number): boolean {
  return rawPayloadBytes <= THREAD_RESUME_MAX_RAW_PAYLOAD_BYTES;
}

export function threadReplayEncodedBytes(
  items: ReadonlyArray<unknown>,
  limit: number = THREAD_RESUME_MAX_REPLAY_ENCODED_BYTES,
): number {
  let total = 0;
  for (const item of items) {
    const encoded = JSON.stringify(item);
    total += Buffer.byteLength(encoded ?? "", "utf8");
    if (total > limit) break;
  }
  return total;
}

type ThreadSnapshotStreamItem = Extract<
  OrchestrationV2ThreadStreamItem,
  { readonly kind: "snapshot" }
>;

export function boundedSnapshotResponseFields(input: {
  readonly bounded: BoundedProjectionResult;
  readonly compactTurnItems: boolean;
  readonly dedupeBodies: boolean;
  readonly compactIds: boolean;
}) {
  const { bounded } = input;
  const capped = capProjectionCheckpointFilesForWire(bounded.projection);
  // Dedupe keeps shared items one object, which the compaction's identity check needs.
  const projection = input.dedupeBodies ? dedupeSnapshotBodies(capped) : capped;
  const compactProjection = input.compactTurnItems ? omitLocalVisibleTurnItems(projection) : null;
  return {
    projection: compactProjection ?? projection,
    historyCursor: bounded.historyCursor,
    hasMoreHistory: bounded.hasMoreHistory,
    latestLocalTurnOrdinal: bounded.latestLocalTurnOrdinal,
    ...(compactProjection === null ? {} : { turnItemsOmitLocalVisible: true as const }),
    ...(input.compactIds ? { idTable: true as const } : {}),
  };
}

/**
 * The voice calls an owner's snapshot shows: those after its oldest local row, or all of them when it holds the
 * whole thread. Null when no local row is loaded yet, so older pages bring them.
 */
export function snapshotVoiceCallSpan(
  projection: Pick<OrchestrationV2ThreadProjection, "thread" | "visibleTurnItems">,
  hasMoreHistory: boolean | undefined,
): { readonly from: number } | null {
  if (hasMoreHistory !== true) return { from: 0 };
  const oldest = projection.visibleTurnItems.find(
    (row) => row.sourceThreadId === projection.thread.id,
  );
  return oldest === undefined ? null : { from: oldest.item.ordinal };
}

/**
 * The voice calls an owner's history page shows: from its oldest own row, or from the start on the thread's oldest
 * page, up to the row its cursor names, where the newer page's calls begin. Null when it shows none.
 */
export function historyVoiceCallSpan(
  page: Pick<OrchestrationV2ThreadHistoryPage, "items" | "hasMoreHistory">,
  threadId: ThreadId,
  newerOrdinal: number | null,
): { readonly from: number; readonly before?: number } | null {
  const oldest = page.items.find((row) => row.sourceThreadId === threadId);
  if (page.hasMoreHistory && oldest === undefined) return null;
  return {
    from: page.hasMoreHistory && oldest !== undefined ? oldest.item.ordinal : 0,
    ...(newerOrdinal === null ? {} : { before: newerOrdinal }),
  };
}

export function buildBoundedThreadStreamSnapshot(input: {
  readonly snapshotSequence: number;
  readonly projection: OrchestrationV2ThreadProjection;
  readonly compactTurnItems?: boolean;
  readonly dedupeBodies?: boolean;
  readonly compactIds?: boolean;
}): ThreadSnapshotStreamItem {
  const bounded = buildBoundedThreadProjection({
    snapshotSequence: input.snapshotSequence,
    projection: projectThreadProjectionForWire(input.projection),
  });
  return {
    kind: "snapshot",
    snapshotSequence: input.snapshotSequence,
    ...boundedSnapshotResponseFields({
      bounded,
      compactTurnItems: input.compactTurnItems === true,
      dedupeBodies: input.dedupeBodies === true,
      compactIds: input.compactIds === true,
    }),
  };
}

export type ThreadResumePlan =
  | {
      readonly mode: "replay";
      readonly afterSequence: number;
      readonly throughSequence: number;
    }
  | { readonly mode: "snapshot" };

/** Bound event count and encoded bytes; replace excessive replay with one current snapshot. */
export function decideThreadResume(input: {
  readonly afterSequence: number;
  readonly highWater: number;
  readonly replayEventCount: number;
  readonly replayEncodedBytes: number;
}): ThreadResumePlan {
  if (
    input.afterSequence > input.highWater ||
    input.replayEventCount > THREAD_RESUME_MAX_REPLAY_EVENTS ||
    input.replayEncodedBytes > THREAD_RESUME_MAX_REPLAY_ENCODED_BYTES
  ) {
    return { mode: "snapshot" };
  }
  return {
    mode: "replay",
    afterSequence: input.afterSequence,
    throughSequence: input.highWater,
  };
}
