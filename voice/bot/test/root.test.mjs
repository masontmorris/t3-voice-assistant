import assert from "node:assert/strict";
import { test } from "node:test";
import { ack, discover, emptyTree, ensureTree, handoff, ingestRoot, snapshot } from "../root.mjs";

function fakeT3() {
	const threads = [];
	const sent = [];
	const items = new Map();
	let n = 0;
	return {
		threads,
		sent,
		items,
		async shell() {
			return { threads };
		},
		async launch({ title }) {
			const id = `t${++n}`;
			threads.push({ id, title, activeRunId: null });
			return id;
		},
		async send(t, text) {
			const id = `m${sent.length + 1}`;
			sent.push({ to: t.id, text, id });
			items.set(t.id, [...(items.get(t.id) || []), { type: "user_message", text }]);
			return id;
		},
		async thread(id) {
			return { turnItems: items.get(id) || [], runs: [] };
		},
	};
}
const opts = { projectId: "p", modelSelection: {} };

test("ensureTree: a second run reuses the live root and its tree", async () => {
	const t3 = fakeT3();
	const data = emptyTree();
	const first = await ensureTree(data, t3, opts);
	const second = await ensureTree(data, t3, opts);
	assert.equal(first.created, true);
	assert.equal(second.created, false);
	assert.equal(second.root, first.root);
	assert.equal(t3.threads.length, 2, "one root and one PM");
	assert.deepEqual(data.nodes.map((x) => x.role), ["orchestrator", "pm"]);
});

test("discover: a thread whose lineage parent is registered joins one level down; others do not", () => {
	const data = { ...emptyTree(), nodes: [{ role: "orchestrator", threadId: "R", parent: null }, { role: "pm", threadId: "P", parent: "R" }] };
	assert.equal(discover(data, { id: "W", title: "worker", lineage: { parentThreadId: "P" } }).role, "worker");
	assert.equal(discover(data, { id: "X", title: "unrelated", lineage: { parentThreadId: "Q" } }), null);
	assert.equal(discover(data, { id: "W", lineage: { parentThreadId: "P" } }), null, "a known node is not added twice");
	assert.equal(data.nodes.length, 3);
});

test("handoff: the same request id twice sends one message to the root", async () => {
	const t3 = fakeT3();
	const data = emptyTree();
	await ensureTree(data, t3, opts);
	const before = t3.sent.length;
	const a = await handoff(data, t3, { requestId: "c1:task_2", text: "how long is the drive?", utterance: "utt_1" }, () => {});
	const b = await handoff(data, t3, { requestId: "c1:task_2", text: "how long is the drive?", utterance: "utt_1" }, () => {});
	assert.deepEqual([a.status, b.status], ["created", "exists"]);
	assert.equal(t3.sent.length - before, 1);
	assert.match(t3.sent.at(-1).text, /^\[request c1:task_2\] /);
});

test("handoff: a send that crashed after reaching the root is not sent again", async () => {
	const t3 = fakeT3();
	const data = emptyTree();
	await ensureTree(data, t3, opts);
	data.handoffs["r9"] = { requestId: "r9", text: "x", status: "sending" };
	t3.items.set(data.root, [{ type: "user_message", text: "[request r9] x" }]);
	const before = t3.sent.length;
	assert.equal((await handoff(data, t3, { requestId: "r9", text: "x" }, () => {})).status, "exists");
	assert.equal(t3.sent.length, before);
});

test("ingestRoot: a handoff run is a root reply, a tagged run is a task result, and other runs add nothing", () => {
	const data = { ...emptyTree(), root: "R", handoffs: { r1: { requestId: "r1", text: "drive time", status: "sent", messageId: "m1" } } };
	const snap = {
		runs: [
			{ id: "run1", userMessageId: "m1", status: "completed" },
			{ id: "run2", userMessageId: "mcp-1", status: "completed" },
			{ id: "run3", userMessageId: "wake-1", status: "completed" },
			{ id: "run4", userMessageId: "mcp-2", status: "running" },
		],
		turnItems: [
			{ runId: "run1", type: "assistant_message", text: "Passed it to the research PM." },
			{ runId: "run2", type: "assistant_message", text: "[request r1] RESULT: About 35 minutes." },
			{ runId: "run3", type: "assistant_message", text: "Noted." },
		],
	};
	const added = ingestRoot(data, snap);
	assert.deepEqual(added.map((e) => [e.source, e.requestId, e.text]), [["root_reply", "r1", "Passed it to the research PM."], ["task_result", "r1", "About 35 minutes."]]);
	assert.equal(ingestRoot(data, snap).length, 0, "a run is read once");
});

test("snapshot: built from handoffs and the outbox alone", () => {
	const data = {
		...emptyTree(),
		handoffs: { "c1:task_2": { requestId: "c1:task_2", text: "drive time", status: "sent" }, "c1:task_3": { requestId: "c1:task_3", text: "weather", status: "sent" }, "c2:task_1": { requestId: "c2:task_1", text: "other", status: "sent" } },
		outbox: [
			{ id: "o1", source: "task_result", requestId: "c1:task_2", text: "About 35 minutes.", delivered: false },
			{ id: "o2", source: "tell_mason", node: "W", requestId: null, text: "needs a token", delivered: false },
			{ id: "o3", source: "tell_mason", node: "W", requestId: null, text: "old notice", delivered: true },
		],
	};
	const s = snapshot(data, { conversationId: "c1", utteranceId: "utt_1", finalTranscript: "t", requestPrefix: "c1:" });
	assert.deepEqual(s.tasks, [
		{ task_id: "c1:task_2", description: "drive time", status: "completed", result_summary: "About 35 minutes." },
		{ task_id: "c1:task_3", description: "weather", status: "running" },
	]);
	assert.deepEqual(s.for_mason.map((m) => m.id), ["o2"]);
	ack(data, ["o2"]);
	assert.equal(snapshot(data, { conversationId: "c1", utteranceId: "utt_2", finalTranscript: "t", requestPrefix: "c1:" }).for_mason.length, 0);
});
