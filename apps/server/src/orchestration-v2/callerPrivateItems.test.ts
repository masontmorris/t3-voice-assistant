// @effect-diagnostics nodeBuiltinImport:off - reads the source tree to find every reader of thread items.
import * as NodeFS from "node:fs";
import * as NodePath from "node:path";
import { expect, it } from "vite-plus/test";

/**
 * A voice call (`isCallerPrivateTurnItem`) is never handed to a model, an agent, a guest or another service. Every
 * module here or in a package that reads thread items, by API or by the `turn_items` table, is listed with how it
 * treats one; a new reader or a re-export of a read function fails until reviewed. LIMITS is what it cannot see.
 */
const LIMITS = [
  "a module handed items as an argument by a listed reader, which passes them on",
  "a read reached through a computed property name or a string built at run time",
  "SQL that names the table without `turn_items` in its text",
  "code outside this repository",
  "branding read results so text is unreadable before the rule narrows them would close the first; it would change every reader's signature",
  "a count or cursor taken from a filtered read through an alias, a helper or another module, or outside COUNT_LEAD lines before and COUNT_REACH after the filter",
  "whether a count a module passes on was made without voice calls: that is the read's own rule (getTimelinePage, and the history page and snapshot window asked withoutCallerPrivate)",
] as const;
const APPLIES = "applies isCallerPrivateTurnItem";
const OWNER = "the owner's own client state";
const READERS: Record<string, string> = {
  "apps/server/src/guest/GuestShareService.ts": APPLIES,
  "apps/server/src/mcp/OrchestratorMcpService.ts":
    "reads the timeline page and thread shells, whose rows, positions and counts leave voice calls out",
  "apps/server/src/orchestration-v2/ThreadManagementService.ts": APPLIES,
  "apps/server/src/orchestration-v2/ProjectionStore.ts": APPLIES,
  "packages/provider-core/src/server/handoffBudget.ts": APPLIES,
  "packages/contracts/src/orchestrationV2.ts": APPLIES,
  "apps/server/src/git/GitManager.ts":
    "thread text reaches the model only through historicalMessage",
  "apps/server/src/orchestration-v2/ContextHandoffService.ts":
    "handoff, fork and merge-back history goes through historicalMessage",
  "apps/server/src/orchestration-v2/Orchestrator.ts":
    "decides events; provider history it assembles goes through ContextHandoffService",
  "apps/server/src/orchestration-v2/WireProjection.ts": "shapes items for the owner's own clients",
  "apps/server/src/orchestration-v2/threadHistoryPaging.ts":
    "pages history for the owner's own clients",
  "apps/server/src/orchestration-v2/http.ts":
    "serves the transcript ThreadManagementService already filters",
  "apps/server/src/orchestration-v2/EventSink.ts": "writes events and projections",
  "apps/server/src/orchestration-v2/ThreadFind.ts":
    "the owner's find within a thread they have open",
  "apps/server/src/orchestration-v2/ThreadSearch.ts":
    "searches messages, so a voice call, which is no message, is never a result",
  "apps/server/src/ws.ts": "the owner's authenticated clients",
  "apps/server/src/assets/AssetAccess.ts":
    "serves an item's own attachment files; a voice call has none",
  "apps/server/src/mcpApps/McpAppRequests.ts": "reads only dynamic_tool items",
  "apps/server/src/mcpApps/McpAppModelContext.ts":
    "joins on an item only to check it exists; the context text is the MCP app's own",
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
  "packages/shared/src/orchestrationV2BoundedSnapshot.ts":
    "bounds a snapshot the server already shaped for its reader",
  "packages/shared/src/orchestrationV2DedupedBodies.ts":
    "dedupes wire bodies for the owner's clients",
  "packages/client-runtime/src/handoff.ts": OWNER,
  "packages/client-runtime/src/platform/threadCacheEncoding.ts": OWNER,
  "packages/client-runtime/src/state/contextWindow.ts": OWNER,
  "packages/client-runtime/src/state/itemSupport.ts": OWNER,
  "packages/client-runtime/src/state/models.ts": OWNER,
  "packages/client-runtime/src/state/orchestrationV2Projection.ts": OWNER,
  "packages/client-runtime/src/state/threadDetail.ts": OWNER,
  "packages/client-runtime/src/state/threadExecution.ts": OWNER,
  "packages/client-runtime/src/state/threadHistoryMerge.ts": OWNER,
  "packages/client-runtime/src/state/threadRequests.ts": OWNER,
  "packages/client-runtime/src/state/threads.ts": OWNER,
};

