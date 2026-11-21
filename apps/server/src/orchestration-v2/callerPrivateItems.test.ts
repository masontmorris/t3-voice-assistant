// @effect-diagnostics nodeBuiltinImport:off - reads the source tree to find every reader of thread items.
import * as NodeFS from "node:fs";
import * as NodePath from "node:path";
import { expect, it } from "vite-plus/test";

/**
 * A voice call's transcript (`voice_call`, `isCallerPrivateTurnItem`) must never be handed to a model, an agent, a
 * guest or another service. The type system cannot tell a reader that hands items on from one that only looks up an
 * item it already knows the type of, so every module that reads thread items is listed here with how it treats them.
 * A new reader fails this test until it is reviewed and listed; one listed as applying the rule must still name it.
 */
const APPLIES = "applies isCallerPrivateTurnItem";
const READERS: Record<string, string> = {
  "apps/server/src/guest/GuestShareService.ts": APPLIES,
  "apps/server/src/mcp/OrchestratorMcpService.ts": APPLIES,
  "apps/server/src/orchestration-v2/ThreadManagementService.ts": APPLIES,
  "packages/provider-core/src/server/handoffBudget.ts": APPLIES,
  "apps/server/src/git/GitManager.ts":
    "thread text reaches the model only through historicalMessage",
  "apps/server/src/orchestration-v2/ContextHandoffService.ts":
    "handoff, fork and merge-back history goes through historicalMessage",
  "apps/server/src/orchestration-v2/Orchestrator.ts":
    "decides events; provider history it assembles goes through ContextHandoffService",
  "apps/server/src/orchestration-v2/ProjectionStore.ts":
    "stores and reads projections for the owner",
  "apps/server/src/orchestration-v2/WireProjection.ts": "shapes items for the owner's own clients",
  "apps/server/src/orchestration-v2/threadHistoryPaging.ts":
    "pages history for the owner's own clients",
  "apps/server/src/orchestration-v2/http.ts":
    "serves the transcript ThreadManagementService already filters",
  "apps/server/src/ws.ts": "the owner's authenticated clients",
  "apps/server/src/assets/AssetAccess.ts":
    "serves an item's own attachment files; a voice call has none",
  "apps/server/src/mcpApps/McpAppRequests.ts": "reads only dynamic_tool items",
  "apps/server/src/mcp/toolkits/thread/handlers.ts": "reads only user_input_request items",
  "apps/server/src/orchestration-v2/CheckpointRestoreSafety.ts": "checks run state, not item text",
  "apps/server/src/orchestration-v2/CommandOutputHub.ts": "reads only command_execution items",
  "apps/server/src/orchestration-v2/IdleCompaction.ts": "times compaction; reads no item text",
  "apps/server/src/orchestration-v2/IdleCompactionService.ts":
    "times compaction; reads no item text",
  "apps/server/src/orchestration-v2/ProviderEventIngestor.ts": "updates items a provider reported",
  "apps/server/src/orchestration-v2/ProviderRuntimeRecoveryService.ts":
    "recovers runs; reads no item text",
  "apps/server/src/orchestration-v2/ProviderSessionManager.ts": "reads node and run state",
  "apps/server/src/orchestration-v2/ProviderTurnStartService.ts":
    "reads unsettled work and run_interrupt_request items",
  "apps/server/src/orchestration-v2/RestartContinuation.ts":
    "reads only run_interrupt_request items",
  "apps/server/src/orchestration-v2/contextBoundary.ts": "reads run and message markers",
  "apps/server/src/orchestration-v2/modelSwitch.ts": "reads only user_message items",
  "apps/server/src/peer/PeerLinkRequests.ts": "reads only link_request items",
  "apps/server/src/scheduledTasks/ScheduledTaskService.ts": "reads only error items",
  "apps/server/src/secrets/SecretRequests.ts": "reads only secret_request items",
  "apps/server/src/voiceSeam/answer.ts": "reads only user_input_request items",
  "apps/server/src/voiceSeam/outcome.ts": "reads run settle state and request items",
  "apps/server/src/voiceSeam/pending.ts": "reads only the pending request's items",
  "apps/server/src/voiceSeam/record.ts": "writes and reads back the voice call itself",
  "apps/server/src/voiceSeam/respond.ts": "reads only approval_request items",
  "apps/server/src/voiceSeam/send.ts": "reads run and message state",
  "packages/provider-core/src/server/subagentProjection.ts":
    "reads assistant_message and error items of a child's run",
};

const READS_ITEMS =
  /visibleTurnItems|"turnItems"|getTimelinePage\(|getThreadHistoryPage\(|getTurnItem\(|historicalMessage\(/;
const NOT_SOURCE =
  /\.test\.ts$|\.testkit\.ts$|\/testkit\/|\/testFixtures\.ts$|\/testLayer\.ts$|\/testWriteLayer\.ts$/;
const root = NodePath.resolve(import.meta.dirname, "../../../..");

function sources(directory: string): Array<string> {
  return NodeFS.readdirSync(NodePath.join(root, directory), { withFileTypes: true }).flatMap(
    (entry) => {
      const path = `${directory}/${entry.name}`;
      if (entry.isDirectory()) return entry.name === "node_modules" ? [] : sources(path);
      return entry.name.endsWith(".ts") && !NOT_SOURCE.test(path) ? [path] : [];
    },
  );
}

it("every module that reads thread items is reviewed for the caller's private voice calls", () => {
  const readers = ["apps/server/src", "packages/provider-core/src"]
    .flatMap(sources)
    .filter((path) => READS_ITEMS.test(NodeFS.readFileSync(NodePath.join(root, path), "utf8")))
    .toSorted();
  expect(
    readers,
    "a new reader of thread items: list it in READERS with how it treats a voice call",
  ).toEqual(Object.keys(READERS).toSorted());
  for (const [path, treatment] of Object.entries(READERS)) {
    if (treatment !== APPLIES) continue;
    expect(NodeFS.readFileSync(NodePath.join(root, path), "utf8"), path).toContain(
      "isCallerPrivateTurnItem",
    );
  }
});
