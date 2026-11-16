// The standing tree: one root, its registry of nodes, voice handoffs, and the outbox the voice reads.
// Everything lives in one JSON store that only the bot process writes. Functions here take the store's data and a T3 client.
import fs from "node:fs";

// tree.json: { root, nodes: [{ role, threadId, parent, charter, quietBudgetMin?, discovered? }],
//   handoffs: { [requestId]: { requestId, text, utterance, at, status: "sending" | "sent", messageId } },
//   outbox: [{ id, at, source: "root_reply" | "task_result" | "tell_mason", node, requestId, text, delivered }],
//   seenRuns: [root run ids already read] }
export const emptyTree = () => ({ root: null, nodes: [], handoffs: {}, outbox: [], seenRuns: [] });

export function openStore(file) {
	const data = fs.existsSync(file) ? { ...emptyTree(), ...JSON.parse(fs.readFileSync(file, "utf8")) } : emptyTree();
	const save = () => {
		fs.writeFileSync(`${file}.tmp`, JSON.stringify(data, null, 2) + "\n");
		fs.renameSync(`${file}.tmp`, file);
	};
	return { data, save };
}

const CHILD_ROLE = { orchestrator: "pm", pm: "worker", worker: "worker" };

export const rootCharter = (pmId) =>
	[
		"You are the root orchestrator in a test of a voice bot. This charter is a placeholder.",
		"Requests arrive as messages that start with [request <id>].",
		`For each request: pass it to the research PM, thread ${pmId}, with the mcp__t3-code__t3_thread_send tool. Keep the [request <id>] tag at the start of what you send. Then reply with one short line saying you passed it on.`,
		"When the PM sends you a result, reply with one line that starts with the same [request <id>] tag, then RESULT:, then the result.",
		"If a message is a heartbeat check-in, reply with one short line and do nothing else. Use no other tools.",
	].join("\n");

export const pmCharter = () =>
	[
		"You are the research PM in a test of a voice bot. This charter is a placeholder.",
		"Tasks arrive from the root as messages that start with [request <id>].",
		"For each task: give it to one worker with the mcp__t3-code__delegate_task tool, using claude-haiku-5-5 at low effort. Tell the worker to answer in one short line, to run no commands, to give an estimate if it cannot look things up, and to say so.",
		"When the worker's result arrives, send it to the root with the mcp__t3-code__t3_thread_send tool, as one line that starts with the task's [request <id>] tag. Then reply with one short line saying you sent it.",
		"If a message is a heartbeat check-in, reply with one short line and do nothing else.",
	].join("\n");

// Idempotent: a root that is still live in T3 is reused with its tree. Only a missing root starts a new tree.
export async function ensureTree(data, t3, { projectId, modelSelection }) {
	const live = (await t3.shell()).threads;
	if (data.root && live.some((t) => t.id === data.root)) return { created: false, root: data.root };
	const pm = await t3.launch({ projectId, title: "bot-root test research PM (throwaway)", modelSelection, prompt: `${pmCharter()}\nReply with one short line: ready.` });
	const root = await t3.launch({ projectId, title: "bot-root test root (throwaway)", modelSelection, prompt: `${rootCharter(pm)}\nReply with one short line: ready.` });
	const pmThread = (await t3.shell()).threads.find((t) => t.id === pm) || { id: pm, activeRunId: "unknown" };
	await t3.send(pmThread, `Setup: the root is thread ${root}. Send results there. Reply with one short line: noted.`);
	Object.assign(data, { ...emptyTree(), root });
	data.nodes.push({ role: "orchestrator", threadId: root, parent: null, charter: rootCharter(pm) });
	data.nodes.push({ role: "pm", threadId: pm, parent: root, charter: pmCharter(), rootThreadId: root });
	return { created: true, root, pm };
}

// A thread whose T3 lineage names a registered node as its parent joins the registry under that node.
export function discover(data, thread) {
	if (data.nodes.some((n) => n.threadId === thread.id)) return null;
	const parentId = thread.lineage && thread.lineage.parentThreadId;
	const parent = parentId && data.nodes.find((n) => n.threadId === parentId);
	if (!parent) return null;
	const node = { role: CHILD_ROLE[parent.role], threadId: thread.id, parent: parent.threadId, charter: thread.title || "", discovered: true };
	data.nodes.push(node);
	return node;
}

export function addOutbox(data, { source, node, requestId = null, text }, at = Date.now()) {
	const entry = { id: `o${data.outbox.length + 1}`, at: new Date(at).toISOString(), source, node, requestId, text, delivered: false };
	data.outbox.push(entry);
	return entry;
}

const handoffText = (req) => `[request ${req.requestId}] ${req.text}`;
const sentInRoot = (snap, req) =>
	((snap.projection || snap).turnItems || []).some((i) => i.type === "user_message" && typeof i.text === "string" && i.text.startsWith(`[request ${req.requestId}]`));

// The same request id never makes a second task. A handoff that crashed mid-send is checked against the root's messages
// before it is sent again.
export async function handoff(data, t3, req, save, at = Date.now()) {
	const known = data.handoffs[req.requestId];
	if (known && known.status === "sent") return { status: "exists", requestId: req.requestId };
	if (known && sentInRoot(await t3.thread(data.root), req)) {
		known.status = "sent";
		save();
		return { status: "exists", requestId: req.requestId };
	}
	data.handoffs[req.requestId] = { requestId: req.requestId, text: req.text, utterance: req.utterance || null, at: new Date(at).toISOString(), status: "sending" };
	save();
	const root = (await t3.shell()).threads.find((t) => t.id === data.root);
	const messageId = await t3.send(root, handoffText(req));
	Object.assign(data.handoffs[req.requestId], { status: "sent", messageId });
	save();
	return { status: "created", requestId: req.requestId };
}

