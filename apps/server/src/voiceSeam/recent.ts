import type { OrchestrationV2TurnItem } from "@t3tools/contracts";
import { isHostPath, workspaceRelativeFilePath } from "@t3tools/shared/path";
import * as DateTime from "effect/DateTime";

import {
  RECENT_ERROR_LENGTH,
  RECENT_LIMIT,
  RECENT_PATH_LENGTH,
  RECENT_PATH_LIMIT,
  type VoiceRecentRecord,
} from "./protocol.ts";

export const RECENT_ITEM_TYPES = [
  "command_execution",
  "dynamic_tool",
  "file_search",
  "web_search",
  "file_change",
] as const satisfies ReadonlyArray<OrchestrationV2TurnItem["type"]>;

const toolOutcome = (status: OrchestrationV2TurnItem["status"]) => {
  switch (status) {
    case "completed":
      return "ok";
    case "failed":
    case "cancelled":
    case "interrupted":
      return "failed";
    default:
      return "running";
  }
};

const commandName = (input: string) => {
  const command = input.trim();
  // A compound command's exit status cannot be attributed to its first program.
  if (/[;&|<>()\r\n\x60]/u.test(command)) return "Command";
  return /^(?:(?:\.\/node_modules\/\.bin\/)?vp test|(?:npm|pnpm|yarn|bun)(?: run)? test|(?:npx |pnpm exec )?(?:vitest|jest)|(?:python(?:3)? -m )?pytest|python(?:3)? -m unittest|cargo test|go test|dotnet test)(?=\s|$)/iu.test(
    command,
  )
    ? "Test run"
    : "Command";
};

const relativePath = (file: string, workspaceRoot: string | undefined) => {
  const slashes = file.replaceAll("\\", "/");
  const relative = isHostPath(slashes)
    ? workspaceRelativeFilePath(file, workspaceRoot)?.replaceAll("\\", "/")
    : slashes;
  if (relative === undefined || /^[a-z]:|^[\w+.-]+:\/\//iu.test(relative)) return undefined;
  const segments: string[] = [];
  for (const segment of relative.split("/")) {
    if (segment === "..") {
      if (segments.pop() === undefined) return undefined;
    } else if (segment !== "" && segment !== ".") {
      segments.push(segment);
    }
  }
  return segments.length > 0 ? segments.join("/") : undefined;
};

export const recentRecords = (
  items: ReadonlyArray<OrchestrationV2TurnItem>,
  workspaceRoot: string | undefined,
): ReadonlyArray<VoiceRecentRecord> => {
  const commands = new Set(
    items.flatMap((item) =>
      item.type === "command_execution" && item.input.trim() ? [item.input.trim()] : [],
    ),
  );
  const recent: VoiceRecentRecord[] = [];
  for (const item of items.toSorted(
    (left, right) =>
      DateTime.toEpochMillis(right.updatedAt) - DateTime.toEpochMillis(left.updatedAt) ||
      right.ordinal - left.ordinal,
  )) {
    switch (item.type) {
      case "command_execution":
        recent.push({
          kind: "tool",
          name: commandName(item.input),
          outcome:
            item.outputIndicatesFailure || (item.exitCode !== undefined && item.exitCode !== 0)
              ? "failed"
              : toolOutcome(item.status),
        });
        break;
      case "file_search":
      case "web_search":
      case "dynamic_tool": {
        const name =
          item.type === "file_search"
            ? "File search"
            : item.type === "web_search"
              ? "Web search"
              : item.toolName !== null && /^[\w.:-]{1,64}$/u.test(item.toolName)
                ? item.toolName
                : "Tool";
        recent.push({ kind: "tool", name, outcome: toolOutcome(item.status) });
        break;
      }
      case "file_change": {
        if (item.status !== "completed") break;
        const paths = new Set<string>();
        const files = item.changes?.length
          ? item.changes.map((change) => change.path)
          : [item.fileName];
        for (const file of files) {
          const path = relativePath(file, workspaceRoot);
          if (path !== undefined) paths.add(path);
        }
        if (paths.size > 0) {
          recent.push({
            kind: "files",
            paths: [...paths]
              .filter((path) => path.length <= RECENT_PATH_LENGTH)
              .slice(0, RECENT_PATH_LIMIT),
            count: paths.size,
          });
        }
        break;
      }
      case "error": {
        const redacted = [...commands].reduce(
          (message, command) => message.replaceAll(command, "[command]"),
          item.failure.message,
        );
        const message = redacted.split(/\r?\n/u, 1)[0] ?? "";
        recent.push({ kind: "error", message: message.slice(0, RECENT_ERROR_LENGTH) });
        break;
      }
    }
    if (recent.length === RECENT_LIMIT) break;
  }
  return recent;
};
