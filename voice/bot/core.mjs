// The heartbeat's decision core, with no I/O of its own. heartbeat.mjs wires it to T3, the decider and the logs.
import { MODEL } from "./decider.mjs";

export const VERDICTS = ["stay_quiet", "wake_pm", "tell_mason"];

const iso = (ms) => new Date(ms).toISOString();

export const fingerprint = (t) => ({
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

export function verdictRequest(worker, pm, trigger, quietSec, lastReply) {
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

const wakeText = (worker, why) =>
	[`Heartbeat check-in (bot spike test). Worker thread ${worker.threadId} needs a look.`, `Why: ${why}.`, "Reply with one short line."].join("\n");

// deps: t3 { send(thread, text), thread(id) }, decider { call(body, label), attempts() }, helpers { lastAssistantText, runOf },
// logCheckin(entry), logTick(entry), say(msg).
export function createHeartbeat(cfg, deps, startedAt) {
	const { t3, decider, helpers, logCheckin, logTick, say } = deps;
	const nodes = new Map(cfg.nodes.map((n) => [n.threadId, { ...n, status: null, lastEventAt: null, thread: null }]));
	const workers = [...nodes.values()].filter((n) => n.role === "worker");
	const pms = [...nodes.values()].filter((n) => n.role === "pm");
	const watches = new Map();
	const wakes = new Map();
	for (const pm of pms) pm.lastCheckinAt = startedAt;

	async function confirmWakes(pm, at) {
		for (const [messageId, w] of wakes) {
			if (w.pm !== pm || pm.thread.activeRunId) continue;
			const snap = await t3.thread(pm.threadId);
			const r = helpers.runOf(snap, messageId);
			if (!r || r.status !== "completed") continue;
			wakes.delete(messageId);
			say(`wake confirmed in pm ${pm.threadId}: run ${r.id} ${r.status}; reply "${helpers.lastAssistantText(snap).slice(0, 80)}"`);
			logCheckin({ at: iso(at), node: pm.threadId, event: "wake_confirmed", messageId, runId: r.id, runStatus: r.status, wokeAt: iso(w.at) });
		}
	}

	function observe(t, at) {
		const n = nodes.get(t.id);
		if (!n) return null;
		n.thread = t;
		n.status = t.status;
		n.lastEventAt = at;
		if (!watches.has(t.id) && n.role === "worker") watches.set(t.id, newWatch(fingerprint(t), at, cfg));
		if (n.role === "pm") return confirmWakes(n, at).catch((e) => say(`wake confirm error: ${e.message}`));
		return null;
	}

	async function wake(pm, worker, why, at) {
		const messageId = await t3.send(pm.thread, wakeText(worker, why));
		pm.lastCheckinAt = at;
		wakes.set(messageId, { pm, at });
		return messageId;
	}

	async function ask(worker, trigger, t) {
		const pm = nodes.get(worker.parent);
		const quietSec = Math.round((t - worker.lastEventAt) / 1000);
		const lastReply = helpers.lastAssistantText(await t3.thread(worker.threadId));
		const body = verdictRequest(worker, pm, trigger, quietSec, lastReply);
		const { record, cached } = await decider.call(body, `${trigger.kind}:${worker.threadId.slice(-8)}`);
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
		const entry = { at: iso(t), node: worker.threadId, role: worker.role, trigger: trigger.kind, delta: trigger.delta, verdict, reason, source: cfg.forceVerdict ? "forced" : "system_one", cached, httpStatus: record.status };
		if (verdict === "wake_pm") entry.wakeMessageId = await wake(pm, worker, reason, t);
		if (verdict === "tell_mason") entry.note = "spike: logged only, Mason not notified";
		logCheckin(entry);
		say(`verdict ${verdict} for worker ${worker.threadId.slice(-8)}: ${reason}`);
	}

	let tickNo = 0;
	let busy = false;
	async function tick(t) {
		if (busy) return null;
		busy = true;
		tickNo++;
		const before = decider.attempts();
		const triggers = [];
		try {
			for (const w of workers) {
				const watch = watches.get(w.threadId);
				if (!watch) continue;
				const trigger = gate(watch, fingerprint(w.thread), w.lastEventAt, t, cfg);
				if (!trigger) continue;
				triggers.push(trigger.kind);
				await ask(w, trigger, t);
			}
			for (const pm of pms) {
				if (t - pm.lastCheckinAt < cfg.backstopSec * 1000) continue;
				const worker = workers.find((w) => w.parent === pm.threadId);
				const reason = `backstop: no check-in for ${Math.round((t - pm.lastCheckinAt) / 1000)}s (backstopSec ${cfg.backstopSec}), fired without asking system one`;
				const messageId = await wake(pm, worker, reason, t);
				triggers.push("backstop");
				logCheckin({ at: iso(t), node: pm.threadId, role: "pm", trigger: "backstop", delta: {}, verdict: "wake_pm", reason, source: "backstop", wakeMessageId: messageId });
				say(`backstop wake of pm ${pm.threadId.slice(-8)}`);
			}
		} catch (e) {
			say(`tick error: ${e.message}`);
		} finally {
			const modelCalls = decider.attempts() - before;
			logTick({ at: iso(t), tick: tickNo, triggers, modelCalls, ledgerTotal: decider.attempts() });
			if (!triggers.length) say(`tick ${tickNo}: no change, no silence timeout, ${modelCalls} model calls`);
			busy = false;
		}
		return triggers;
	}

	return { observe, tick, watches, nodes, ticks: () => tickNo };
}
