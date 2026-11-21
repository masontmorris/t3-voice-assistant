import type {
  OrchestrationV2OwnerProjectedTurnItem,
  OrchestrationV2ProjectedTurnItem,
  OrchestrationV2VoiceCall,
} from "@t3tools/contracts";
import * as DateTime from "effect/DateTime";

const startedAt = (call: OrchestrationV2VoiceCall) =>
  DateTime.toEpochMillis(call.startedAt ?? call.updatedAt);

/** The order the server reads calls in: the item they follow, then when they began, then their id. */
function compareVoiceCalls(left: OrchestrationV2VoiceCall, right: OrchestrationV2VoiceCall) {
  return (
    left.ordinal - right.ordinal ||
    startedAt(left) - startedAt(right) ||
    (left.id < right.id ? -1 : left.id > right.id ? 1 : 0)
  );
}

/**
 * The owner's timeline: the thread's rows with each voice call after the item of its own thread it follows. A call
 * that follows no item (ordinal 0) comes first among the thread's own rows. One whose item is not loaded waits for
 * the older page that holds it.
 */
export function withVoiceCallRows(
  rows: ReadonlyArray<OrchestrationV2ProjectedTurnItem>,
  calls: ReadonlyArray<OrchestrationV2VoiceCall> | undefined,
): ReadonlyArray<OrchestrationV2OwnerProjectedTurnItem> {
  if (calls === undefined || calls.length === 0) return rows;
  // Keyed by the row a call goes after; -1 is before every row.
  const after = new Map<number, Array<OrchestrationV2VoiceCall>>();
  for (const call of [...calls].sort(compareVoiceCalls)) {
    const own = (row: OrchestrationV2ProjectedTurnItem) => row.sourceThreadId === call.threadId;
    let anchor: number | undefined;
    if (call.ordinal === 0) {
      const first = rows.findIndex(own);
      anchor = first === -1 ? rows.length - 1 : first - 1;
    } else {
      rows.forEach((row, index) => {
        if (own(row) && row.item.ordinal <= call.ordinal) anchor = index;
      });
    }
    if (anchor === undefined) continue;
    after.set(anchor, [...(after.get(anchor) ?? []), call]);
  }
  if (after.size === 0) return rows;
  const callRows = (index: number) =>
    (after.get(index) ?? []).map((call): OrchestrationV2OwnerProjectedTurnItem => ({
      position: rows[index]?.position ?? 0,
      visibility: "local",
      sourceThreadId: call.threadId,
      sourceItemId: call.id,
      item: call,
    }));
  return [...callRows(-1), ...rows.flatMap((row, index) => [row, ...callRows(index)])];
}
