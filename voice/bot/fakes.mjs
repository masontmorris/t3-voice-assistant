import { createHeartbeat } from "./core.mjs";
import { lastAssistantText, runOf } from "./t3.mjs";

export const S = 1000;
const BASE = { silenceSec: 30, backstopSec: 100, ceilingSec: 500, forceVerdict: null };

// A fake T3 and decider. `decide(body)` returns a verdict string or { choice, p }, or throws to simulate a decider error.
export function harness(nodes, decide, cfg = {}) {
	const checkins = [];
	const sent = [];
	const snaps = new Map();
	const replies = new Map();
	const threads = new Map();
	let attempts = 0;
	const t3 = {
		async send(t, text) {
			const id = `m${sent.length + 1}`;
			sent.push({ to: t.id, text, id });
			return id;
		},
		async thread(id) {
			const snap = snaps.get(id) || { runs: [], turnItems: [] };
			return replies.has(id) ? { ...snap, turnItems: [{ type: "assistant_message", text: replies.get(id) }] } : snap;
		},
	};
	const decider = {
		attempts: () => attempts,
		async call(body) {
			const out = decide(body);
			attempts++;
			const { choice, p } = typeof out === "string" ? { choice: out, p: { [out]: 0.9 } } : out;
			return { record: { status: 200, response: { answers: { verdict: { choice, probabilities: p } } } }, cached: false };
		},
	};
	const hb = createHeartbeat({ ...BASE, ...cfg, nodes }, { t3, decider, helpers: { lastAssistantText, runOf }, logCheckin: (e) => checkins.push(e), logTick: () => {}, say: () => {} }, 0);
	const set = (id, fields, at) => {
		const t = { id, status: "completed", activeRunId: null, latestRunId: `${id}-r1`, pendingRuntimeRequest: null, pendingBackgroundTasks: [], lineage: { parentThreadId: null }, ...threads.get(id), ...fields };
		threads.set(id, t);
		return hb.observe(t, at);
	};
	const run = async (from, to, each) => {
		for (let t = from; t <= to; t += 10 * S) {
			if (each) await each(t);
			await hb.tick(t);
		}
	};
	return { hb, checkins, sent, snaps, replies, set, run, attempts: () => attempts };
}

export const verdictFor = (body, map) => {
	for (const [needle, v] of Object.entries(map)) if (body.state.worker.charter.includes(needle)) return v;
	return "stay_quiet";
};
