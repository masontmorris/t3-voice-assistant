// Gated heartbeat spike: listener event stream, silence timer, system-one verdict, wake one PM thread, verdict log.
// Usage:
//   node heartbeat.mjs setup <projectId>   create a throwaway PM and three worker threads and write state/config.json
//   node heartbeat.mjs run [overrides.json] follow T3 and run the gate for config.runSec seconds
//   node heartbeat.mjs nudge               send the first worker one message so the gate sees a real change
//   node heartbeat.mjs archive             archive every thread named in state/config.json
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { createHeartbeat, VERDICTS } from "./core.mjs";
import { decider } from "./decider.mjs";
import { connect, lastAssistantText, openQuestionText, runOf } from "./t3.mjs";

const STATE = path.join(path.dirname(fileURLToPath(import.meta.url)), "state");
const CONFIG = path.join(STATE, "config.json");
const CHECKINS = path.join(STATE, "checkins.jsonl");
const TICKS = path.join(STATE, "ticks.jsonl");
const HAIKU = { instanceId: "claudeAgent", model: "claude-haiku-5-5", options: [{ id: "effort", value: "low" }] };
const DEFAULTS = { tickSec: 10, silenceSec: 45, backstopSec: 180, ceilingSec: 1440, runSec: 300, budget: 120, forceVerdict: null };

const now = () => Date.now();
const iso = (ms) => new Date(ms).toISOString();
const append = (file, entry) => fs.appendFileSync(file, JSON.stringify(entry) + "\n");
const say = (msg) => console.log(`${iso(now())} ${msg}`);

async function setup(projectId) {
	if (!projectId) throw new Error("usage: node heartbeat.mjs setup <projectId>");
	const t3 = connect();
	fs.mkdirSync(STATE, { recursive: true });
	const pmCharter = "Test PM for the heartbeat spike. Reply with one short line to every message.";
	const workers = [
		{
			name: "promise",
			charter: "Test worker for the heartbeat spike. It uploads a bundle and reports the link.",
			prompt: "Reply with exactly this line and nothing else: I started the upload in the background, and I'll report back when the upload completes.",
		},
		{
			name: "question",
			charter: "Test worker for the heartbeat spike. It runs one shell command and reports the output.",
			prompt: "Run the shell command `echo heartbeat-test` and reply with its output.",
		},
		{
			name: "finished",
			charter: "Test worker for the heartbeat spike. It writes one status line and then needs nothing from anyone.",
			prompt: "Reply with exactly one short line saying your task is done and you need nothing.",
		},
	];
	const pm = await t3.launch({ projectId, title: "bot-spike r3 test PM (throwaway)", modelSelection: HAIKU, prompt: `${pmCharter}\nReply with one short line: ready.` });
	const nodes = [{ role: "pm", threadId: pm, parent: null, charter: pmCharter }];
	for (const w of workers) {
		const id = await t3.launch({ projectId, title: `bot-spike r3 ${w.name} worker (throwaway)`, modelSelection: HAIKU, prompt: `${w.charter}\n${w.prompt}` });
		nodes.push({ role: "worker", threadId: id, parent: pm, charter: w.charter });
	}
	fs.writeFileSync(CONFIG, JSON.stringify({ ...DEFAULTS, nodes }, null, 2) + "\n");
	say(`created pm ${pm} and workers ${nodes.slice(1).map((n) => n.threadId).join(", ")}; wrote ${CONFIG}`);
}

async function run(overridesFile) {
	const cfg = { ...JSON.parse(fs.readFileSync(CONFIG, "utf8")), ...(overridesFile ? JSON.parse(fs.readFileSync(overridesFile, "utf8")) : {}) };
	if (cfg.forceVerdict && !VERDICTS.includes(cfg.forceVerdict)) throw new Error(`forceVerdict must be one of ${VERDICTS}`);
	const t3 = connect();
	const d1 = decider({ dir: STATE, budget: cfg.budget });
	const hb = createHeartbeat(
		cfg,
		{
			t3,
			decider: d1,
			helpers: { lastAssistantText, openQuestionText, runOf },
			logCheckin: (e) => append(CHECKINS, e),
			logTick: (e) => append(TICKS, e),
			say,
		},
		now(),
	);
	say(`run: tick ${cfg.tickSec}s, silence ${cfg.silenceSec}s, backstop ${cfg.backstopSec}s, ceiling ${cfg.ceilingSec}s, runSec ${cfg.runSec}, forceVerdict ${cfg.forceVerdict}`);

	// Later snapshots are project-metadata refreshes with no threads (see t3listen), so only the first one seeds state.
	let seeded = false;
	const stream = t3.followShell((v) => {
		const at = now();
		if (v.kind === "snapshot" && !seeded) {
			seeded = true;
			for (const t of v.snapshot.threads || []) if (hb.nodes.has(t.id)) hb.observe(t, Date.parse(t.updatedAt) || at);
			say(`snapshot: watching ${hb.watches.size} worker(s)`);
		} else if (v.kind === "thread.updated") hb.observe(v.thread, at);
	});
	const timer = setInterval(() => hb.tick(now()), cfg.tickSec * 1000);
	const stop = () => {
		clearInterval(timer);
		t3.followShell.close();
	};
	process.on("SIGINT", stop);
	setTimeout(stop, cfg.runSec * 1000);
	await stream;
	say(`run ended after ${hb.ticks()} ticks; ledger attempts ${d1.attempts()}`);
}

async function archive() {
	const t3 = connect();
	const cfg = JSON.parse(fs.readFileSync(CONFIG, "utf8"));
	const live = (await t3.shell()).threads;
	for (const id of [...cfg.nodes.map((n) => n.threadId), ...(cfg.extraThreads || [])]) {
		const t = live.find((x) => x.id === id);
		if (t && t.activeRunId) {
			await t3.interrupt(id, t.activeRunId);
			say(`interrupted the active run in ${id}`);
		}
		await t3.archive(id);
		say(`archived ${id}`);
	}
}

// Test helper: start one turn in the first worker so the gate sees a real change.
async function nudge() {
	const t3 = connect();
	const cfg = JSON.parse(fs.readFileSync(CONFIG, "utf8"));
	const id = cfg.nodes.find((n) => n.role === "worker").threadId;
	const t = (await t3.shell()).threads.find((x) => x.id === id);
	await t3.send(t, "Are you still blocked? Reply with one short line.");
	say(`nudged worker ${id}`);
}

const [cmd, arg] = process.argv.slice(2);
const commands = { setup: () => setup(arg), run: () => run(arg), archive, nudge };
if (!commands[cmd]) throw new Error("usage: node heartbeat.mjs setup <projectId> | run [overrides.json] | nudge | archive");
await commands[cmd]();
