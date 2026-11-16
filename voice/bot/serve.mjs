// The standing bot: one root, its tree, the heartbeat over that tree, and a localhost API for the voice side.
// Usage:
//   node serve.mjs setup <projectId>          create the root and its research PM, or reuse them if the root is still live
//   node serve.mjs serve [overrides.json]     follow T3, discover nodes, run the heartbeat, fill the outbox, serve the API
//   node serve.mjs add-test-workers <projectId>  add the check 4 workers under the PM
//   node serve.mjs archive                    interrupt and archive every thread in the registry
// API on 127.0.0.1:<port> (default 8787):
//   POST /handoff { requestId, text, utterance }   hand a request to the root; the same requestId never makes a second task
//   GET  /outbox?after=<id>&waitMs=<n>              entries after <id>, waiting up to waitMs for a new one
//   POST /outbox/ack { ids }                        mark entries delivered
//   GET  /snapshot?conversation=&utterance=&transcript=&prefix=   the end-of-utterance snapshot, with no model call
//   GET  /registry                                  the tree's nodes
import fs from "node:fs";
import http from "node:http";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { createHeartbeat } from "./core.mjs";
import { decider } from "./decider.mjs";
import { ack, addOutbox, discover, ensureTree, handoff, ingestRoot, openStore, snapshot } from "./root.mjs";
import { connect, lastAssistantText, openQuestionText, runOf } from "./t3.mjs";

const STATE = path.join(path.dirname(fileURLToPath(import.meta.url)), "state");
const TREE = path.join(STATE, "tree.json");
const HAIKU = { instanceId: "claudeAgent", model: "claude-haiku-5-5", options: [{ id: "effort", value: "low" }] };
const DEFAULTS = { port: 8787, tickSec: 10, silenceSec: 600, backstopSec: 100000, ceilingSec: 1440, runSec: 600, budget: 120, forceVerdict: null };

const now = () => Date.now();
const iso = (ms) => new Date(ms).toISOString();
const append = (file, entry) => fs.appendFileSync(path.join(STATE, file), JSON.stringify(entry) + "\n");
const say = (msg) => console.log(`${iso(now())} ${msg}`);

async function setup(projectId) {
	if (!projectId) throw new Error("usage: node serve.mjs setup <projectId>");
	fs.mkdirSync(STATE, { recursive: true });
	const store = openStore(TREE);
	const r = await ensureTree(store.data, connect(), { projectId, modelSelection: HAIKU });
	store.save();
	say(`${r.created ? "created" : "reused"} root ${r.root}; registry has ${store.data.nodes.length} nodes`);
}

async function addTestWorkers(projectId) {
	const t3 = connect();
	const store = openStore(TREE);
	const pm = store.data.nodes.find((n) => n.role === "pm");
	const workers = [
		{ name: "token", charter: "Test worker for the bot-root spike. It publishes a dataset.", prompt: "Use your question tool to ask the user one question, \"Please paste the Hugging Face write token for the eval dataset.\", with the options \"I'll paste it\" and \"Skip publishing\". Then wait for the answer." },
		{ name: "blocked", charter: "Test worker for the bot-root spike. It needs a build number from its PM before it can continue.", prompt: "Reply with exactly one short line saying you are blocked waiting on the PM for the build number." },
	];
	for (const w of workers) {
		const id = await t3.launch({ projectId, title: `bot-root ${w.name} worker (throwaway)`, modelSelection: HAIKU, prompt: `${w.charter}\n${w.prompt}` });
		store.data.nodes.push({ role: "worker", threadId: id, parent: pm.threadId, charter: w.charter, testWorker: w.name });
		say(`added ${w.name} worker ${id} under pm ${pm.threadId}`);
	}
	store.save();
}

