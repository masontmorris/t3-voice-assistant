import type {
  OrchestrationV2OwnerProjectedTurnItem,
  OrchestrationV2ProjectedTurnItem,
  OrchestrationV2VoiceCall,
} from "@t3tools/contracts";

/**
 * The owner's timeline: the thread's rows with each voice call after the item it follows. A call that follows no
 * loaded item of its thread waits for the older page that holds that item.
 */
export function withVoiceCallRows(
  rows: ReadonlyArray<OrchestrationV2ProjectedTurnItem>,
  calls: ReadonlyArray<OrchestrationV2VoiceCall> | undefined,
): ReadonlyArray<OrchestrationV2OwnerProjectedTurnItem> {
  if (calls === undefined || calls.length === 0) return rows;
  const after = new Map<number, Array<OrchestrationV2VoiceCall>>();
  for (const call of calls) {
    let anchor = -1;
    rows.forEach((row, index) => {
      if (row.sourceThreadId === call.threadId && row.item.ordinal <= call.ordinal) anchor = index;
    });
    if (anchor === -1) continue;
    after.set(anchor, [...(after.get(anchor) ?? []), call]);
  }
  if (after.size === 0) return rows;
  return rows.flatMap((row, index) => [
    row,
    ...(after.get(index) ?? []).map((call): OrchestrationV2OwnerProjectedTurnItem => ({
      position: row.position,
      visibility: "local",
      sourceThreadId: call.threadId,
      sourceItemId: call.id,
      item: call,
    })),
  ]);
}
