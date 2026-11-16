// Gated heartbeat spike: listener event stream, silence timer, system-one verdict, wake one PM thread, verdict log.
// Usage:
//   node heartbeat.mjs setup <projectId>   create a throwaway PM and worker thread and write state/config.json
//   node heartbeat.mjs run [overrides.json] follow T3 and run the gate for config.runSec seconds
//   node heartbeat.mjs nudge               send the worker one message so the gate sees a real change
//   node heartbeat.mjs archive             archive every thread named in state/config.json
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { decider, MODEL } from "./decider.mjs";
import { connect, lastAssistantText, runOf } from "./t3.mjs";

const STATE = path.join(path.dirname(fileURLToPath(import.meta.url)), "state");
const CONFIG = path.join(STATE, "config.json");
const CHECKINS = path.join(STATE, "checkins.jsonl");
const TICKS = path.join(STATE, "ticks.jsonl");
const VERDICTS = ["stay_quiet", "wake_pm", "tell_mason"];
const HAIKU = { instanceId: "claudeAgent", model: "claude-haiku-5-5", options: [{ id: "effort", value: "low" }] };
const DEFAULTS = { tickSec: 10, silenceSec: 45, backstopSec: 180, runSec: 300, budget: 60, forceVerdict: null };

const now = () => Date.now();
const iso = (ms) => new Date(ms).toISOString();
const append = (file, entry) => fs.appendFileSync(file, JSON.stringify(entry) + "\n");
const say = (msg) => console.log(`${iso(now())} ${msg}`);

const fingerprint = (t) => ({
	status: t.status,
	activeRunId: t.activeRunId || null,
	latestRunId: t.latestRunId || null,
	pendingRequest: (t.pendingRuntimeRequest && t.pendingRuntimeRequest.id) || null,
});

function diff(before, after) {
	const delta = {};
	for (const k of Object.keys(after)) if (before[k] !== after[k]) delta[k] = { from: before[k], to: after[k] };
	return delta;
}

const newWatch = (fp, lastEventAt, cfg) => ({ seen: fp, silenceStep: 1, nextSilenceAt: lastEventAt + cfg.silenceSec * 1000 });

// The deterministic first stage. Returns a trigger, or null when the model must not be asked.
// After a silence ask, the next one waits 2x as long, then 4x, and a change resets the wait.
export function gate(watch, fp, lastEventAt, t, cfg) {
	const delta = diff(watch.seen, fp);
	if (Object.keys(delta).length) {
		Object.assign(watch, newWatch(fp, lastEventAt, cfg));
		return { kind: "change", delta };
	}
	if (t >= watch.nextSilenceAt) {
		watch.silenceStep *= 2;
		watch.nextSilenceAt = t + cfg.silenceSec * 1000 * watch.silenceStep;
		return { kind: "silence", delta: { quietSec: Math.round((t - lastEventAt) / 1000) } };
	}
	return null;
}

function verdictRequest(worker, pm, trigger, quietSec, lastReply) {
	return {
		model: MODEL,
		state: {
			worker: { role: worker.role, charter: worker.charter, status: worker.status, quiet_seconds: quietSec, last_reply: lastReply.slice(0, 300) },
			pm: { role: pm.role, charter: pm.charter },
			trigger: trigger.kind,
			delta: trigger.delta,
		},
		questions: {
			verdict: {
				type: "choice",
				instructions: "A heartbeat checks on a worker agent thread that reports to a PM thread. Decide what to do about this worker now.",
				criteria: {
					stay_quiet: "The worker is working normally, or it finished and needs nothing from anyone. Nobody needs to look.",
					wake_pm: "The worker has stalled, drifted from its charter, or is waiting on an answer from its PM that nobody has given. The PM should look now.",
					tell_mason: "Only the human owner can unblock it: it needs money, an account, a credential, or a decision outside the PM's charter.",
				},
			},
		},
	};
}

function wakeText(worker, why) {
	return [
		`Heartbeat check-in (bot spike test). Worker thread ${worker.threadId} needs a look.`,
		`Why: ${why}.`,
		"Reply with one short line.",
	].join("\n");
}