async function serve(overridesFile) {
	const cfg = { ...DEFAULTS, ...(overridesFile ? JSON.parse(fs.readFileSync(overridesFile, "utf8")) : {}) };
	const t3 = connect();
	const store = openStore(TREE);
	const { data } = store;
	if (!data.root) throw new Error("no root yet; run setup first");
	const waiters = new Set();
	const toOutbox = (item) => {
		const e = addOutbox(data, item);
		store.save();
		for (const w of waiters) w();
		say(`outbox ${e.id} ${e.source} ${e.requestId || "-"}: ${e.text.slice(0, 100)}`);
		return e.id;
	};
	const hb = createHeartbeat(
		{ ...cfg, nodes: data.nodes },
		{ t3, decider: decider({ dir: STATE, budget: cfg.budget }), helpers: { lastAssistantText, openQuestionText, runOf }, logCheckin: (e) => append("checkins.jsonl", e), logTick: (e) => append("ticks.jsonl", e), say, toOutbox },
		now(),
	);
	say(`serve: root ${data.root}, ${data.nodes.length} nodes, port ${cfg.port}, silence ${cfg.silenceSec}s, runSec ${cfg.runSec}`);

	let ingesting = Promise.resolve();
	const readRoot = () =>
		(ingesting = ingesting
			.then(async () => {
				for (const e of ingestRoot(data, await t3.thread(data.root))) {
					store.save();
					for (const w of waiters) w();
					say(`outbox ${e.id} ${e.source} ${e.requestId}: ${e.text.slice(0, 100)}`);
				}
				store.save();
			})
			.catch((e) => say(`root read error: ${e.message}`)));

	const learn = (t, at) => {
		const added = discover(data, t);
		if (added) {
			store.save();
			hb.addNode(added);
			say(`discovered ${added.role} ${added.threadId} under ${added.parent} from T3 lineage`);
		}
		hb.observe(t, at);
		if (t.id === data.root && !t.activeRunId) readRoot();
	};
	let seeded = false;
	const stream = t3.followShell((v) => {
		const at = now();
		if (v.kind === "snapshot" && !seeded) {
			seeded = true;
			const threads = v.snapshot.threads || [];
			for (let grew = true; grew; ) {
				const before = data.nodes.length;
				for (const t of threads) discover(data, t);
				grew = data.nodes.length > before;
			}
			store.save();
			for (const n of data.nodes) hb.addNode(n);
			for (const t of threads) if (hb.nodes.has(t.id)) learn(t, Date.parse(t.updatedAt) || at);
			say(`snapshot: registry has ${data.nodes.length} nodes, root ${threads.some((t) => t.id === data.root) ? "live" : "MISSING"}`);
		} else if (v.kind === "thread.updated") learn(v.thread, at);
	});

	const json = (res, code, body) => {
		res.writeHead(code, { "content-type": "application/json" });
		res.end(JSON.stringify(body));
	};
	const body = (req) => new Promise((resolve) => {
		let s = "";
		req.on("data", (c) => (s += c));
		req.on("end", () => resolve(s ? JSON.parse(s) : {}));
	});
	const after = (id) => data.outbox.slice(id ? data.outbox.findIndex((e) => e.id === id) + 1 : 0);
	const server = http.createServer(async (req, res) => {
		try {
			const url = new URL(req.url, "http://127.0.0.1");
			if (req.method === "POST" && url.pathname === "/handoff") {
				const r = await body(req);
				if (!r.requestId || !r.text) return json(res, 400, { error: "requestId and text are required" });
				const out = await handoff(data, t3, r, store.save);
				say(`handoff ${r.requestId}: ${out.status}`);
				return json(res, 200, out);
			}
			if (req.method === "GET" && url.pathname === "/outbox") {
				const since = url.searchParams.get("after");
				const waitMs = Math.min(Number(url.searchParams.get("waitMs") || 0), 60000);
				if (!after(since).length && waitMs > 0)
					await new Promise((resolve) => {
						const done = () => {
							waiters.delete(done);
							resolve();
						};
						waiters.add(done);
						setTimeout(done, waitMs);
					});
				return json(res, 200, { entries: after(since) });
			}
			if (req.method === "POST" && url.pathname === "/outbox/ack") {
				ack(data, (await body(req)).ids || []);
				store.save();
				return json(res, 200, { ok: true });
			}
			if (req.method === "GET" && url.pathname === "/snapshot") {
				const q = url.searchParams;
				return json(res, 200, snapshot(data, { conversationId: q.get("conversation"), utteranceId: q.get("utterance"), finalTranscript: q.get("transcript") || "", requestPrefix: q.get("prefix") || "" }));
			}
			if (req.method === "GET" && url.pathname === "/registry") return json(res, 200, { root: data.root, nodes: data.nodes });
			json(res, 404, { error: "not found" });
		} catch (e) {
			json(res, 500, { error: e.message });
		}
	});
	server.listen(cfg.port, "127.0.0.1");
	fs.writeFileSync(path.join(STATE, "serve.json"), JSON.stringify({ pid: process.pid, port: cfg.port, startedAt: iso(now()) }) + "\n");
	const timer = setInterval(() => hb.tick(now()), cfg.tickSec * 1000);
	const stop = () => {
		clearInterval(timer);
		server.close();
		t3.followShell.close();
	};
	process.on("SIGINT", stop);
	process.on("SIGTERM", stop);
	setTimeout(stop, cfg.runSec * 1000);
	await stream;
	await ingesting;
	store.save();
	say(`serve ended after ${hb.ticks()} ticks; registry ${data.nodes.length} nodes; outbox ${data.outbox.length} entries`);
}

async function archive() {
	const t3 = connect();
	const { data } = openStore(TREE);
	const live = (await t3.shell()).threads;
	for (const n of data.nodes) {
		const t = live.find((x) => x.id === n.threadId);
		if (t && t.activeRunId) await t3.interrupt(n.threadId, t.activeRunId);
		await t3.archive(n.threadId);
		say(`archived ${n.role} ${n.threadId}`);
	}
}

const [cmd, arg] = process.argv.slice(2);
const commands = { setup: () => setup(arg), serve: () => serve(arg), "add-test-workers": () => addTestWorkers(arg), archive };
if (!commands[cmd]) throw new Error("usage: node serve.mjs setup <projectId> | serve [overrides.json] | add-test-workers <projectId> | archive");
await commands[cmd]();
