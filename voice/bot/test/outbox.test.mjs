import assert from "node:assert/strict";
import { test } from "node:test";
import { harness, S } from "../fakes.mjs";

const PM = { role: "pm", threadId: "P", parent: null, charter: "pm" };
const card = { status: "waiting", activeRunId: "W-r1", pendingRuntimeRequest: { id: "q1", createdAt: new Date(0).toISOString() } };

function questionHarness(recipient) {
	const asked = [];
	const h = harness([PM, { role: "worker", threadId: "W", parent: "P", charter: "worker" }], (b) => {
		asked.push(Object.keys(b.questions)[0]);
		return b.questions.recipient ? recipient : "stay_quiet";
	}, { backstopSec: 1e6, silenceSec: 1e6, questionTimeoutSec: 60 });
	h.replies.set("W", "I need a Hugging Face write token. I asked in the question card.");
	h.set("P", {}, 0);
	h.set("W", card, 0);
	return { h, asked };
}

test("recipient: a question for Mason goes to the outbox, and the PM is not woken", async () => {
	const { h, asked } = questionHarness("tell_mason");
	await h.run(10 * S, 120 * S);
	assert.deepEqual(asked, ["recipient"], "the decider is asked one recipient question, once");
	const entry = h.checkins.find((e) => e.source === "rule_question");
	assert.equal(entry.verdict, "tell_mason");
	assert.equal(h.sent.length, 0, "the PM is not woken");
	assert.equal(h.outbox.length, 1);
	assert.equal(h.outbox[0].source, "tell_mason");
	assert.equal(h.outbox[0].node, "W");
});

test("recipient: a question for the PM wakes the PM, and nothing goes to the outbox", async () => {
	const { h } = questionHarness("wake_pm");
	await h.run(10 * S, 120 * S);
	assert.equal(h.checkins.find((e) => e.source === "rule_question").verdict, "wake_pm");
	assert.equal(h.sent.length, 1);
	assert.equal(h.outbox.length, 0);
});

test("recipient: a decider error fails open to the PM", async () => {
	const h = harness([PM, { role: "worker", threadId: "W", parent: "P", charter: "worker" }], () => {
		throw new Error("call budget of 120 attempts used");
	}, { backstopSec: 1e6, silenceSec: 1e6, questionTimeoutSec: 60 });
	h.set("P", {}, 0);
	h.set("W", card, 0);
	await h.run(10 * S, 120 * S);
	const entry = h.checkins.find((e) => e.source === "rule_question");
	assert.equal(entry.verdict, "wake_pm");
	assert.match(entry.reason, /budget/);
	assert.equal(h.sent.length, 1);
});

test("outbox: a tell_mason verdict from the gate lands in the outbox, and a wake_pm verdict does not", async () => {
	const nodes = [PM, { role: "worker", threadId: "M", parent: "P", charter: "needs mason" }, { role: "worker", threadId: "K", parent: "P", charter: "needs pm" }];
	const h = harness(nodes, (b) => (b.state.worker.charter === "needs mason" ? "tell_mason" : "wake_pm"), { backstopSec: 1e6 });
	for (const id of ["P", "M", "K"]) h.set(id, {}, 0);
	await h.run(10 * S, 30 * S);
	assert.deepEqual(h.outbox.map((o) => [o.source, o.node]), [["tell_mason", "M"]]);
	assert.equal(h.sent.filter((m) => m.to === "P").length, 1, "only the wake_pm verdict wakes the PM");
});
