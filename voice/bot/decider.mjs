// OpenRouter Decisions client for the free decider, ported from voice/segmentation/client.py on voice/stage2-hybrid.
// Every distinct request is cached on disk and never re-sent. Every HTTP attempt goes to the ledger, which enforces the budget.
// The key comes from OPENROUTER_API_KEY or the env file named by VOICE_ENV_FILE, and only ever goes in the Authorization header.
import crypto from "node:crypto";
import fs from "node:fs";
import path from "node:path";

export const MODEL = "perplexity/pplx-decider-flash-v1.3:free";
const URL = "https://openrouter.ai/api/alpha/decisions";
const MAX_ATTEMPTS = 5;

function apiKey() {
	if (process.env.OPENROUTER_API_KEY) return process.env.OPENROUTER_API_KEY;
	const file = process.env.VOICE_ENV_FILE;
	if (!file) throw new Error("set OPENROUTER_API_KEY or VOICE_ENV_FILE");
	for (const line of fs.readFileSync(file, "utf8").replace(/^﻿/, "").split(/\r?\n/)) {
		const i = line.indexOf("=");
		if (i > 0 && line.slice(0, i).trim() === "OPENROUTER_API_KEY") return line.slice(i + 1).trim().replace(/^["']|["']$/g, "");
	}
	throw new Error("OPENROUTER_API_KEY not found in VOICE_ENV_FILE");
}

const mentionsBilling = (body) => /credit|billing|payment|insufficient/i.test(JSON.stringify((body && body.error) || ""));
const nonzeroCost = (body) => !!(body && body.usage && body.usage.cost);
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

export function decider({ dir, budget }) {
	const cacheDir = path.join(dir, "cache");
	const ledger = path.join(dir, "calls.jsonl");
	const stop = path.join(dir, "BILLING_STOP");
	fs.mkdirSync(cacheDir, { recursive: true });
	const attempts = () => (fs.existsSync(ledger) ? fs.readFileSync(ledger, "utf8").split("\n").filter(Boolean).length : 0);
	let key = null;
	let gap = 0;

	return {
		attempts,
		async call(body, label) {
			if (!body.model.endsWith(":free")) throw new Error(`refusing non-free model ${body.model}`);
			const hash = crypto.createHash("sha256").update("decisions\n" + JSON.stringify(body)).digest("hex").slice(0, 24);
			const file = path.join(cacheDir, `decisions-${hash}.json`);
			if (fs.existsSync(file)) return { record: JSON.parse(fs.readFileSync(file, "utf8")), cached: true };
			if (fs.existsSync(stop)) throw new Error("BILLING_STOP present; no more calls");
			key ??= apiKey();
			let result;
			for (let attempt = 0; attempt < MAX_ATTEMPTS; attempt++) {
				if (attempts() >= budget) throw new Error(`call budget of ${budget} attempts used`);
				if (gap) await sleep(gap);
				const at = new Date().toISOString();
				const t0 = performance.now();
				let status = null;
				let response = null;
				let error = null;
				try {
					const r = await fetch(URL, {
						method: "POST",
						headers: { Authorization: `Bearer ${key}`, "Content-Type": "application/json" },
						body: JSON.stringify(body),
						signal: AbortSignal.timeout(30000),
					});
					status = r.status;
					response = await r.json().catch(() => ({ unparsed: true }));
				} catch (e) {
					error = e.name;
				}
				const ms = Math.round(performance.now() - t0);
				fs.appendFileSync(ledger, JSON.stringify({ at, route: "decisions", hash, label, status, ms, error }) + "\n");
				result = { status, response, at, ms };
				if (status === 402 || (status >= 400 && mentionsBilling(response)) || (status === 200 && nonzeroCost(response))) {
					fs.writeFileSync(stop, JSON.stringify({ at, status, label }));
					throw new Error(`billing stop: status ${status} on ${label}`);
				}
				if (status === 429) {
					gap = Math.min(30000, Math.max(2000, 2 * gap));
					continue;
				}
				if (status === null || [500, 502, 503, 524, 529].includes(status)) {
					await sleep(2 ** (attempt + 1) * 1000);
					continue;
				}
				gap = gap / 2 >= 500 ? gap / 2 : 0;
				break;
			}
			const record = { label, request: body, status: result.status, response: result.response, timing: { at: result.at, ms: result.ms } };
			fs.writeFileSync(file, JSON.stringify(record, null, 2) + "\n");
			return { record, cached: false };
		},
	};
}
