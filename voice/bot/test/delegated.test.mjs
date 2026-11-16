import assert from "node:assert/strict";
import { test } from "node:test";
import { harness, S } from "../fakes.mjs";

const PM = { role: "pm", threadId: "P", parent: null, charter: "pm" };
const CHILD = "thread:delegated-task:command%3Amcp%3Aabc%3Adelegate-task%3A1234";
const child = { role: "worker", threadId: CHILD, parent: "P", charter: "delegated worker", discovered: true };
const lineage = { lineage: { parentThreadId: "P" } };
const cancelItem = (taskId) => ({ type: "dynamic_tool", runId: "P-r3", text: JSON.stringify({ toolName: "mcp__t3-code__task_cancel", input: { taskId }, output: {} }) });

function endedChild(parentItems) {
	const h = harness([PM, child], () => "wake_pm", { backstopSec: 1e6, silenceSec: 30 });
	h.snaps.set("P", { runs: [], turnItems: parentItems });
	h.set("P", {}, 0);
	h.set(CHILD, { ...lineage, status: "running", activeRunId: `${CHILD}-r1` }, 0);
	h.set(CHILD, { status: "interrupted", activeRunId: null }, 5 * S);
	return h;
}

test("a child run the parent cancelled itself wakes nobody and asks no model", async () => {
	const h = endedChild([cancelItem("node:delegated-task:command%3Amcp%3Aabc%3Adelegate-task%3A1234")]);
	await h.run(10 * S, 200 * S);
	assert.equal(h.attempts(), 0, "no decider call");
	assert.equal(h.sent.length, 0, "the parent is not woken");
	assert.ok(h.checkins.some((e) => e.node === CHILD && e.dropped && /cancelled it/.test(e.reason)), "the drop is logged");
});

test("a child run that ended interrupted without a cancel from its parent wakes the parent by rule, with no model call", async () => {
	const h = endedChild([cancelItem("node:delegated-task:someone-else")]);
	await h.run(10 * S, 200 * S);
	assert.equal(h.attempts(), 0, "no decider call");
	assert.equal(h.sent.length, 1, "the parent is woken once");
	assert.equal(h.checkins.find((e) => e.node === CHILD && e.verdict).source, "rule_child_end");
});

test("a delegated worker gets only the fixed rules: no decider calls while quiet, and the stall rule at its budget", async () => {
	const h = harness([PM, child], () => "wake_pm", { backstopSec: 100, silenceSec: 30, roleBudgetMin: { worker: 5 } });
	h.set("P", {}, 0);
	h.set(CHILD, { ...lineage, status: "running", activeRunId: `${CHILD}-r1` }, 0);
	await h.run(10 * S, 400 * S);
	assert.equal(h.attempts(), 0, "no decider call");
	const wakes = h.checkins.filter((e) => e.node === CHILD && e.verdict === "wake_pm" && !e.suppressed);
	assert.deepEqual(wakes.map((e) => [e.source, Date.parse(e.at) / S]), [["rule_stall", 300]], "one wake, from the stall rule, and no backstop");
});

test("the cancel is found in T3's HTTP snapshot shape too, where toolName and input sit on the item with no text", async () => {
	const h = endedChild([{ type: "dynamic_tool", runId: "P-r3", toolName: "mcp__t3-code__task_cancel", input: { taskId: "node:delegated-task:command%3Amcp%3Aabc%3Adelegate-task%3A1234" }, outputOmitted: true }]);
	await h.run(10 * S, 200 * S);
	assert.equal(h.sent.length, 0, "the parent is not woken");
	assert.ok(h.checkins.some((e) => e.node === CHILD && e.dropped && /cancelled it/.test(e.reason)));
});
