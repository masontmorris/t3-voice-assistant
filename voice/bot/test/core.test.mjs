import assert from "node:assert/strict";
import { test } from "node:test";
import { createHeartbeat } from "../core.mjs";
import { lastAssistantText, runOf } from "../t3.mjs";

const S = 1000;
const BASE = { silenceSec: 30, backstopSec: 100, ceilingSec: 500, forceVerdict: null };

// A fake T3 and decider. `decide(body)` returns a verdict string, or throws to simulate a decider error.
function harness(nodes, decide, cfg = {}) {
	const checkins = [];
	const sent = [];
	const snaps = new Map();
	const threads = new Map();
	let attempts = 0;
	const t3 = {
		async send(t, text) {
			const id = `m${sent.length + 1}`;
			sent.push({ to: t.id, text, id });
			return id;
		},
		async thread(id) {
			return snaps.get(id) || { runs: [], turnItems: [] };
		},
	};
	const decider = {
		attempts: () => attempts,
		async call(body) {
			const choice = decide(body);
			attempts++;
			return { record: { status: 200, response: { answers: { verdict: { choice, probabilities: { [choice]: 0.9 } } } } }, cached: false };
		},
	};
	const hb = createHeartbeat({ ...BASE, ...cfg, nodes }, { t3, decider, helpers: { lastAssistantText, runOf }, logCheckin: (e) => checkins.push(e), logTick: () => {}, say: () => {} }, 0);
	const set = (id, fields, at) => {
		const t = { id, status: "completed", activeRunId: null, latestRunId: `${id}-r1`, pendingRuntimeRequest: null, lineage: { parentThreadId: null }, ...threads.get(id), ...fields };
		threads.set(id, t);
		return hb.observe(t, at);
	};
	const run = async (from, to, each) => {
		for (let t = from; t <= to; t += 10 * S) {
			if (each) await each(t);
			await hb.tick(t);
		}
	};
	return { hb, checkins, sent, snaps, set, run, attempts: () => attempts };
}

const verdictFor = (body, map) => {
	for (const [needle, v] of Object.entries(map)) if (body.state.worker.charter.includes(needle)) return v;
	return "stay_quiet";
};

test("fix 1: the backstop reaches a quiet worker even while a noisy sibling keeps waking the PM", async () => {
	const nodes = [
		{ role: "pm", threadId: "P", parent: null, charter: "pm" },
		{ role: "worker", threadId: "A", parent: "P", charter: "noisy worker" },
		{ role: "worker", threadId: "B", parent: "P", charter: "quiet worker" },
	];
	const h = harness(nodes, (b) => verdictFor(b, { noisy: "wake_pm", quiet: "stay_quiet" }));
	for (const id of ["P", "A", "B"]) h.set(id, {}, 0);
	let n = 1;
	await h.run(10 * S, 150 * S, (t) => h.set("A", { latestRunId: `A-r${++n}` }, t));
	assert.ok(
		h.checkins.some((e) => e.trigger === "backstop" && e.node === "B" && !e.suppressed),
		"worker B gets a backstop check-in within 150 s",
	);
});

test("fix 2: a decider error fails open with a logged wake_pm", async () => {
	const nodes = [
		{ role: "pm", threadId: "P", parent: null, charter: "pm" },
		{ role: "worker", threadId: "W", parent: "P", charter: "worker" },
	];
	const h = harness(nodes, () => {
		throw new Error("call budget of 60 attempts used");
	}, { backstopSec: 10000 });
	h.set("P", {}, 0);
	h.set("W", {}, 0);
	await h.run(10 * S, 40 * S);
	const entry = h.checkins.find((e) => e.node === "W" && e.trigger === "silence");
	assert.ok(entry, "the silence trigger produces a check-in entry");
	assert.equal(entry.verdict, "wake_pm");
	assert.match(entry.reason, /budget/);
	assert.equal(h.sent.filter((m) => m.to === "P").length, 1, "the PM is woken once");
});

test("fix 3: the backstop skips a worker whose state has not changed since the PM looked, until the ceiling", async () => {
	const nodes = [
		{ role: "pm", threadId: "P", parent: null, charter: "pm" },
		{ role: "worker", threadId: "W", parent: "P", charter: "worker" },
	];
	const h = harness(nodes, () => "stay_quiet");
	h.set("P", {}, 0);
	h.set("W", {}, 0);
	await h.run(10 * S, 1000 * S);
	const fired = h.checkins.filter((e) => e.trigger === "backstop" && !e.suppressed).map((e) => Date.parse(e.at) / S);
	assert.deepEqual(fired, [100, 600], "one backstop at backstopSec, the next only at backstopSec + ceilingSec");
	assert.ok(h.checkins.some((e) => e.trigger === "backstop" && e.suppressed && /unchanged/.test(e.reason)), "the skipped backstop is logged with its reason");
});

