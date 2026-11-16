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

const sameState = (a, b) => !!a && !!b && Object.keys(a).every((k) => a[k] === b[k]);

// seen and the silence fields drive the gate. lookedAt and lookedFp record when the parent was last woken about this node and
// what the node's state was then. lookedFp starts null, so the first backstop always fires.
const newWatch = (fp, lastEventAt, cfg, startedAt) => ({
	seen: fp,
	silenceStep: 1,
	nextSilenceAt: lastEventAt + cfg.silenceSec * 1000,
	lookedAt: startedAt,
	lookedFp: null,
	backstopSkipLogged: null,
});

// The deterministic first stage. Returns a trigger, or null when the model must not be asked.
// After a silence ask, the next one waits 2x as long, then 4x, and a change resets the wait.
export function gate(watch, fp, lastEventAt, t, cfg) {
	const delta = diff(watch.seen, fp);
	if (Object.keys(delta).length) {
		watch.seen = fp;
		watch.silenceStep = 1;
		watch.nextSilenceAt = lastEventAt + cfg.silenceSec * 1000;
		return { kind: "change", delta };
	}
	if (t >= watch.nextSilenceAt) {
		watch.silenceStep *= 2;
		watch.nextSilenceAt = t + cfg.silenceSec * 1000 * watch.silenceStep;
		return { kind: "silence", delta: { quietSec: Math.round((t - lastEventAt) / 1000) } };
	}
	return null;
}

// A wake about a node is blocked when its parent already looked at this exact state and the ceiling has not passed.
export function wakeBlock(watch, fp, t, cfg) {
	if (!sameState(watch.lookedFp, fp) || t - watch.lookedAt >= cfg.ceilingSec * 1000) return null;
	return `state unchanged since the parent already looked at ${iso(watch.lookedAt)}; next wake after a change or at ${iso(watch.lookedAt + cfg.ceilingSec * 1000)}`;
}

const RUN_KEYS = ["status", "activeRunId", "latestRunId"];

// The quiet time a running node may have before the stall rule wakes its parent, by role, when nothing more specific is known.
export const ROLE_BUDGET_MIN = { worker: 30, pm: 60, orchestrator: 120 };
export const RULE_DEFAULTS = { questionTimeoutSec: 600, promiseGraceSec: 300, estimateSlack: 2 };
export const STAY_QUIET_BAR = 0.88;

