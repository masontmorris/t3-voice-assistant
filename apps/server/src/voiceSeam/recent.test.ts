import { assert, it } from "@effect/vitest";
import { type OrchestrationV2TurnItem, RunId, ThreadId, TurnItemId } from "@t3tools/contracts";
import * as DateTime from "effect/DateTime";

import { recentRecords } from "./recent.ts";

const AT = DateTime.makeUnsafe("2026-11-18T12:00:00.000Z");

const base = (ordinal: number) => ({
  id: TurnItemId.make(`item-${ordinal}`),
  threadId: ThreadId.make("thread:recent"),
  runId: RunId.make("run:recent"),
  nodeId: null,
  providerThreadId: null,
  providerTurnId: null,
  nativeItemRef: null,
  parentItemId: null,
  ordinal,
  title: null,
  status: "completed" as const,
  startedAt: null,
  completedAt: null,
  updatedAt: DateTime.add(AT, { seconds: ordinal }),
});

const errorRecord = (commands: ReadonlyArray<string>, message: string) => {
  const items: OrchestrationV2TurnItem[] = commands.map((input, ordinal) => ({
    ...base(ordinal),
    type: "command_execution",
    input,
    output: "",
    exitCode: 0,
  }));
  items.push({
    ...base(commands.length),
    status: "failed",
    type: "error",
    failure: { class: "provider_error", code: null, retryable: null, message },
  });
  return recentRecords(items, "C:/fixture/workspace")[0];
};

it.each([
  {
    case: "a shorter command run first does not leave a longer one's arguments",
    commands: ["pnpm test", "pnpm test --grep private-fixture"],
    message: "worker disconnected while running pnpm test --grep private-fixture.",
    expected: "worker disconnected while running [command]",
  },
  {
    case: "a short command does not match inside a word",
    commands: ["ls"],
    message: "The provider call fails here",
    expected: "The provider call fails here",
  },
  {
    case: "a multi-line command is cut before its first line is kept",
    commands: ["git commit -m 'private\nsecond line'"],
    message: "hook rejected git commit -m 'private\nsecond line'",
    expected: "hook rejected [command]",
  },
  {
    case: "absolute host paths are scrubbed",
    commands: [],
    message: "fatal: 'C:/Users/someone/.t3/worktrees/proj/abc' already exists in /home/someone/x",
    expected: "fatal: '[path]' already exists in [path]",
  },
  {
    case: "urls and relative words are not paths",
    commands: [],
    message: "fetch https://example.com failed for src/main.ts and/or tests",
    expected: "fetch https://example.com failed for src/main.ts and/or tests",
  },
])("error records: $case", ({ commands, message, expected }) => {
  assert.deepStrictEqual(errorRecord(commands, message), { kind: "error", message: expected });
});
