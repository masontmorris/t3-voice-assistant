"""Run one hosted configuration over an existing frozen pick set."""

import argparse
import json
import time
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from functools import partial
from pathlib import Path
from typing import Callable

from voice.liquid_decider import client as liquid

from . import harness, project_harness
from .decider import FULL_LIMIT, MIN_CONFIDENCE, Candidate, choice_request, interpret, shortlist
from .model import render, render_projects

ARMS = ("narrow+decider", "full+decider")
REPEATS = 3


@dataclass(frozen=True)
class Case:
	label: dict
	gold: dict
	visible: list[Candidate]
	evidence: Callable[[set[str]], str]


def thread_evidence(utterance, threads, projects, now, offered):
	rows = [thread for thread in threads if thread["threadId"] in offered]
	return render(utterance, rows, projects, now)[-1]["content"]


def project_evidence(utterance, projects, offered):
	rows = [project for project in projects if project["projectId"] in offered]
	return render_projects(utterance, rows)[-1]["content"]


def load_cases(kind, root):
	if kind == "project":
		snap, labels = project_harness.load_inputs(root)
		projects = project_harness.projects_of(snap)
		items = [Candidate(p["projectId"], p["name"]) for p in projects]
		return [Case(label, project_harness.gold_for_thread(label), items,
		             partial(project_evidence, label["utterance"], projects)) for label in labels]
	snaps, labels = harness.load_inputs(root)
	visible = {key: harness.visible(snap) for key, snap in snaps.items()}
	listings = {
		key: [Candidate(t["threadId"], t["title"], snap["projects"].get(t["projectId"], ""))
		      for t in visible[key]]
		for key, snap in snaps.items()
	}
	cases = []
	for label in labels:
		key = label["snapshot"]
		evidence = partial(thread_evidence, label["utterance"], visible[key],
		                   snaps[key]["projects"], snaps[key]["takenAt"])
		cases.append(Case(label, label["gold"], listings[key], evidence))
	return cases


def selected_cases(cases, arm):
	if arm == "full+decider":
		return [case for case in cases if len(case.visible) <= FULL_LIMIT]
	return cases


def write_json(path, data):
	path.write_text(json.dumps(data, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")


def score(case, items, answer, kind):
	picked, parse = interpret(answer, items, kind)
	listed = [item.id for item in case.visible]
	strict = harness.outcome(picked, case.gold, listed, parse)
	confirm = harness.confirm_outcome(picked, case.gold, listed, parse)
	return {
		"pick": asdict(picked), "parse": parse,
		"outcome": strict, "lenient": harness.lenient(strict, case.gold),
		"confirm": confirm, "confirm_lenient": harness.lenient(confirm, case.gold),
	}


def run(kind, arm, root, config, expect):
	cases = load_cases(kind, root)
	selected = selected_cases(cases, arm)
	if not selected:
		print(f"No eligible cases for {arm}. Visible list sizes: {sorted({len(c.visible) for c in cases})}.")
		return
	out = root / "runs" / f"round-b-{arm}"
	out.mkdir(parents=True, exist_ok=False)
	started = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
	header = (f"{config}\n{expect}\nmodel {liquid.MODEL}, repeats {REPEATS}, "
	          f"confidence floor {MIN_CONFIDENCE}, {len(selected)} eligible cases, started {started}")
	(out / "config.txt").write_text(header + "\n", encoding="utf-8")
	print(header, flush=True)
	prefix = f"pick/round-b/{kind}/{arm}/{started}/"
	rows = []
	client = liquid.LiquidClient(url=f"https://{liquid.HOST}", day_limit=liquid.DAY_LIMIT_USD)
	try:
		for idx, case in enumerate(selected, 1):
			items = shortlist(case.label["utterance"], case.visible) if arm == "narrow+decider" else case.visible
			body = choice_request(kind, case.evidence({item.id for item in items}), items,
			                      case.label.get("task_record_thread_id"))
			for attempt in range(1, REPEATS + 1):
				stem = f"{case.label['id']}-r{attempt}"
				start = time.perf_counter()
				record = client.call("decisions", body, label=f"{prefix}{idx}/{attempt}", cache_record=False)
				wall_ms = round((time.perf_counter() - start) * 1000, 2)
				write_json(out / f"{stem}.raw.json", record)
				if record["status"] != 200:
					raise liquid.CallFailed(record)
				timing = record["timing"]
				answer = record["response"]["answers"].get("pick")
				row = {
					"case": case.label["id"], "slice": case.label["slice"], "arm": arm, "attempt": attempt,
					"utterance": case.label["utterance"], "gold": case.gold, "shortlist": [asdict(i) for i in items],
					"request_ms": round(timing["ms"] + (timing["connect_ms"] or 0), 2),
					"wall_ms": wall_ms, "model_call": True,
					**score(case, items, answer, kind),
				}
				write_json(out / f"{stem}.json", row)
				rows.append(row)
			if idx % 10 == 0 or idx == len(selected):
				print(f"Scored {idx}/{len(selected)} cases", flush=True)
	finally:
		client.close()
		entries = [entry for entry in liquid.entries() if entry["label"].startswith(prefix)]
		calls = {"attempts": len(entries), "spend_usd": sum(e["cost"] for e in entries),
		         "status_counts": dict(Counter(e["status"] for e in entries))}
		write_json(out / "calls.json", calls)
		print(json.dumps(calls), flush=True)
	summary = harness.summarise(rows, [arm])
	pass_bar = project_harness.pass_bar if kind == "project" else harness.pass_bar
	write_json(out / "summary.json", {"summary": summary, "pass_bar": pass_bar(summary, arm)})


def main():
	parser = argparse.ArgumentParser()
	parser.add_argument("--kind", choices=("thread", "project"), required=True)
	parser.add_argument("--arm", choices=ARMS, required=True)
	parser.add_argument("--root", type=Path, required=True)
	parser.add_argument("--config-line", required=True)
	parser.add_argument("--expect", required=True)
	args = parser.parse_args()
	run(args.kind, args.arm, args.root, args.config_line, args.expect)


if __name__ == "__main__":
	main()
