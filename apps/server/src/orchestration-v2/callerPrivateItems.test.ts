// @effect-diagnostics nodeBuiltinImport:off - reads the source tree to find who reaches a voice call.
import * as NodeFS from "node:fs";
import * as NodePath from "node:path";
import { expect, it } from "vite-plus/test";

/**
 * A voice call is no turn item, so no read of a thread's items, counts or positions sees one; that is the type's
 * work. This scan checks the rest: who may read calls, who handles their event, and which server code hands
 * them to a client. NOT_SEEN is what it cannot see.
 */
const NOT_SEEN = [
  "SQL that names the table through a string built at run time",
  "a consumer that forwards every thread event without naming types: the owner's ws stream does, and the voice seam's watch hands its voice server only the events it names",
  "code outside this repository",
] as const;

const READ = /getThreadVoiceCalls|orchestration_v2_projection_voice_calls/;
const READERS: Record<string, string> = {
  "apps/server/src/orchestration-v2/ProjectionStore.ts": "defines the read and projects the table",
  "apps/server/src/orchestration-v2/Orchestrator.ts": "refuses a second record of one card",
  "apps/server/src/orchestration-v2/ThreadManagementService.ts": "passes the read on",
  "apps/server/src/voiceSeam/record.ts": "the voice seam reads back the card it recorded",
  "apps/server/src/ws.ts": "the owner's own thread subscription",
  "apps/server/src/orchestration-v2/http.ts": "the owner's own snapshot and history reads",
  "apps/server/src/persistence/Migrations/103_VoiceCalls.ts": "creates the table",
  "apps/server/scripts/migrate-dev-db.ts": "clears every call from a dev seed",
};

const EVENT = /thread\.voice-call\.recorded/;
const EVENT_HANDLERS: Record<string, string> = {
  "packages/contracts/src/orchestrationV2.ts": "defines it",
  "apps/server/src/orchestration-v2/Orchestrator.ts": "emits it",
  "apps/server/src/orchestration-v2/ProjectionStore.ts":
    "projects it, touches no thread, and leaves it out of a thread's latest sequence",
  "apps/server/src/guest/GuestShareService.ts": "drops it from a guest's stream",
  "apps/server/src/relay/AgentAwarenessRelay.ts": "never relays it",
  "packages/client-runtime/src/state/orchestrationV2Projection.ts": "the owner's client keeps it",
  "apps/server/scripts/migrate-dev-db.ts": "drops it from a dev seed",
};

/** Server code that puts calls in front of a client; the owner's own reads alone. */
const HANDED_OUT = /\bvoiceCalls\s*[:},]/;
const HANDS_OUT: Record<string, string> = {
  "apps/server/src/ws.ts": "the owner's own thread subscription",
  "apps/server/src/orchestration-v2/http.ts": "the owner's own snapshot and history reads",
};

const NOT_SOURCE =
  /\.test\.ts$|\.testkit\.ts$|\/testkit\/|[tT]estFixtures\.ts$|\/test[A-Z]\w*\.ts$/;
const root = NodePath.resolve(import.meta.dirname, "../../../..");

function sources(directory: string): Array<string> {
  if (!NodeFS.existsSync(NodePath.join(root, directory))) return [];
  return NodeFS.readdirSync(NodePath.join(root, directory), { withFileTypes: true }).flatMap(
    (entry) => {
      const path = `${directory}/${entry.name}`;
      if (entry.isDirectory()) return entry.name === "node_modules" ? [] : sources(path);
      return /\.tsx?$/.test(entry.name) && !NOT_SOURCE.test(path) ? [path] : [];
    },
  );
}

const roots = (parent: string) =>
  NodeFS.readdirSync(NodePath.join(root, parent), { withFileTypes: true }).flatMap((entry) =>
    entry.isDirectory() ? [`${parent}/${entry.name}/src`] : [],
  );
const files = [...roots("apps"), ...roots("packages"), "apps/server/scripts"].flatMap(sources);
const text = (path: string) => NodeFS.readFileSync(NodePath.join(root, path), "utf8");
const matching = (pattern: RegExp, within: (path: string) => boolean = () => true) =>
  files.filter((path) => within(path) && pattern.test(text(path))).toSorted();

it("reads voice calls only on the reviewed owner paths", () => {
  expect(
    matching(READ),
    `a new reader of voice calls: list it in READERS with why it may. Not seen: ${NOT_SEEN.join("; ")}`,
  ).toEqual(Object.keys(READERS).toSorted());
});

it("handles a voice call's event only where it was reviewed", () => {
  expect(matching(EVENT), "a new handler of thread.voice-call.recorded").toEqual(
    Object.keys(EVENT_HANDLERS).toSorted(),
  );
});

it("hands voice calls to a client only on the owner's own reads", () => {
  expect(
    matching(HANDED_OUT, (path) => path.startsWith("apps/server/")),
    "server code that sends voiceCalls",
  ).toEqual(Object.keys(HANDS_OUT).toSorted());
});
