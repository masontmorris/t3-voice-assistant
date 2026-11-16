// Runs the labeled verdict set against the free decider and prints the confusion table.
// Usage: node eval/run.mjs <v1|v2> <dev|held|all> [--probe]
// --probe also sends the variant's question over an empty state. Results go to eval/results-<variant>-<split>.json.
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { VERDICTS } from "../core.mjs";
import { decider } from "../decider.mjs";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const [variant, split, flag] = process.argv.slice(2);
if (!["v1", "v2"].includes(variant) || !["dev", "held", "all"].includes(split)) throw new Error("usage: node eval/run.mjs <v1|v2> <dev|held|all> [--probe]");
const { verdictRequest } = await import(variant === "v1" ? "../core.mjs" : "./v2.mjs");
const set = JSON.parse(fs.readFileSync(path.join(HERE, "cases.json"), "utf8"));
const d1 = decider({ dir: path.join(HERE, "..", "state"), budget: 60 });

const requestFor = (c) =>
	verdictRequest({ role: "worker", charter: c.charter, status: c.status }, { role: "pm", charter: set.pmCharter }, { kind: c.trigger, delta: c.delta }, c.quietSec, c.lastReply);

async function verdictOf(body, label) {
	const { record, cached } = await d1.call(body, label);
	const a = record.status === 200 ? record.response.answers.verdict : null;
	return { got: a ? a.choice : `http ${record.status}`, p: a ? a.probabilities : null, cached };
}

const rows = [];
for (const c of set.cases.filter((x) => split === "all" || x.split === split)) {
	const r = await verdictOf(requestFor(c), `eval:${variant}:${c.id}`);
	rows.push({ id: c.id, split: c.split, situation: c.situation, expected: c.expected, ...r });
	console.log(`${c.id.padEnd(3)} ${c.split.padEnd(4)} expected ${c.expected.padEnd(10)} got ${r.got.padEnd(10)} ${r.p ? VERDICTS.map((v) => `${v}=${r.p[v].toFixed(2)}`).join(" ") : ""}${r.cached ? " (cached)" : ""}`);
}
let probe = null;
if (flag === "--probe") {
	const body = { ...requestFor(set.cases[0]), state: {} };
	probe = await verdictOf(body, `eval:${variant}:empty-state`);
	console.log(`empty state: got ${probe.got} ${VERDICTS.map((v) => `${v}=${probe.p[v].toFixed(2)}`).join(" ")}`);
}

for (const s of ["dev", "held"]) {
	const sub = rows.filter((r) => r.split === s);
	if (!sub.length) continue;
	console.log(`\n${s}: ${sub.filter((r) => r.got === r.expected).length}/${sub.length} correct. Rows are expected, columns are got.`);
	console.log(`${"".padEnd(11)}${VERDICTS.map((v) => v.padEnd(11)).join("")}`);
	for (const e of VERDICTS) console.log(`${e.padEnd(11)}${VERDICTS.map((g) => String(sub.filter((r) => r.expected === e && r.got === g).length).padEnd(11)).join("")}`);
	for (const r of sub.filter((x) => x.got !== x.expected)) {
		const way = r.expected !== "stay_quiet" && r.got === "stay_quiet" ? "missed alarm" : r.expected === "stay_quiet" ? "needless alert" : "wrong recipient";
		console.log(`  miss ${r.id} (${r.situation}): expected ${r.expected}, got ${r.got}, ${way}`);
	}
}
fs.writeFileSync(path.join(HERE, `results-${variant}-${split}.json`), JSON.stringify({ variant, split, rows, probe, ledgerAttempts: d1.attempts() }, null, 2) + "\n");
console.log(`\nledger attempts ${d1.attempts()} of 60`);
