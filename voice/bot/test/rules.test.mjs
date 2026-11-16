import assert from "node:assert/strict";
import { test } from "node:test";
import { harness, S } from "../fakes.mjs";

const PM = { role: "pm", threadId: "P", parent: null, charter: "pm" };
const QUIET = { choice: "stay_quiet", p: { stay_quiet: 0.99, wake_pm: 0.01, tell_mason: 0 } };
const at = (e) => Date.parse(e.at) / S;

test("stall rule: a running node quiet past its charter budget wakes the PM with no model call", async () => {
	const h = harness([PM, { role: "worker", threadId: "W", parent: "P", charter: "worker", quietBudgetMin: 10 }], () => QUIET, { backstopSec: 1e6 });
	h.set("P", {}, 0);
	h.set("W", { status: "running", activeRunId: "W-r1" }, 0);
	await h.run(10 * S, 660 * S);
	const rule = h.checkins.filter((e) => e.source === "rule_stall");
	assert.equal(rule.length, 1, "one stall-rule entry");
	assert.ok(at(rule[0]) >= 600 && at(rule[0]) < 610, "it fires once quiet reaches 10 minutes");
	assert.equal(rule[0].verdict, "wake_pm");
	assert.equal(rule[0].httpStatus, undefined, "the rule asked no model");
	assert.equal(h.sent.length, 1, "the PM is woken once");
});

test("stall rule: with no charter budget, the worker's own estimate sets it at twice the estimate", async () => {
	const h = harness([PM, { role: "worker", threadId: "W", parent: "P", charter: "worker" }], () => QUIET, { backstopSec: 1e6 });
	h.replies.set("W", "Running the suite now. It takes about 10 minutes on this machine.");
	h.set("P", {}, 0);
	h.set("W", { status: "running", activeRunId: "W-r1" }, 0);
	await h.run(10 * S, 1500 * S);
	const rule = h.checkins.find((e) => e.source === "rule_stall");
	assert.ok(rule, "the stall rule fires");
	assert.ok(at(rule) >= 1200 && at(rule) < 1210, "at 20 minutes of quiet");
	assert.match(rule.reason, /estimate/);
});

test("promise rule: a finished run that promises a later report, with nothing running, wakes the PM", async () => {
	const promise = "I started the export in the background and will report back once it finishes.";
	const nodes = [PM, { role: "worker", threadId: "A", parent: "P", charter: "worker a" }, { role: "worker", threadId: "B", parent: "P", charter: "worker b" }];
	const h = harness(nodes, () => QUIET, { backstopSec: 1e6, promiseGraceSec: 60 });
	h.replies.set("A", promise);
	h.replies.set("B", promise);
	h.set("P", {}, 0);
	h.set("A", {}, 0);
	h.set("B", { pendingBackgroundTasks: [{ id: "bg-1" }] }, 0);
	await h.run(10 * S, 200 * S);
	const a = h.checkins.find((e) => e.node === "A" && e.source === "rule_promise");
	assert.ok(a && at(a) >= 60 && at(a) < 70, "A wakes the PM after the grace period");
	assert.ok(!h.checkins.some((e) => e.node === "B" && e.source === "rule_promise"), "B has a background task that can still report, so the rule holds off");
});

test("question rule: a question open past the timeout wakes the PM with no model call", async () => {
	const h = harness([PM, { role: "worker", threadId: "W", parent: "P", charter: "worker" }], () => QUIET, { backstopSec: 1e6, questionTimeoutSec: 120 });
	h.set("P", {}, 0);
	h.set("W", { status: "running", activeRunId: "W-r1", pendingRuntimeRequest: { id: "q1", createdAt: new Date(0).toISOString() } }, 0);
	await h.run(10 * S, 200 * S);
	const rule = h.checkins.find((e) => e.source === "rule_question");
	assert.ok(rule && at(rule) >= 120 && at(rule) < 130, "the rule fires at the timeout");
	assert.equal(rule.verdict, "wake_pm");
	assert.equal(h.sent.length, 1);
});

test("rule 1 narrowed: a failed run of a T3 child wakes its parent by rule, and only a completed one is dropped", async () => {
	const nodes = [PM, { role: "worker", threadId: "F", parent: "P", charter: "failed child" }, { role: "worker", threadId: "C", parent: "P", charter: "completed child" }];
	const h = harness(nodes, () => QUIET, { backstopSec: 1e6 });
	const child = { lineage: { parentThreadId: "P" } };
	h.set("P", {}, 0);
	h.set("F", { ...child, status: "running", activeRunId: "F-r1" }, 0);
	h.set("C", { ...child, status: "running", activeRunId: "C-r1" }, 0);
	h.set("F", { status: "failed", activeRunId: null }, 5 * S);
	h.set("C", { status: "completed", activeRunId: null }, 5 * S);
	await h.run(10 * S, 10 * S);
	assert.ok(h.checkins.some((e) => e.node === "F" && e.trigger === "change" && e.source === "rule_child_end"), "the failed child run wakes the parent by rule");
	assert.equal(h.attempts(), 0, "with no decider call");
	assert.ok(h.checkins.some((e) => e.node === "C" && e.dropped && /rule 1/.test(e.reason)), "the completed child run is dropped");
});