const NUMBER_WORDS = { a: 1, an: 1, one: 1, two: 2, three: 3, four: 4, five: 5, six: 6, seven: 7, eight: 8, nine: 9, ten: 10, fifteen: 15, twenty: 20, thirty: 30, forty: 40, fifty: 50, sixty: 60, ninety: 90 };
const ESTIMATE_RE = new RegExp(
	String.raw`\b(?:takes?|about|around|roughly|eta|finish(?:es)? in|done in|expect(?:ed)?|another)\b[^.]{0,30}?\b(\d+(?:\.\d+)?|${Object.keys(NUMBER_WORDS).join("|")})\s*(minutes?|mins?|hours?|hrs?)\b`,
	"i",
);
const CHARTER_BUDGET_RE = /\bprogress every (\d+) ?min/i;
const PROMISE_RE =
	/\b(?:will|i'll|i will)\s+(?:report back|check back|follow up|let (?:you|the pm) know|update you|post the results?)\b|\bonce (?:it|this|that|the \w+) (?:finishes|completes|is done)\b|\bwhen it (?:finishes|completes)\b/i;

// The worker's own stated estimate in minutes, or null. "half an hour" counts as 30.
export function estimateMin(reply) {
	if (/\bhalf an hour\b/i.test(reply)) return 30;
	const m = ESTIMATE_RE.exec(reply || "");
	if (!m) return null;
	const n = NUMBER_WORDS[m[1].toLowerCase()] ?? Number(m[1]);
	return /^h/i.test(m[2]) ? n * 60 : n;
}

// The node's expectation: how long it may stay quiet while running. Explicit config wins, then the charter, then the
// worker's own estimate with slack, then the role default.
export function budgetFor(f, cfg) {
	if (f.quietBudgetMin) return { min: f.quietBudgetMin, from: "the node's configured budget" };
	const c = CHARTER_BUDGET_RE.exec(f.charter || "");
	if (c) return { min: Number(c[1]), from: "the charter" };
	const slack = cfg.estimateSlack ?? RULE_DEFAULTS.estimateSlack;
	const e = estimateMin(f.lastReply);
	if (e) return { min: e * slack, from: `the worker's estimate of ${e} min, times ${slack}` };
	const roles = { ...ROLE_BUDGET_MIN, ...cfg.roleBudgetMin };
	return { min: roles[f.role] ?? ROLE_BUDGET_MIN.worker, from: `the ${f.role} default` };
}

// The fixed rules, checked before any model call. f: { role, charter, quietBudgetMin, running, status, quietSec, lastReply,
// openQuestionSec, backgroundTasks }. Returns { source, reason } for a wake, or null.
export function ruleVerdict(f, cfg) {
	const questionTimeoutSec = cfg.questionTimeoutSec ?? RULE_DEFAULTS.questionTimeoutSec;
	if (f.openQuestionSec != null && f.openQuestionSec >= questionTimeoutSec)
		return { source: "rule_question", reason: `a question has been open for ${Math.round(f.openQuestionSec)}s, past questionTimeoutSec ${questionTimeoutSec}` };
	if (f.running) {
		const b = budgetFor(f, cfg);
		if (f.quietSec >= b.min * 60)
			return { source: "rule_stall", reason: `running and quiet for ${Math.round(f.quietSec / 60)} min, past its budget of ${b.min} min from ${b.from}` };
		return null;
	}
	const promiseGraceSec = cfg.promiseGraceSec ?? RULE_DEFAULTS.promiseGraceSec;
	if (f.status === "completed" && !f.backgroundTasks && PROMISE_RE.test(f.lastReply || "") && f.quietSec >= promiseGraceSec)
		return { source: "rule_promise", reason: `the run ended promising a later report, nothing is running or in the background, and it has been quiet for ${Math.round(f.quietSec)}s` };
	return null;
}

// stay_quiet counts only when its probability clears the bar. Below it, the gate wakes whichever of wake_pm and tell_mason
// the decider found likelier, and wake_pm on a tie, because the PM can still escalate to Mason.
export function applyBar(choice, p, cfg) {
	const bar = cfg.stayQuietBar === undefined ? STAY_QUIET_BAR : cfg.stayQuietBar;
	if (choice !== "stay_quiet" || bar == null || (p && p.stay_quiet >= bar)) return { verdict: choice, note: null };
	const verdict = (p.tell_mason || 0) > (p.wake_pm || 0) ? "tell_mason" : "wake_pm";
	return { verdict, note: `stay_quiet p=${(p.stay_quiet || 0).toFixed(2)} is below the bar ${bar}, so the gate routed it to ${verdict}` };
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

const wakeText = (node, why) =>
	[`Heartbeat check-in (bot spike test). ${node.role} thread ${node.threadId} needs a look.`, `Why: ${why}.`, "Reply with one short line."].join("\n");

// deps: t3 { send(thread, text), thread(id) }, decider { call(body, label), attempts() }, helpers { lastAssistantText, runOf },
// logCheckin(entry), logTick(entry), say(msg). Every node with a parent is watched, and its parent is the one woken about it.
export function createHeartbeat(cfg, deps, startedAt) {
	const { t3, decider, helpers, logCheckin, logTick, say } = deps;
	const nodes = new Map(cfg.nodes.map((n) => [n.threadId, { ...n, status: null, lastEventAt: null, thread: null }]));
	const watched = [...nodes.values()].filter((n) => n.parent && nodes.has(n.parent));
	const watches = new Map();
	const wakes = new Map();
	const botMessages = new Set();
	const botRuns = new Set();

	async function confirmWakes(target, at) {
		for (const [messageId, w] of wakes) {
			if (w.target !== target || target.thread.activeRunId) continue;
			const snap = await t3.thread(target.threadId);
			const r = helpers.runOf(snap, messageId);
			if (!r || r.status !== "completed") continue;
			wakes.delete(messageId);
			say(`wake confirmed in ${target.role} ${target.threadId}: run ${r.id} ${r.status}; reply "${helpers.lastAssistantText(snap).slice(0, 80)}"`);
			logCheckin({ at: iso(at), node: target.threadId, event: "wake_confirmed", messageId, runId: r.id, runStatus: r.status, wokeAt: iso(w.at) });
		}
	}

	function observe(t, at) {
		const n = nodes.get(t.id);
		if (!n) return null;
		n.thread = t;
		n.status = t.status;
		n.lastEventAt = at;
		if (!watches.has(t.id) && watched.includes(n)) watches.set(t.id, newWatch(fingerprint(t), at, cfg, startedAt));
		if ([...wakes.values()].some((w) => w.target === n)) return confirmWakes(n, at).catch((e) => say(`wake confirm error: ${e.message}`));
		return null;
	}

	// Sends the wake unless the suppression rule blocks it. Sets entry.wakeMessageId, entry.suppressed or entry.wakeError.
	async function wake(node, entry, t) {
		const watch = watches.get(node.threadId);
		const fp = fingerprint(node.thread);
		const block = wakeBlock(watch, fp, t, cfg);
		if (block) {
			entry.suppressed = true;
			entry.reason += `; wake suppressed: ${block}`;
			return;
		}
		const target = nodes.get(node.parent);
		try {
			const messageId = await t3.send(target.thread, wakeText(node, entry.reason));
			botMessages.add(messageId);
			wakes.set(messageId, { target, at: t });
			entry.wakeMessageId = messageId;
			watch.lookedAt = t;
			watch.lookedFp = fp;
		} catch (e) {
			entry.wakeError = e.message;
		}
	}

	// Removes run fields that changed only because of a run the bot's own wake started, and drops a finished run of a T3 child,
	// since T3 already wakes the parent when a child's run ends (rule 1). Returns { delta } or { dropped: reason }.
	async function filterDelta(node, delta, fp) {
		const runIds = [fp.activeRunId, fp.latestRunId].filter(Boolean);
		if ((delta.activeRunId || delta.latestRunId) && runIds.some((id) => !botRuns.has(id)) && botMessages.size) {
			const snap = await t3.thread(node.threadId);
			for (const r of (snap.projection || snap).runs || []) if (botMessages.has(r.userMessageId)) botRuns.add(r.id);
		}
		const kept = { ...delta };
		if (runIds.length && runIds.every((id) => botRuns.has(id))) for (const k of RUN_KEYS) delete kept[k];
		if (!Object.keys(kept).length) return { dropped: "the run was started by the bot's own wake message" };
		const child = !!node.thread.lineage && node.thread.lineage.parentThreadId === node.parent;
		if (child && !fp.activeRunId && fp.status === "completed" && Object.keys(kept).every((k) => RUN_KEYS.includes(k)))
			return { dropped: "rule 1: the node is a T3 child of its parent, and T3 wakes the parent when the child's run completes" };
		return { delta: kept };
	}

	async function refreshReply(node, watch) {
		try {
			watch.lastReply = helpers.lastAssistantText(await t3.thread(node.threadId));
		} catch (e) {
			watch.lastReply = watch.lastReply || "";
			say(`reply read error for ${node.threadId.slice(-8)}: ${e.message}`);
		}
	}

	function trackQuestion(node, watch, fp, t) {
		if (!fp.pendingRequest) {
			watch.question = null;
			return;
		}
		if (watch.question && watch.question.id === fp.pendingRequest) return;
		const opened = Date.parse((node.thread.pendingRuntimeRequest || {}).createdAt);
		watch.question = { id: fp.pendingRequest, since: Number.isFinite(opened) ? opened : t };
	}

	const featuresOf = (node, watch, fp, t) => ({
		role: node.role,
		charter: node.charter,
		quietBudgetMin: node.quietBudgetMin,
		running: !!fp.activeRunId,
		status: fp.status,
		quietSec: (t - node.lastEventAt) / 1000,
		lastReply: watch.lastReply || "",
		openQuestionSec: watch.question ? (t - watch.question.since) / 1000 : null,
		backgroundTasks: (node.thread.pendingBackgroundTasks || []).length,
	});

	async function ask(node, trigger, t) {
		const parent = nodes.get(node.parent);
		const entry = { at: iso(t), node: node.threadId, role: node.role, trigger: trigger.kind, delta: trigger.delta };
		let verdict;
		try {
			const quietSec = Math.round((t - node.lastEventAt) / 1000);
			const body = verdictRequest(node, parent, trigger, quietSec, watches.get(node.threadId).lastReply || "");
			const { record, cached } = await decider.call(body, `${trigger.kind}:${node.threadId.slice(-8)}`);
			const answer = record.status === 200 && record.response.answers && record.response.answers.verdict;
			const modelVerdict = answer && VERDICTS.includes(answer.choice) ? answer.choice : null;
			const p = answer && answer.probabilities ? answer.probabilities[answer.choice] : null;
			const barred = modelVerdict ? applyBar(modelVerdict, answer.probabilities, cfg) : null;
			verdict = barred ? barred.verdict : "wake_pm";
			entry.reason = modelVerdict
				? `${trigger.kind} ${JSON.stringify(trigger.delta)}; system one chose ${modelVerdict} (p=${p && p.toFixed(2)})${barred.note ? `; ${barred.note}` : ""}`
				: `${trigger.kind}; system one gave no usable verdict (http ${record.status}), failing open`;
			entry.source = modelVerdict ? "system_one" : "fail_open";
			if (modelVerdict) entry.modelChoice = modelVerdict;
			Object.assign(entry, { cached, httpStatus: record.status });
		} catch (e) {
			verdict = "wake_pm";
			entry.reason = `${trigger.kind} ${JSON.stringify(trigger.delta)}; decider error (${e.message}), failing open`;
			entry.source = "fail_open";
		}
		if (cfg.forceVerdict && entry.source === "system_one") {
			entry.reason += `; TEST SWITCH forceVerdict overrode ${verdict} with ${cfg.forceVerdict}`;
			verdict = cfg.forceVerdict;
			entry.source = "forced";
		}
		entry.verdict = verdict;
		if (verdict === "wake_pm") await wake(node, entry, t);
		if (verdict === "tell_mason") entry.note = "spike: logged only, Mason not notified";
		logCheckin(entry);
		say(`verdict ${verdict}${entry.suppressed ? " (suppressed)" : ""} for ${node.role} ${node.threadId.slice(-8)}: ${entry.reason}`);
	}

	// The backstop is per node and never asks system one. It fires when the parent has not looked at this node for backstopSec,
	// except that a node whose state is unchanged since that look waits for ceilingSec. A skip is logged once per look.
	async function backstop(node, t) {
		const watch = watches.get(node.threadId);
		if (t - watch.lookedAt < cfg.backstopSec * 1000) return false;
		if (wakeBlock(watch, fingerprint(node.thread), t, cfg)) {
			if (watch.backstopSkipLogged === watch.lookedAt) return false;
			watch.backstopSkipLogged = watch.lookedAt;
		}
		const reason = `backstop: the parent has not looked at this node for ${Math.round((t - watch.lookedAt) / 1000)}s (backstopSec ${cfg.backstopSec}), without asking system one`;
		const entry = { at: iso(t), node: node.threadId, role: node.role, trigger: "backstop", delta: {}, verdict: "wake_pm", reason, source: "backstop" };
		await wake(node, entry, t);
		logCheckin(entry);
		say(`backstop ${entry.suppressed ? "skipped" : "wake"} for ${node.role} ${node.threadId.slice(-8)}: ${entry.reason}`);
		return true;
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
			for (const n of watched) {
				const watch = watches.get(n.threadId);
				if (!watch) continue;
				const fp = fingerprint(n.thread);
				let trigger = gate(watch, fp, n.lastEventAt, t, cfg);
				if (watch.lastReply === undefined || (trigger && trigger.kind === "change")) await refreshReply(n, watch);
				trackQuestion(n, watch, fp, t);
				if (trigger && trigger.kind === "change") {
					const f = await filterDelta(n, trigger.delta, fp);
					if (f.dropped) {
						logCheckin({ at: iso(t), node: n.threadId, role: n.role, trigger: "change", delta: trigger.delta, dropped: true, reason: f.dropped });
						trigger = null;
					} else trigger.delta = f.delta;
				}
				const rule = wakeBlock(watch, fp, t, cfg) ? null : ruleVerdict(featuresOf(n, watch, fp, t), cfg);
				if (rule) {
					triggers.push(rule.source);
					const entry = { at: iso(t), node: n.threadId, role: n.role, trigger: trigger ? trigger.kind : "rule", delta: trigger ? trigger.delta : {}, verdict: "wake_pm", reason: rule.reason, source: rule.source };
					await wake(n, entry, t);
					logCheckin(entry);
					say(`${rule.source} wake for ${n.role} ${n.threadId.slice(-8)}: ${rule.reason}`);
				} else if (trigger) {
					triggers.push(trigger.kind);
					await ask(n, trigger, t);
				}
				if (await backstop(n, t)) triggers.push("backstop");
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
