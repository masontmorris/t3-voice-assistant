import type {
  OrchestrationV2ProjectedTurnItem,
  OrchestrationV2Run,
  OrchestrationV2TurnItem,
  OrchestrationV2VoiceCall,
  OrchestrationV2VoiceCallEvidence,
} from "@t3tools/contracts";
import * as DateTime from "effect/DateTime";

export type VoiceCallItem = OrchestrationV2VoiceCall;

/** Lines shown before "show all", so a long call costs the timeline little until it is asked for. */
export const VOICE_CALL_SHOWN_LINES = 40;

export function voiceCallMinutes(item: VoiceCallItem): number {
  if (item.startedAt === null || item.completedAt === null) return 0;
  const ms = DateTime.toEpochMillis(item.completedAt) - DateTime.toEpochMillis(item.startedAt);
  return Math.max(1, Math.round(ms / 60_000));
}

/** The lines to show, each keyed by its place in the call: a recorded call never changes. */
export function shownVoiceCallLines(item: VoiceCallItem, showAll: boolean) {
  const lines = showAll ? item.lines : item.lines.slice(0, VOICE_CALL_SHOWN_LINES);
  return lines.map((line, place) => ({ key: `${item.id}:${place}`, line }));
}

const firstLine = (text: string) => text.trim().split("\n", 1)[0]?.slice(0, 200) ?? "";

function itemSnippet(item: OrchestrationV2TurnItem): string | null {
  return item.type === "assistant_message" || item.type === "user_message"
    ? firstLine(item.text)
    : (item.title ?? null);
}

export type ResolvedEvidence =
  | {
      readonly kind: "item";
      readonly itemType: OrchestrationV2TurnItem["type"];
      readonly text: string | null;
    }
  | { readonly kind: "run"; readonly status: OrchestrationV2Run["status"] }
  | { readonly kind: "thread" }
  | { readonly kind: "missing" };

/**
 * What a checked line's evidence points at among the thread's loaded items. A read snapshot cites the thread as a
 * whole; an item outside the loaded history is missing rather than guessed.
 */
export function resolveVoiceCallEvidence(
  evidence: OrchestrationV2VoiceCallEvidence,
  items: ReadonlyArray<OrchestrationV2ProjectedTurnItem>,
  runs: ReadonlyArray<Pick<OrchestrationV2Run, "id" | "status">>,
): ResolvedEvidence {
  const { ref } = evidence;
  if (ref === null) return { kind: "thread" };
  if (ref.type === "run") {
    const run = runs.find((candidate) => candidate.id === ref.runId);
    return run === undefined ? { kind: "missing" } : { kind: "run", status: run.status };
  }
  const found = items.find(({ item }) =>
    ref.type === "turn_item"
      ? item.id === ref.turnItemId
      : ref.type === "message"
        ? (item.type === "assistant_message" || item.type === "user_message") &&
          item.messageId === ref.messageId
        : "requestId" in item && item.requestId === ref.requestId,
  );
  return found === undefined
    ? { kind: "missing" }
    : { kind: "item", itemType: found.item.type, text: itemSnippet(found.item) };
}