test("fix 4: in a tree with depth, a watched PM's own change is asked about, and a run the bot started is not", async () => {
	const nodes = [
		{ role: "orchestrator", threadId: "O", parent: null, charter: "orchestrator" },
		{ role: "pm", threadId: "P", parent: "O", charter: "pm under the orchestrator" },
		{ role: "worker", threadId: "W", parent: "P", charter: "stalled worker" },
	];
	const h = harness(nodes, (b) => (b.state.delta.pendingRequest || b.state.worker.charter.includes("stalled") ? "wake_pm" : "stay_quiet"), { backstopSec: 10000 });
	for (const id of ["O", "P", "W"]) h.set(id, {}, 0);
	await h.run(10 * S, 30 * S);
	const wake = h.sent.find((m) => m.to === "P");
	assert.ok(wake, "the stalled worker wakes its PM");
	h.snaps.set("P", { runs: [{ id: "P-r2", userMessageId: wake.id, status: "running" }], turnItems: [] });
	await h.set("P", { status: "running", activeRunId: "P-r2", latestRunId: "P-r2" }, 31 * S);
	await h.run(40 * S, 40 * S);
	assert.ok(!h.checkins.some((e) => e.node === "P" && e.trigger === "change" && !e.dropped), "the bot's own wake run is not a change");
	assert.equal(h.sent.filter((m) => m.to === "O").length, 0, "nothing has woken the orchestrator yet");
	await h.set("P", { status: "running", activeRunId: "P-r2", latestRunId: "P-r2", pendingRuntimeRequest: { id: "q1" } }, 45 * S);
	await h.run(50 * S, 50 * S);
	assert.ok(h.checkins.some((e) => e.node === "P" && e.trigger === "change" && e.delta.pendingRequest), "a question the PM opened is asked about");
	assert.equal(h.sent.filter((m) => m.to === "O").length, 1, "the orchestrator is woken once about its PM's question");
});

test("fix 4, rule 1: a run-finished delta is dropped for a T3 child of its PM and kept for a non-child", async () => {
	const nodes = [
		{ role: "pm", threadId: "P", parent: null, charter: "pm" },
		{ role: "worker", threadId: "C", parent: "P", charter: "child worker" },
		{ role: "worker", threadId: "N", parent: "P", charter: "plain worker" },
	];
	const h = harness(nodes, () => "stay_quiet", { backstopSec: 10000 });
	h.set("P", {}, 0);
	h.set("C", { status: "running", activeRunId: "C-r1", lineage: { parentThreadId: "P" } }, 0);
	h.set("N", { status: "running", activeRunId: "N-r1" }, 0);
	h.set("C", { status: "completed", activeRunId: null }, 5 * S);
	h.set("N", { status: "completed", activeRunId: null }, 5 * S);
	await h.run(10 * S, 10 * S);
	assert.ok(!h.checkins.some((e) => e.node === "C" && e.verdict), "no verdict is asked for the child's finished run");
	assert.ok(h.checkins.some((e) => e.node === "C" && e.dropped && /rule 1/.test(e.reason)), "the dropped delta is logged");
	assert.ok(h.checkins.some((e) => e.node === "N" && e.trigger === "change" && e.verdict), "the non-child's finished run is asked about");
});

test("repeat-wake suppression: one wake per stall, a new wake after the state changes", async () => {
	const nodes = [
		{ role: "pm", threadId: "P", parent: null, charter: "pm" },
		{ role: "worker", threadId: "W", parent: "P", charter: "stalled worker" },
	];
	const h = harness(nodes, () => "wake_pm", { backstopSec: 10000 });
	h.set("P", {}, 0);
	h.set("W", {}, 0);
	await h.run(10 * S, 150 * S);
	assert.equal(h.sent.length, 1, "silence asks at 30 s and 90 s wake the PM once");
	assert.ok(h.checkins.some((e) => e.node === "W" && e.verdict === "wake_pm" && e.suppressed && /already/.test(e.reason)), "the repeat wake is logged as suppressed");
	h.set("W", { latestRunId: "W-r2" }, 155 * S);
	await h.run(160 * S, 160 * S);
	assert.equal(h.sent.length, 2, "a changed state wakes the PM again");
});
