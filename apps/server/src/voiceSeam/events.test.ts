import { assert, it } from "@effect/vitest";
import type { OrchestrationV2StoredEvent } from "@t3tools/contracts";

import {
  describeEvent,
  distillAll,
  FIXTURES,
  needLandings,
  promptOrder,
  type FixtureName,
} from "./testFixtures.ts";

it.each<[FixtureName, string]>([
  ["approval", "request first"],
  ["question", "request first"],
  ["interrupted", "request first"],
  ["linkCard", "request first"],
  ["lateSecretCard", "request first"],
  ["messageQuestion", "turn item first"],
])("records where the %s prompt's turn item lands against its request: %s", (name, expected) => {
  assert.strictEqual(promptOrder(FIXTURES[name]), expected);
});

it.each<[FixtureName, ReadonlyArray<string>]>([
  ["plainReply", []],
  ["approval", ["command (live) asked before the run ends, resolved before the run ends"]],
  ["question", ["user_input (live) asked before the run ends, resolved before the run ends"]],
  ["interrupted", ["file-change (live) asked before the run ends, cancelled before the run ends"]],
  [
    "linkCard",
    [
      "command (live) asked before the run ends, resolved before the run ends",
      "link_request asked before the run ends, completed after the run ends",
    ],
  ],
  [
    "messageQuestion",
    ["user_input (message) asked before the run ends, resolved after the run ends"],
  ],
  [
    "lateSecretCard",
    [
      "command (live) asked before the run ends, resolved before the run ends",
      "secret_request asked after the run ends, open never",
    ],
  ],
])("records where each %s need lands against its run's terminal run.updated", (name, expected) => {
  assert.deepStrictEqual(needLandings(FIXTURES[name]), expected);
});

const SETTLE = "provider-thread.updated => settle";

it.each<[FixtureName, ReadonlyArray<string>]>([
  [
    "plainReply",
    [
      SETTLE,
      "run.updated => run starting +settle",
      SETTLE,
      "run.updated => run running +settle",
      SETTLE,
      'message.updated => reply "first fixture turn complete"',
      SETTLE,
      "run.updated => run waiting +settle",
      SETTLE,
      "run.updated => run completed +settle",
    ],
  ],
  [
    "approval",
    [
      SETTLE,
      "run.updated => run starting +settle",
      SETTLE,
      "run.updated => run running +settle",
      SETTLE,
      "runtime-request.updated => pending approval command",
      "runtime-request.updated => resolved resolved +settle",
      'message.updated => reply "Created the requested probe file."',
      SETTLE,
      "run.updated => run waiting +settle",
      SETTLE,
      "run.updated => run completed +settle",
    ],
  ],
  [
    "question",
    [
      SETTLE,
      "run.updated => run starting +settle",
      SETTLE,
      "run.updated => run running +settle",
      SETTLE,
      "runtime-request.updated => pending question user_input",
      "runtime-request.updated => resolved resolved +settle",
      'message.updated => reply "plan questions fixture complete"',
      SETTLE,
      "run.updated => run waiting +settle",
      SETTLE,
      "run.updated => run completed +settle",
    ],
  ],
  [
    "interrupted",
    [
      SETTLE,
      "run.updated => run starting +settle",
      SETTLE,
      "run.updated => run running +settle",
      SETTLE,
      'message.updated => reply "The project has a single math module. I will add a `clamp` helper beside `sum`.\\n\\n"',
      "runtime-request.updated => pending approval file-change",
      "runtime-request.updated => resolved cancelled +settle",
      SETTLE,
      "run.updated => run interrupted +settle",
      SETTLE,
      "run.updated => run interrupted +settle",
    ],
  ],
  [
    "linkCard",
    [
      SETTLE,
      SETTLE,
      "run.updated => run running +settle",
      SETTLE,
      "runtime-request.updated => pending approval command",
      "turn-item.updated => pending in_app link_request",
      "runtime-request.updated => resolved resolved +settle",
      'message.updated => reply "Created the requested probe file."',
      SETTLE,
      "run.updated => run waiting +settle",
      SETTLE,
      "run.updated => run completed +settle",
      "turn-item.updated => settle",
    ],
  ],
  [
    "messageQuestion",
    [
      SETTLE,
      SETTLE,
      "run.updated => run running +settle",
      "runtime-request.updated => pending question user_input",
      "run.updated => run waiting +settle",
      SETTLE,
      "run.updated => run completed +settle",
      "runtime-request.updated => resolved resolved +settle",
      SETTLE,
      SETTLE,
      "run.updated => run running +settle",
      'message.updated => reply "second fixture turn complete"',
      "run.updated => run waiting +settle",
      SETTLE,
      "run.updated => run completed +settle",
    ],
  ],
  [
    "lateSecretCard",
    [
      SETTLE,
      SETTLE,
      "run.updated => run running +settle",
      SETTLE,
      "runtime-request.updated => pending approval command",
      "runtime-request.updated => resolved resolved +settle",
      'message.updated => reply "Created the requested probe file."',
      SETTLE,
      "run.updated => run waiting +settle",
      SETTLE,
      "run.updated => run completed +settle",
      "turn-item.updated => secret card of run known",
    ],
  ],
])("distills the recorded %s turn", (name, expected) => {
  assert.deepStrictEqual(distillAll(FIXTURES[name]), expected);
});

