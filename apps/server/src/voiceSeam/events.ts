import type {
  MessageId,
  OrchestrationV2RunStatus,
  OrchestrationV2RuntimeRequest,
  OrchestrationV2StoredEvent,
  RunId,
  ThreadId,
} from "@t3tools/contracts";

export type VoicePendingKind = "approval" | "question" | "in_app";

/** What raised a pending signal: a runtime request kind, or an in-app card. */
export type VoicePendingSource =
  | OrchestrationV2RuntimeRequest["kind"]
  | "secret_request"
  | "link_request";

export type VoiceResolvedStatus = "resolved" | "expired" | "cancelled";

/** Each signal comes from exactly one stored event, and `sequence` is that event's global sequence. */
export type VoiceSignal =
  | {
      readonly type: "run";
      readonly threadId: ThreadId;
      readonly runId: RunId;
      readonly status: OrchestrationV2RunStatus;
      readonly sequence: number;
    }
  | {
      readonly type: "reply";
      readonly threadId: ThreadId;
      readonly runId: RunId | null;
      readonly messageId: MessageId;
      readonly text: string;
      readonly sequence: number;
    }
  | {
      readonly type: "pending";
      readonly threadId: ThreadId;
      readonly requestId: string;
      readonly kind: VoicePendingKind;
      readonly source: VoicePendingSource;
      readonly sequence: number;
    }
  | {
      readonly type: "resolved";
      readonly threadId: ThreadId;
      readonly requestId: string;
      readonly status: VoiceResolvedStatus;
      readonly sequence: number;
    }
  | { readonly type: "gone"; readonly threadId: ThreadId; readonly sequence: number };

export interface Distilled {
  readonly sequence: number;
  readonly signal: VoiceSignal | null;
  /** The event can change what a settle read answers. */
  readonly settleCheck: boolean;
}

/** `dynamic_tool_call` and `auth_refresh` do not wait on the user (RestartContinuation.ts). */
const PENDING_KIND: Record<
  OrchestrationV2RuntimeRequest["kind"],
  Exclude<VoicePendingKind, "in_app"> | null
> = {
  command: "approval",
  "file-read": "approval",
  "file-change": "approval",
  permission: "approval",
  "mcp-elicitation": "approval",
  user_input: "question",
  dynamic_tool_call: null,
  auth_refresh: null,
};

const signalOnly = (signal: VoiceSignal): Distilled => ({
  sequence: signal.sequence,
  signal,
  settleCheck: false,
});

/** Stateless, so a replay from any cursor yields the same signals in the same order. */
export function distill(stored: OrchestrationV2StoredEvent): Distilled | null {
  const { sequence, event } = stored;
  const threadId = event.threadId;
  switch (event.type) {
    case "run.updated":
      return {
        sequence,
        settleCheck: true,
        signal: {
          type: "run",
          threadId,
          runId: event.payload.id,
          status: event.payload.status,
          sequence,
        },
      };
    case "subagent.updated":
    case "provider-thread.updated":
      return { sequence, signal: null, settleCheck: true };
    case "message.updated": {
      const message = event.payload;
      if (message.role !== "assistant" || message.streaming) return null;
      return signalOnly({
        type: "reply",
        threadId,
        runId: message.runId,
        messageId: message.id,
        text: message.text,
        sequence,
      });
    }
    case "runtime-request.updated": {
      const request = event.payload;
      if (request.status === "pending") {
        // T3 refuses both an answer and a dismissal for a request that cannot be resumed.
        const kind =
          request.responseCapability.type === "not_resumable"
            ? "in_app"
            : PENDING_KIND[request.kind];
        if (kind === null) return null;
        return signalOnly({
          type: "pending",
          threadId,
          requestId: request.id,
          kind,
          source: request.kind,
          sequence,
        });
      }
      // Any request that ends can be the last thing holding "done" back, even one never relayed.
      return {
        sequence,
        settleCheck: true,
        signal: {
          type: "resolved",
          threadId,
          requestId: request.id,
          status: request.status,
          sequence,
        },
      };
    }
    case "turn-item.updated": {
      const item = event.payload;
      if (item.type !== "secret_request" && item.type !== "link_request") return null;
      // A link card's agent does not wait on it, so the card asks while its item waits, as the shell reads it.
      const asks =
        item.type === "secret_request"
          ? item.secretStatus === "pending"
          : item.status === "waiting";
      if (!asks) return { sequence, signal: null, settleCheck: true };
      return signalOnly({
        type: "pending",
        threadId,
        requestId: item.id,
        kind: "in_app",
        source: item.type,
        sequence,
      });
    }
    case "thread.deleted":
      return signalOnly({ type: "gone", threadId, sequence });
    default:
      return null;
  }
}