const READS_ITEMS =
  /visibleTurnItems|"turnItems"|getTimelinePage\(|getThreadHistoryPage\(|getTurnItem\(|historicalMessage\(|turn_items/;
/** The schema migrations create and index the table; they read no item text. */
const SCHEMA_ONLY = /^apps\/server\/src\/persistence\/Migrations\//;
const READ_FUNCTIONS =
  "historicalMessage|getThreadRecords|getTimelinePage|getThreadHistoryPage|getTurnItem";
const REEXPORT = new RegExp(
  `export\\s*\\{[^}]*\\b(?:${READ_FUNCTIONS})\\b[^}]*\\}|export\\s+(?:const|let)\\s+\\w+\\s*=\\s*[\\w.]*\\b(?:${READ_FUNCTIONS})\\s*[;,\\n]`,
);
const NOT_SOURCE =
  /\.test\.ts$|\.testkit\.ts$|\/testkit\/|\/testFixtures\.ts$|TestFixtures\.ts$|\/testLayer\.ts$|\/testWriteLayer\.ts$/;
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

const text = (path: string) => NodeFS.readFileSync(NodePath.join(root, path), "utf8");

/** Readers for someone other than the owner, which must ask the shared reads to leave calls out. */
const NOT_OWNER = ["apps/server/src/guest/GuestShareService.ts"];
const COUNT_LEAD = 3;
const COUNT_REACH = 40;
const COUNT_FIELDS =
  "totalItems|itemCount|visibleItemCount|hasMore|hasEarlier|hasMoreHistory|nextCursor|historyCursor|nextPosition|earlierPosition|snapshotSequence";
const FILTERED_READ = /\b(\w+)\.\w+\.filter\(\s*\(?\s*\w+\s*\)?\s*=>\s*!isCallerPrivateTurnItem/g;

/** Where rows of a read are filtered after it while the same read's counts or cursors go on. */
function countsOfFilteredReads(source: string): Array<string> {
  return Array.from(source.matchAll(FILTERED_READ)).flatMap((match) => {
    const read = match[1]!;
    const line = source.slice(0, match.index).split("\n").length - 1;
    const after = source
      .split("\n")
      .slice(Math.max(0, line - COUNT_LEAD), line + COUNT_REACH)
      .join("\n");
    const passedOn = new RegExp(String.raw`\.\.\.${read}\b|\b${read}\.(?:${COUNT_FIELDS})\b`);
    return passedOn.test(after) ? [read] : [];
  });
}

it("every module that reads thread items is reviewed for the caller's private voice calls", () => {
  const packages = NodeFS.readdirSync(NodePath.join(root, "packages"), { withFileTypes: true })
    .filter(
      (entry) =>
        entry.isDirectory() &&
        NodeFS.existsSync(NodePath.join(root, "packages", entry.name, "src")),
    )
    .map((entry) => `packages/${entry.name}/src`);
  const files = ["apps/server/src", ...packages].flatMap(sources);
  expect(
    files.filter((path) => REEXPORT.test(text(path))),
    "a read function re-exported under another name escapes this review",
  ).toEqual([]);
  const readers = files
    .filter((path) => !SCHEMA_ONLY.test(path) && READS_ITEMS.test(text(path)))
    .toSorted();
  expect(
    readers,
    `a new reader of thread items: list it in READERS with how it treats a voice call. Not seen by this scan: ${LIMITS.join("; ")}`,
  ).toEqual(Object.keys(READERS).toSorted());
  for (const [path, treatment] of Object.entries(READERS)) {
    if (treatment !== APPLIES) continue;
    expect(text(path), path).toContain("isCallerPrivateTurnItem");
    expect(
      countsOfFilteredReads(text(path)),
      `${path}: rows filtered after a read leave that read's counts and cursors counting a voice call`,
    ).toEqual([]);
  }
  for (const path of NOT_OWNER) {
    const source = text(path);
    for (const call of source.matchAll(/getThreadSnapshotWindow\([^)]*\)/g)) {
      expect(call[0], path).toContain("withoutCallerPrivate: true");
    }
    for (const call of source.matchAll(/getThreadHistoryPage\([^)]*\)/g)) {
      expect(call[0], path).toMatch(/,\s*true\s*\)$/);
    }
  }
});

it("catches counts passed on from a read filtered after the fact", () => {
  for (const sample of [
    "const rows = timeline.items.filter((row) => !isCallerPrivateTurnItem(row.item));\nreturn { total: timeline.totalItems };",
    "const page = { ...read, items: read.items.filter((row) => !isCallerPrivateTurnItem(row.item)) };",
    "({\n  ...projection,\n  turnItems: projection.turnItems.filter((item) => !isCallerPrivateTurnItem(item)),\n})",
  ]) {
    expect(countsOfFilteredReads(sample), sample).not.toEqual([]);
  }
  expect(
    countsOfFilteredReads(
      "const rows = timeline.items.filter((row) => !isCallerPrivateTurnItem(row.item));\nreturn rows.length;",
    ),
  ).toEqual([]);
});

it("catches a re-export of a read function under another name", () => {
  for (const sample of [
    "export { historicalMessage as toHistory } from './handoffBudget';",
    "export { getTimelinePage };",
    "export const readItems = projectionStore.getThreadRecords;\n",
  ]) {
    expect(REEXPORT.test(sample), sample).toBe(true);
  }
  expect(REEXPORT.test("export const historicalMessageCap = 3;")).toBe(false);
});