const first = <A>(found: ReadonlyArray<A>): A => {
  const [value] = found;
  if (value === undefined) throw new Error("a fixture lacks an event this table needs");
  return value;
};

const created = first(
  FIXTURES.approval.flatMap((stored) =>
    stored.event.type === "thread.created" ? [{ ...stored, event: stored.event }] : [],
  ),
);
const request = first(
  FIXTURES.approval.flatMap((stored) =>
    stored.event.type === "runtime-request.updated" ? [{ ...stored, event: stored.event }] : [],
  ),
);
const linkItem = first(
  FIXTURES.linkCard.flatMap((stored) =>
    stored.event.type === "turn-item.updated" && stored.event.payload.type === "link_request"
      ? [{ ...stored, event: stored.event, item: stored.event.payload }]
      : [],
  ),
);
const secretCard = (secretStatus: "pending" | "saved" | "declined" | "cancelled") => ({
  ...linkItem,
  event: {
    ...linkItem.event,
    payload: {
      ...linkItem.item,
      type: "secret_request" as const,
      label: "API key",
      reason: "The agent needs it",
      status: secretStatus === "pending" ? ("waiting" as const) : ("completed" as const),
      secretStatus,
    },
  },
});

it.each<[string, OrchestrationV2StoredEvent, string | null]>([
  [
    "a deleted thread",
    { ...created, event: { ...created.event, type: "thread.deleted" } },
    "thread.deleted => gone",
  ],
  [
    "a tool call the provider answers itself",
    {
      ...request,
      event: { ...request.event, payload: { ...request.event.payload, kind: "dynamic_tool_call" } },
    },
    null,
  ],
  [
    "an expired auth refresh, which can still settle the thread",
    {
      ...request,
      event: {
        ...request.event,
        payload: { ...request.event.payload, kind: "auth_refresh", status: "expired" },
      },
    },
    "runtime-request.updated => resolved expired +settle",
  ],
  [
    "a pending approval that cannot be resumed",
    {
      ...request,
      event: {
        ...request.event,
        payload: {
          ...request.event.payload,
          responseCapability: { type: "not_resumable", reason: "The provider session ended." },
        },
      },
    },
    "runtime-request.updated => pending in_app command",
  ],
  [
    "a pending question that cannot be resumed",
    {
      ...request,
      event: {
        ...request.event,
        payload: {
          ...request.event.payload,
          kind: "user_input",
          responseCapability: { type: "not_resumable", reason: "The provider session ended." },
        },
      },
    },
    "runtime-request.updated => pending in_app user_input",
  ],
  [
    "an expired approval",
    {
      ...request,
      event: { ...request.event, payload: { ...request.event.payload, status: "expired" } },
    },
    "runtime-request.updated => resolved expired +settle",
  ],
  [
    "a waiting secret card, which only the pending read may relay",
    secretCard("pending"),
    "turn-item.updated => secret card of run known",
  ],
  ["a saved secret card", secretCard("saved"), "turn-item.updated => settle"],
  [
    "a link card that stopped waiting",
    {
      ...linkItem,
      event: {
        ...linkItem.event,
        payload: { ...linkItem.item, status: "cancelled", linkStatus: "cancelled" },
      },
    },
    "turn-item.updated => settle",
  ],
])("distills %s", (_name, stored, expected) => {
  assert.strictEqual(describeEvent(stored), expected);
});
