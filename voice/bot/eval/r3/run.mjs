// Round 3 eval. `tune` runs the tune cases and prints the bar that the rule in cases.json picks.
// `held` runs the held cases once with STAY_QUIET_BAR from core.mjs and prints the whole-gate and decider-alone tables.
// Usage: node eval/r3/run.mjs <tune|held>
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { applyBar, ruleVerdict, STAY_QUIET_BAR, VERDICTS, verdictRequest } from "../../core.mjs";
import { decider } from "../../decider.mjs";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const split = process.argv[2];
if (!["tune", "held"].includes(split)) throw new Error("usage: node eval/r3/run.mjs <tune|held>");
if (split === "held" && STAY_QUIET_BAR == null) throw new Error("freeze STAY_QUIET_BAR in core.mjs before the held run");
const set = JSON.parse(fs.readFileSync(path.join(HERE, "cases.json"), "utf8"));
const d1 = decider({ dir: path.join(HERE, "..", "..", "state"), budget: 120 });

const rows = [];
for (const c of set.cases.filter((x) => x.split === split)) {
	const body = verdictRequest({ role: "worker", charter: c.charter, status: c.status }, { role: "pm", charter: set.pmCharter }, { kind: c.trigger, delta: c.delta }, c.quietSec, c.lastReply);
	const { record, cached } = await d1.call(body, `eval-r3:${c.id}`);
	const a = record.status === 200 ? record.response.answers.verdict : null;
	const decider_ = a ? a.choice : "wake_pm";
	const rule = ruleVerdict({ role: "worker", ...c }, {});
	const barred = split === "held" ? applyBar(decider_, a && a.probabilities, {}) : null;
	const gate = rule ? "wake_pm" : barred ? barred.verdict : null;
	const layer = rule ? rule.source : barred && barred.note ? "bar" : "decider";
	rows.push({ id: c.id, situation: c.situation, expected: c.expected, decider: decider_, p: a ? a.probabilities : null, rule: rule && rule.source, gate, layer, cached });
	console.log(
		`${c.id.padEnd(18)} expected ${c.expected.padEnd(10)} decider ${decider_.padEnd(10)} stay_quiet=${a ? a.probabilities.stay_quiet.toFixed(2) : "-"}  rule ${(rule ? rule.source : "-").padEnd(13)}${gate ? ` gate ${gate} (${layer})` : ""}`,
	);
}

const way = (expected, got) => (expected !== "stay_quiet" && got === "stay_quiet" ? "missed alarm" : expected === "stay_quiet" ? "needless alert" : "wrong recipient");
function table(title, key) {
	console.log(`\n${title}: ${rows.filter((r) => r[key] === r.expected).length}/${rows.length} correct. Rows are expected, columns are got.`);
	console.log(`${"".padEnd(11)}${VERDICTS.map((v) => v.padEnd(11)).join("")}`);
	for (const e of VERDICTS) console.log(`${e.padEnd(11)}${VERDICTS.map((g) => String(rows.filter((r) => r.expected === e && r[key] === g).length).padEnd(11)).join("")}`);
	for (const r of rows.filter((x) => x[key] !== x.expected)) console.log(`  miss ${r.id}: expected ${r.expected}, got ${r[key]}, ${way(r.expected, r[key])}${key === "gate" ? `, decided by ${r.layer}` : ""}`);
}

let bar = null;
if (split === "tune") {
	const open = rows.filter((r) => !r.rule && r.p);
	const A = open.filter((r) => r.expected !== "stay_quiet").map((r) => r.p.stay_quiet);
	const H = open.filter((r) => r.expected === "stay_quiet").map((r) => r.p.stay_quiet);
	const maxA = A.length ? Math.max(...A) : 0;
	const minH = H.length ? Math.min(...H) : 1;
	bar = minH - 0.01 > maxA ? Math.floor((minH - 0.01) * 100) / 100 : Math.ceil((maxA + 0.01) * 100) / 100;
	bar = Math.min(0.99, bar);
	console.log(`\nrule-free tune cases: A (alarms) = [${A.map((x) => x.toFixed(3)).join(", ")}], H (healthy) = [${H.map((x) => x.toFixed(3)).join(", ")}]`);
	console.log(`bar by the cases.json rule: ${bar}`);
	table("decider alone, tune", "decider");
} else {
	table("whole gate (rules, then decider with bar " + STAY_QUIET_BAR + ")", "gate");
	table("decider alone", "decider");
	const layers = {};
	for (const r of rows) layers[r.layer] = (layers[r.layer] || 0) + 1;
	console.log(`\nlayer that decided each held case: ${JSON.stringify(layers)}`);
}
fs.writeFileSync(path.join(HERE, `results-${split}.json`), JSON.stringify({ split, bar: split === "tune" ? bar : STAY_QUIET_BAR, rows, ledgerAttempts: d1.attempts() }, null, 2) + "\n");
console.log(`\nledger attempts ${d1.attempts()} of 120`);