const itemsOf = (snap) => {
	const items = (snap.projection || snap).turnItems || [];
	return Array.isArray(items) ? items : Object.values(items);
};
const runsOf = (snap) => (snap.projection || snap).runs || [];

// A t3_thread_send call that delivered this message to this thread. T3's HTTP snapshot omits tool outputs
// (outputOmitted), so the call is matched by recipient and exact text, or by the created message id when the output is there.
function sentBy(item, threadId, msg) {
	if (item.type !== "dynamic_tool" || !/t3_thread_send$/.test(item.toolName || "")) return false;
	const input = item.input || {};
	if (input.threadId === threadId && input.message === msg.text) return true;
	const out = item.output && (item.output.structuredContent || item.output);
	return !!out && out.messageId === msg.messageId;
}

// The set of request ids a root run's messages lead back to. Each hop goes from a message to the thread that sent it
// (senderThreadId), to the send call there, to the run that made the call, and on to every message that started that run.
// T3 batches messages that arrive together into one run, so one run can lead to several requests.
export async function chainRequests(data, rootSnap, run, readThread, maxHops = 6) {
	const byMessage = new Map(Object.values(data.handoffs).filter((h) => h.messageId).map((h) => [h.messageId, h.requestId]));
	const cache = new Map([[data.root, rootSnap]]);
	const read = async (id) => {
		if (!cache.has(id)) cache.set(id, await readThread(id));
		return cache.get(id);
	};
	const runMessages = (snap, runId) => itemsOf(snap).filter((i) => i.type === "user_message" && i.runId === runId);
	const found = new Set();
	const seen = new Set();
	let frontier = runMessages(rootSnap, run.id).map((m) => ({ threadId: data.root, msg: m }));
	for (let hop = 0; hop <= maxHops && frontier.length; hop++) {
		const next = [];
		for (const { threadId, msg } of frontier) {
			if (seen.has(msg.messageId)) continue;
			seen.add(msg.messageId);
			if (byMessage.has(msg.messageId)) {
				found.add(byMessage.get(msg.messageId));
				continue;
			}
			if (!msg.senderThreadId) continue;
			const senderSnap = await read(msg.senderThreadId);
			for (const call of itemsOf(senderSnap).filter((i) => sentBy(i, threadId, msg)))
				for (const m of runMessages(senderSnap, call.runId)) next.push({ threadId: msg.senderThreadId, msg: m });
		}
		frontier = next;
	}
	return found;
}

// Each "[request <id>] ..." segment of a reply, as [id, text without the tag and a leading RESULT:].
function tagSegments(text) {
	const marks = [...text.matchAll(/\[request ([^\]\s]+)\]/g)];
	return marks.map((m, k) => [m[1], text.slice(m.index + m[0].length, k + 1 < marks.length ? marks[k + 1].index : undefined).replace(/^\s*RESULT:\s*/i, "").trim()]);
}

// Turns each newly completed root run into outbox entries. A run started by a handoff message is that request's root reply.
// Any other run gives one task result per [request <id>] segment of its reply, since T3 can batch several results into one
// run. The sender chain checks each one: "confirmed" when the run leads back to that request, "not_in_chain" when it does not,
// "unavailable" without a thread reader.
export async function ingestRoot(data, snap, readThread = null, at = Date.now()) {
	const items = itemsOf(snap);
	const byMessage = new Map(Object.values(data.handoffs).filter((h) => h.messageId).map((h) => [h.messageId, h.requestId]));
	const added = [];
	for (const run of runsOf(snap)) {
		if (run.status !== "completed" || data.seenRuns.includes(run.id)) continue;
		data.seenRuns.push(run.id);
		const reply = items.filter((i) => i.runId === run.id && i.type === "assistant_message" && i.text).at(-1);
		if (!reply) continue;
		const fromHandoff = byMessage.get(run.userMessageId);
		if (fromHandoff) {
			added.push(addOutbox(data, { source: "root_reply", node: data.root, requestId: fromHandoff, text: reply.text }, at));
			continue;
		}
		const segments = tagSegments(reply.text).filter(([id]) => data.handoffs[id]);
		if (!segments.length) continue;
		const chain = readThread ? await chainRequests(data, snap, run, readThread) : null;
		for (const [requestId, text] of segments) {
			const entry = addOutbox(data, { source: "task_result", node: data.root, requestId, text }, at);
			entry.chain = !chain ? "unavailable" : chain.has(requestId) ? "confirmed" : "not_in_chain";
			added.push(entry);
		}
	}
	return added;
}

export function ack(data, ids) {
	for (const e of data.outbox) if (ids.includes(e.id)) e.delivered = true;
}

// The end-of-utterance snapshot (docs/voice/plan.md), built from the handoffs and the outbox alone. A handoff with a task
// result is completed. Anything else is running. Undelivered notices for Mason ride along.
export function snapshot(data, { conversationId, utteranceId, finalTranscript, requestPrefix = "" }) {
	const tasks = Object.values(data.handoffs)
		.filter((h) => h.requestId.startsWith(requestPrefix))
		.map((h) => {
			const result = data.outbox.filter((e) => e.source === "task_result" && e.requestId === h.requestId).at(-1);
			return result ? { task_id: h.requestId, description: h.text, status: "completed", result_summary: result.text } : { task_id: h.requestId, description: h.text, status: "running" };
		});
	const forMason = data.outbox.filter((e) => e.source === "tell_mason" && !e.delivered).map((e) => ({ id: e.id, node: e.node, text: e.text }));
	return { type: "utterance_snapshot", conversation_id: conversationId, utterance_id: utteranceId, final_transcript: finalTranscript, tasks, clarification_needed: null, for_mason: forMason };
}