async function setup(projectId) {
	if (!projectId) throw new Error("usage: node heartbeat.mjs setup <projectId>");
	const t3 = connect();
	fs.mkdirSync(STATE, { recursive: true });
	const pmCharter = "Test PM for the heartbeat spike. Reply with one short line to every message.";
	const workerCharter = "Test worker for the heartbeat spike. It needs a build number from its PM before it can continue.";
	const pm = await t3.launch({ projectId, title: "bot-spike test PM (throwaway)", modelSelection: HAIKU, prompt: `${pmCharter}\nReply with one short line: ready.` });
	const worker = await t3.launch({
		projectId,
		title: "bot-spike test worker (throwaway)",
		modelSelection: HAIKU,
		prompt: `${workerCharter}\nReply with exactly one short line saying you are blocked waiting on the PM for the build number.`,
	});
	const config = {
		...DEFAULTS,
		nodes: [
			{ role: "pm", threadId: pm, parent: null, charter: pmCharter },
			{ role: "worker", threadId: worker, parent: pm, charter: workerCharter },
		],
	};
	fs.writeFileSync(CONFIG, JSON.stringify(config, null, 2) + "\n");
	say(`created pm ${pm} and worker ${worker}; wrote ${CONFIG}`);
}

async function run(overridesFile) {
	const cfg = { ...JSON.parse(fs.readFileSync(CONFIG, "utf8")), ...(overridesFile ? JSON.parse(fs.readFileSync(overridesFile, "utf8")) : {}) };
	if (cfg.forceVerdict && !VERDICTS.includes(cfg.forceVerdict)) throw new Error(`forceVerdict must be one of ${VERDICTS}`);
	const t3 = connect();
	const d1 = decider({ dir: STATE, budget: cfg.budget });
	const nodes = new Map(cfg.nodes.map((n) => [n.threadId, { ...n, status: null, lastEventAt: null, thread: null }]));
	const workers = [...nodes.values()].filter((n) => n.role === "worker");
	const pms = [...nodes.values()].filter((n) => n.role === "pm");
	const watches = new Map();
	const wakes = new Map(); // messageId -> { pm, at }
	const startedAt = now();
	for (const pm of pms) pm.lastCheckinAt = startedAt;
	say(`run: tick ${cfg.tickSec}s, silence ${cfg.silenceSec}s, backstop ${cfg.backstopSec}s, runSec ${cfg.runSec}, forceVerdict ${cfg.forceVerdict}`);

	function observe(t, at) {
		const n = nodes.get(t.id);
		if (!n) return;
		n.thread = t;
		n.status = t.status;
		n.lastEventAt = at;
		if (!watches.has(t.id) && n.role === "worker") watches.set(t.id, newWatch(fingerprint(t), at, cfg));
		if (n.role === "pm") confirmWakes(n).catch((e) => say(`wake confirm error: ${e.message}`));
	}

	async function confirmWakes(pm) {
		for (const [messageId, w] of wakes) {
			if (w.pm !== pm || pm.thread.activeRunId) continue;
			const snap = await t3.thread(pm.threadId);
			const r = runOf(snap, messageId);
			if (!r || r.status !== "completed") continue;
			wakes.delete(messageId);
			say(`wake confirmed in pm ${pm.threadId}: run ${r.id} ${r.status}; reply "${lastAssistantText(snap).slice(0, 80)}"`);
			append(CHECKINS, { at: iso(now()), node: pm.threadId, event: "wake_confirmed", messageId, runId: r.id, runStatus: r.status, wokeAt: iso(w.at) });
		}
	}

	async function wake(pm, worker, why) {
		const messageId = await t3.send(pm.thread, wakeText(worker, why));
		pm.lastCheckinAt = now();
		wakes.set(messageId, { pm, at: pm.lastCheckinAt });
		return messageId;
	}

	async function ask(worker, trigger) {
		const pm = nodes.get(worker.parent);
		const quietSec = Math.round((now() - worker.lastEventAt) / 1000);
		const lastReply = lastAssistantText(await t3.thread(worker.threadId));
		const body = verdictRequest(worker, pm, trigger, quietSec, lastReply);
		const { record, cached } = await d1.call(body, `${trigger.kind}:${worker.threadId.slice(-8)}`);
		const answer = record.status === 200 && record.response.answers && record.response.answers.verdict;
		const modelVerdict = answer && VERDICTS.includes(answer.choice) ? answer.choice : null;
		const p = answer && answer.probabilities ? answer.probabilities[answer.choice] : null;
		// Fail open: an unreadable answer wakes the PM.
		let verdict = modelVerdict || "wake_pm";
		let reason = modelVerdict
			? `${trigger.kind} ${JSON.stringify(trigger.delta)}; system one chose ${modelVerdict} (p=${p && p.toFixed(2)})`
			: `${trigger.kind}; system one gave no usable verdict (http ${record.status}), failing open`;
		if (cfg.forceVerdict) {
			reason += `; TEST SWITCH forceVerdict overrode ${verdict} with ${cfg.forceVerdict}`;
			verdict = cfg.forceVerdict;
		}
		const entry = { at: iso(now()), node: worker.threadId, role: worker.role, trigger: trigger.kind, delta: trigger.delta, verdict, reason, source: cfg.forceVerdict ? "forced" : "system_one", cached, httpStatus: record.status };
		if (verdict === "wake_pm") entry.wakeMessageId = await wake(pm, worker, reason);
		if (verdict === "tell_mason") entry.note = "spike: logged only, Mason not notified";
		append(CHECKINS, entry);
		say(`verdict ${verdict} for worker ${worker.threadId.slice(-8)}: ${reason}`);
		return cached ? 0 : 1;
	}

	let tickNo = 0;
	let busy = false;
	async function tick() {
		if (busy) return;
		busy = true;
		const t = now();
		tickNo++;
		const before = d1.attempts();
		const triggers = [];
		try {
			for (const w of workers) {
				const watch = watches.get(w.threadId);
				if (!watch) continue;
				const trigger = gate(watch, fingerprint(w.thread), w.lastEventAt, t, cfg);
				if (!trigger) continue;
				triggers.push(trigger.kind);
				await ask(w, trigger);
			}
			for (const pm of pms) {
				if (t - pm.lastCheckinAt < cfg.backstopSec * 1000) continue;
				const worker = workers.find((w) => w.parent === pm.threadId);
				const reason = `backstop: no check-in for ${Math.round((t - pm.lastCheckinAt) / 1000)}s (backstopSec ${cfg.backstopSec}), fired without asking system one`;
				const messageId = await wake(pm, worker, reason);
				triggers.push("backstop");
				append(CHECKINS, { at: iso(now()), node: pm.threadId, role: "pm", trigger: "backstop", delta: {}, verdict: "wake_pm", reason, source: "backstop", wakeMessageId: messageId });
				say(`backstop wake of pm ${pm.threadId.slice(-8)}`);
			}
		} catch (e) {
			say(`tick error: ${e.message}`);
		} finally {
			const modelCalls = d1.attempts() - before;
			append(TICKS, { at: iso(t), tick: tickNo, triggers, modelCalls, ledgerTotal: d1.attempts() });
			if (!triggers.length) say(`tick ${tickNo}: no change, no silence timeout, ${modelCalls} model calls`);
			busy = false;
		}
	}

	// Later snapshots are project-metadata refreshes with no threads (see t3listen), so only the first one seeds state.
	let seeded = false;
	const stream = t3.followShell((v) => {
		const at = now();
		if (v.kind === "snapshot" && !seeded) {
			seeded = true;
			for (const t of v.snapshot.threads || []) if (nodes.has(t.id)) observe(t, Date.parse(t.updatedAt) || at);
			say(`snapshot: watching ${watches.size} worker(s), ${pms.length} pm(s)`);
		} else if (v.kind === "thread.updated") observe(v.thread, at);
	});
	const timer = setInterval(tick, cfg.tickSec * 1000);
	const stop = () => {
		clearInterval(timer);
		t3.followShell.close();
	};
	process.on("SIGINT", stop);
	setTimeout(stop, cfg.runSec * 1000);
	await stream;
	say(`run ended after ${tickNo} ticks; ledger attempts ${d1.attempts()}`);
}

async function archive() {
	const t3 = connect();
	const cfg = JSON.parse(fs.readFileSync(CONFIG, "utf8"));
	for (const id of [...cfg.nodes.map((n) => n.threadId), ...(cfg.extraThreads || [])]) {
		await t3.archive(id);
		say(`archived ${id}`);
	}
}

// Test helper: start one turn in the worker so the gate sees a real change.
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
