"""Derive every number in docs/voice/local-decider.md. No network, no GPU.

    python -m voice.local_decider.report

Reads the bench output under $VOICE_HOME/evidence/local-decider/, the committed scenario results, and the prototype's
clip run records under $VOICE_HOME. Writes voice/local_decider/results/report.json, which holds aggregates, scenario
ids and clip file names only, never a transcript.
"""

import json
import statistics
from collections import Counter, defaultdict
from pathlib import Path

from voice.local_decider.bench import CACHES, OUT, compact, load, same, sequences
from voice.local_decider.client import VOICE_HOME
from voice.segmentation.cursor import words
from voice.segmentation.report import dist
from voice.segmentation.run import RESULTS

REPORT = Path(__file__).resolve().parent / "results" / "report.json"
BUCKETS = [(0, 250), (250, 500), (500, 1000), (1000, 2000), (2000, 4000), (4000, 10 ** 9)]
REFERENCE = "ub8192"
REAL = [f"{i:02d}_{n}" for i, n in enumerate(["single", "two_requests", "dependent", "correction", "topic_change",
                                              "not_a_request", "implicit", "cancel", "run_on", "noisy"], 1)]
CLOUD_CLIPS = VOICE_HOME / "evidence" / "hybrid" / "run-h-c"
D1_CLIPS = VOICE_HOME / "evidence" / "local-decider" / "clips"


def sdist(values):
    """dist() for seconds, to the millisecond."""
    values = sorted(values)
    return {"n": len(values), "median": round(statistics.median(values), 3),
            "p95": round(values[min(len(values) - 1, int(0.95 * len(values)))], 3),
            "min": round(values[0], 3), "max": round(values[-1], 3)}


def rows(path):
    return [json.loads(line) for line in Path(path).read_text(encoding="utf-8").splitlines()]


def qtype(name):
    """The question's kind, from its name: `s3_kind` is `kind`, `d1_same_as` is `same_as`."""
    return name.split("_", 1)[1] if "_" in name else name


def fit(points):
    """Least-squares ms = a + b * tokens."""
    xs, ys = zip(*points)
    mx, my = statistics.mean(xs), statistics.mean(ys)
    b = sum((x - mx) * (y - my) for x, y in points) / sum((x - mx) ** 2 for x in xs)
    return {"intercept_ms": round(my - b * mx, 1), "ms_per_1k_tokens": round(b * 1000, 1)}


def latency(data):
    out = {"all": dist([r["ms"] for r in data]), "fit": fit([(r["input_tokens"], r["ms"]) for r in data]),
           "input_tokens": dist([r["input_tokens"] for r in data]), "buckets": {}}
    for lo, hi in BUCKETS:
        sel = [r["ms"] for r in data if lo <= r["input_tokens"] < hi]
        if sel:
            out["buckets"][f"{lo}-{hi if hi < 10 ** 9 else 'max'}"] = dist(sel)
    return out


def diff_answers(a, b):
    """Decision flips between two full answer sets, and the largest probability move."""
    flips, moved = 0, 0.0
    for name, x in a.items():
        y = b.get(name)
        if not isinstance(y, dict):
            continue
        if not same(compact({name: x})[name], compact({name: y})[name]):
            flips += 1
        if x.get("type") == "choice":
            moved = max(moved, max(abs(p - y["probabilities"].get(k, 0)) for k, p in x["probabilities"].items()))
        else:
            moved = max(moved, abs(x["noul"] - y["noul"]))
    return flips, moved


def replay_section(tag):
    path = OUT / f"replay-{tag}.jsonl"
    if not path.exists():
        return None
    data = [r for r in rows(path) if r["status"] == 200]
    servers = json.loads((OUT / f"replay-{tag}-servers.json").read_text(encoding="utf-8")) \
        if (OUT / f"replay-{tag}-servers.json").exists() else []
    by_cfg = defaultdict(list)
    for r in data:
        by_cfg[r["config"]].append(r)
    ref = {r["file"]: r["answers"] for r in by_cfg.get(REFERENCE, []) if r["round"] == 0}
    out = {}
    for cfg, rs in by_cfg.items():
        first = {r["file"]: r["answers"] for r in rs if r["round"] == 0}
        flips = moved = questions = 0
        for f, answers in first.items():
            if f in ref:
                fl, mv = diff_answers(ref[f], answers)
                flips, moved, questions = flips + fl, max(moved, mv), questions + len(answers)
        later = [r for r in rs if r["round"] > 0]
        repeat_flips = sum(sum(not same(v, compact(first[r["file"]])[k]) for k, v in r["answers"].items())
                           for r in later if r["file"] in first)
        srv = [s for s in servers if s["config"] == cfg]
        out[cfg] = {
            "calls": len(rs), "rounds": len({r["round"] for r in rs}),
            "errors": sum(s["errors"] for s in srv) if srv else None,
            "latency": latency(rs),
            "per_round_median_ms": [round(statistics.median(r["ms"] for r in rs if r["round"] == k), 1)
                                    for k in sorted({r["round"] for r in rs})],
            "vs_reference": {"questions": questions, "decision_flips": flips, "max_probability_move": round(moved, 4)},
            "repeat_flips_vs_round0": repeat_flips,
            "servers": [{k: s[k] for k in ("round", "startup_s", "idle_mib", "loaded_mib", "peak_mib", "wall_s",
                                           "calls_per_s")} for s in srv]}
    return out


def agreement_section(tag="ubsweep"):
    """d1 at the reference config, round 0, against pplx's recorded answer to the same request."""
    path = OUT / f"replay-{tag}.jsonl"
    if not path.exists():
        return None
    hybrid_files = {f for seqs in sequences().values() for files in seqs.values() for f in files}
    tallies = {"all": defaultdict(Counter), "hybrid": defaultdict(Counter)}
    for r in rows(path):
        if r["config"] != REFERENCE or r["round"] != 0 or r["status"] != 200:
            continue
        pplx = compact(load(CACHES[r["source"]] / r["file"])["response"]["answers"])
        d1 = compact(r["answers"])
        for name, value in pplx.items():
            if name not in d1:
                continue
            agree = same(value, d1[name])
            for scope in ("all", "hybrid") if r["file"] in hybrid_files else ("all",):
                tallies[scope][qtype(name)]["n"] += 1
                tallies[scope][qtype(name)]["agree"] += agree
    return {scope: {t: {"n": c["n"], "agree": c["agree"], "rate": round(c["agree"] / c["n"], 3)}
                    for t, c in sorted(by.items())} for scope, by in tallies.items()}


Q_BUCKETS = [(0, 3), (3, 6), (6, 10), (10, 20), (20, 40)]


def q_latency(data):
    """Latency by the number of questions in the request, which is the size packing acts on."""
    out = {}
    for lo, hi in Q_BUCKETS:
        sel = [r["ms"] for r in data if lo <= r["questions"] < hi]
        if sel:
            out[f"{lo}-{hi}"] = dist(sel)
    return out


def llama_reference():
    """llama.cpp's throughput on its own replay, at the flags client.py ships (batch 2048)."""
    path = OUT / "replay-ubsweep-servers.json"
    if not path.exists():
        return None
    server = next((s for s in load(path) if s["config"] == "ub2048" and s["round"] == 0), None)
    if server is None:
        return None
    questions = sum(r["questions"] for r in rows(OUT / "replay-ubsweep.jsonl")
                    if r["config"] == "ub2048" and r["round"] == 0)
    return {"startup_s": server["startup_s"], "peak_mib": server["peak_mib"], "wall_s": server["wall_s"],
            "calls_per_s": round(server["calls"] / server["wall_s"], 2),
            "questions_per_s": round(questions / server["wall_s"], 2)}


def without_buckets(section):
    """A latency block without its token buckets, which for a packed server count its own tokens, not request size."""
    return {k: v for k, v in section.items() if k != "buckets"}


def packing_section():
    """The largest requests at several set sizes: the state is read once per group, so the reported input tokens fit
    `groups * state + question tokens` exactly."""
    path = OUT / "packing.json"
    if not path.exists():
        return None
    data = load(path)
    calls = [r for r in data if "file" in r]
    by_file = defaultdict(list)
    for r in calls:
        by_file[r["file"]].append(r)
    out = {"requests": len(by_file), "concurrent": next((r for r in data if "concurrent_s" in r), None),
           "per_request": []}
    for f, rs in by_file.items():
        rs.sort(key=lambda r: -r["groups"])
        big, small = rs[0], rs[-1]
        state = (big["input_tokens"] - small["input_tokens"]) / (big["groups"] - small["groups"])
        out["per_request"].append({
            "file": f, "questions": big["questions"], "state_tokens": round(state),
            "question_tokens": round(small["input_tokens"] - small["groups"] * state),
            "rows": [{"groups": r["groups"], "set_size": r["set_size"], "ms": r["ms"],
                      "input_tokens": r["input_tokens"]} for r in rs]})
    return out


def mistralrs_section(tag="mistralrs"):
    """mistral.rs rows: latency, throughput, VRAM, agreement with pplx, and flips against llama.cpp's answers.

    The 94 hybrid requests are the segmenter's own calls; the rest of the corpus is the replay set llama.cpp ran.
    Answer diffs use the batch-8192 round-0 llama.cpp answers as the reference, which are byte-identical to batch
    2048's, and only over files both runs hold.
    """
    path = OUT / f"replay-{tag}.jsonl"
    if not path.exists():
        return None
    data = [r for r in rows(path) if r["status"] == 200]
    servers_path = OUT / f"replay-{tag}-servers.json"
    servers = json.loads(servers_path.read_text(encoding="utf-8")) if servers_path.exists() else []
    hybrid_files = {f for seqs in sequences().values() for files in seqs.values() for f in files}
    llama = {r["file"]: r for r in rows(OUT / "replay-ubsweep.jsonl")
             if r["config"] == "ub8192" and r["round"] == 0 and r["status"] == 200}
    packed = {r["file"]: r for g in load(OUT / "transformers.json")["groups"].values() for r in g} \
        if (OUT / "transformers.json").exists() else {}
    out = {}
    for cfg in sorted({r["config"] for r in data}):
        first = {r["file"]: r for r in data if r["config"] == cfg and r["round"] == 0}
        shared = [r for f, r in first.items() if f in llama]
        hybrid = [r for f, r in first.items() if f in hybrid_files]
        srv = [s for s in servers if s["config"] == cfg]
        tallies = {scope: defaultdict(Counter) for scope in ("all", "hybrid")}
        flips_llama, questions, moved = 0, 0, 0.0
        for name, r in first.items():
            pplx = compact(load(CACHES[r["source"]] / name)["response"]["answers"])
            mine = compact(r["answers"])
            for q, value in pplx.items():
                if q in mine:
                    for scope in ("all", "hybrid") if name in hybrid_files else ("all",):
                        tallies[scope][qtype(q)]["n"] += 1
                        tallies[scope][qtype(q)]["agree"] += same(value, mine[q])
            if name in llama:
                fl, mv = diff_answers(llama[name]["answers"], r["answers"])
                flips_llama, moved = flips_llama + fl, max(moved, mv)
                questions += len(r["answers"])
        tf = [{"file": f, "questions": packed[f]["questions"],
               "set16_flips_vs_llama": packed[f]["set16"]["flips_vs_llama"],
               "set16_flips_vs_pplx": packed[f]["set16"]["flips_vs_pplx"],
               "llama_flips_vs_pplx": packed[f]["llama_vs_pplx_flips"],
               "set16_input_tokens": packed[f]["set16"]["input_tokens"], "llama_tokens": packed[f]["llama_tokens"],
               "mine_flips_vs_llama": diff_answers(llama[f]["answers"], first[f]["answers"])[0]
               if f in first and f in llama else None,
               "mine_flips_vs_pplx": diff_answers(load(CACHES[first[f]["source"]] / f)["response"]["answers"],
                                                 first[f]["answers"])[0] if f in first else None,
               "mine_input_tokens": first[f]["input_tokens"] if f in first else None}
              for f in packed]
        out[cfg] = {
            "calls": len(data) // max(1, len({r["round"] for r in data})), "errors": sum(s["errors"] for s in srv),
            "latency": without_buckets(latency(list(first.values()))),
            "hybrid_latency": without_buckets(latency(hybrid)),
            "shared_latency": without_buckets(latency(shared)), "shared_calls": len(shared),
            "llama_shared_latency": without_buckets(latency([llama[f] for f in first if f in llama])),
            "llama_hybrid_latency": without_buckets(latency([llama[f] for f in first if f in llama and f in hybrid_files])),
            "by_questions": {"mine": q_latency(shared),
                             "llama": q_latency([llama[f] for f in first if f in llama])},
            "hybrid_calls": len(hybrid), "hybrid_questions": sum(len(r["answers"]) for r in hybrid),
            "servers": [{k: s[k] for k in ("round", "startup_s", "idle_mib", "loaded_mib", "peak_mib", "wall_s",
                                           "calls_per_s", "questions_per_s", "paged_attn")} for s in srv],
            "agreement": {scope: {"answers": sum(c["n"] for c in by.values()),
                                  "rate": round(sum(c["agree"] for c in by.values())
                                                / max(1, sum(c["n"] for c in by.values())), 3),
                                  "by_type": {t: {"n": c["n"], "agree": c["agree"], "rate": round(c["agree"] / c["n"], 3)}
                                              for t, c in sorted(by.items())}}
                          for scope, by in tallies.items()},
            "vs_llama": {"questions": questions, "decision_flips": flips_llama,
                         "max_probability_move": round(moved, 4)},
            "vs_transformers_totals": {k: sum(r[k] for r in tf if r.get(k) is not None)
                                       for k in ("questions", "set16_flips_vs_llama", "set16_flips_vs_pplx",
                                                 "llama_flips_vs_pplx", "mine_flips_vs_llama", "mine_flips_vs_pplx")},
            "vs_transformers_tokens": [sum(r["set16_input_tokens"] for r in tf),
                                       sum(r["llama_tokens"] for r in tf),
                                       sum(r["mine_input_tokens"] or 0 for r in tf)],
            "llama_reference": llama_reference(),
        }
    return out


def reuse_section():
    path = OUT / "reuse.jsonl"
    if not path.exists():
        return None
    data = rows(path)
    out = {}
    for scope in ("all", "original", "heldout", "clips"):
        rs = [r for r in data if scope == "all" or r["set"] == scope]
        errors = Counter(qtype(n) for r in rs for n in r["reuse_errors"])
        reused = Counter(qtype(n) for r in rs for n in r["reused_names"])
        out[scope] = {
            "calls": len(rs), "calls_skipped": sum(r["asked"] == 0 for r in rs),
            "questions_full": dist([r["questions"] for r in rs]), "questions_asked": dist([r["asked"] for r in rs]),
            "questions_total": [sum(r["questions"] for r in rs), sum(r["asked"] for r in rs)],
            "tokens_full": dist([r["full_tokens"] for r in rs]), "tokens_reuse": dist([r["reuse_tokens"] for r in rs]),
            "tokens_total": [sum(r["full_tokens"] for r in rs), sum(r["reuse_tokens"] for r in rs)],
            "ms_full": dist([r["full_ms"] for r in rs]), "ms_reuse": dist([r["reuse_ms"] for r in rs]),
            "reused_answers": sum(reused.values()), "reuse_errors": sum(errors.values()),
            "reuse_error_rate": round(sum(errors.values()) / max(1, sum(reused.values())), 4),
            "errors_by_type": {t: [errors[t], reused[t]] for t in sorted(reused)}}
    return out


def scenario_runs():
    """Pass counts and final segments, d1 against the committed pplx runs (stream-h, stream-h-heldout)."""
    out = {}
    for set_name, suffix in (("original", ""), ("heldout", "-heldout")):
        base = RESULTS / f"stream-h{suffix}"
        for variant in ("d1", "d1-reuse"):
            run = RESULTS / f"stream-h{suffix}-{variant}"
            if not (run / "summary.json").exists():
                continue
            pplx = {r["scenario"]: r for r in load(base / "summary.json")}
            mine = {r["scenario"]: r for r in load(run / "summary.json")}
            changed, rows_ = 0, {}
            for sid, r in mine.items():
                a, b = load(base / f"{sid}.json"), load(run / f"{sid}.json")
                seg_a = [s["text"] for s in a["decision_points"][-1]["segments"]]
                seg_b = [s["text"] for s in b["decision_points"][-1]["segments"]]
                changed += seg_a != seg_b
                ms = [c["ms"] for p in b["decision_points"] for c in p["calls"]]
                rows_[sid] = {"pplx": pplx[sid]["pass"], "d1": r["pass"], "calls": [pplx[sid]["calls"], r["calls"]],
                              "final_segments_changed": seg_a != seg_b, "failures": r["failures"],
                              "premature": [pplx[sid]["premature"], r["premature"]], "max_ms": max(ms) if ms else 0}
            out[f"{set_name}/{variant}"] = {
                "pass": [sum(r["pass"] for r in pplx.values()), sum(r["pass"] for r in mine.values())],
                "premature": [sum(r["premature"] for r in pplx.values()), sum(r["premature"] for r in mine.values())],
                "calls": [sum(r["calls"] for r in pplx.values()), sum(r["calls"] for r in mine.values())],
                "final_segments_changed": changed, "scenarios": len(mine), "per_scenario": rows_}
    return out


def lag(record):
    """Per word of the final transcript: seconds from its first partial to the end of the first partial decision
    point whose transcript holds it. Words no partial decision point covered are the uncovered tail."""
    updates = [e for e in record["timeline"] if e["kind"] == "transcript_update" and not e["is_final"]]
    points = [e for e in record["timeline"] if e["kind"] == "decision_point" and not e["is_final"]
              and e["source"] == "update"]
    count_at = {u["revision"]: len(words(u["text"])) for u in updates}
    n = len(words(record["final_transcript"]))
    lags, uncovered = [], 0
    for i in range(n):
        heard = next((u["t"] for u in updates if count_at[u["revision"]] > i), None)
        done = next((p["t"] for p in points if count_at.get(p["revision"], 0) > i), None)
        if heard is None or done is None:
            uncovered += 1
        else:
            lags.append(done - heard)
    return lags, uncovered, n


def clip_run(run):
    from voice.prototype.replay import grade
    expected = json.loads((Path(__file__).resolve().parents[1] / "prototype" / "expected.json").read_text("utf-8"))
    per, lags, during, after = {}, [], [], []
    for stem in REAL:
        path = run / f"{stem}.json"
        if not path.exists():
            continue
        record = load(path)
        if "error" in record:
            per[stem] = {"error": record["error"][:200]}
            continue
        graded = grade(expected[stem], record["tasks"], record["dispatch_log"])
        clip_lags, uncovered, n = lag(record)
        lags += clip_lags
        for p in (e for e in record["timeline"] if e["kind"] == "decision_point"):
            for c in p["calls"]:
                (during if p["started"] < record["audio_end"] else after).append(c["ms"])
        m = record["metrics"]
        per[stem] = {"pass": graded["pass"], "premature": graded["premature_dispatches"],
                     "first_reply_audio_s": round(m["first_reply_audio_after_audio_end_s"], 3),
                     "snapshot_ready_s": round(m["snapshot_ready_after_audio_end_s"], 3),
                     "calls": sum(len(e["calls"]) for e in record["timeline"] if e["kind"] == "decision_point"),
                     "words": n, "uncovered_tail_words": uncovered,
                     "lag_median_s": round(statistics.median(clip_lags), 3) if clip_lags else None,
                     "vram_mib": (record.get("config") or {}).get("vram_mib_after_load")}
    ok = [r for r in per.values() if "pass" in r]
    if not ok:
        return None
    return {"clips": len(per), "errors": len(per) - len(ok), "pass": sum(r["pass"] for r in ok),
            "premature": sum(r["premature"] for r in ok),
            "first_reply_audio_s": sdist([r["first_reply_audio_s"] for r in ok]),
            "snapshot_ready_s": sdist([r["snapshot_ready_s"] for r in ok]),
            "lag_s": sdist(lags) if lags else None, "uncovered_tail_words": [r["uncovered_tail_words"] for r in ok],
            "call_ms_during_speech": dist(during) if during else None,
            "call_ms_after_speech": dist(after) if after else None,
            "vram_mib": sorted({r["vram_mib"] for r in ok if r["vram_mib"]}), "per_clip": per}


def clips_section():
    runs = {"cloud": CLOUD_CLIPS}
    if D1_CLIPS.is_dir():
        runs.update({d.name: d for d in sorted(D1_CLIPS.iterdir()) if d.is_dir()})
    return {name: clip_run(run) for name, run in runs.items() if run.is_dir()}


def bargein_section():
    out = {}
    for path in sorted(OUT.glob("bargein-*.json")):
        data = load(path)
        rs = [r for r in data["rows"] if r["status"] == "response.done"]
        loaded, idle = [r for r in rs if r["d1_load"]], [r for r in rs if not r["d1_load"]]
        out[path.stem] = {
            "both_loaded_mib": data["both_loaded_mib"], "trials": len(rs),
            "first_audio_s": {"with_d1": sdist([r["first_audio_s"] for r in loaded]),
                              "without": sdist([r["first_audio_s"] for r in idle])},
            "reply_s": {"with_d1": sdist([r["reply_s"] for r in loaded]), "without": sdist([r["reply_s"] for r in idle])},
            "reply_audio_s": sdist([r["reply_audio_s"] for r in rs]),
            "d1_ms_while_speaking": dist([m for r in loaded for m in r["d1_ms"]]),
            "peak_mib": max(r["peak_mib"] for r in rs)}
    return out


def promptcache_section():
    """Reuse within one request (questions after the first) and across calls (the first question), per flag set."""
    path = OUT / "promptcache.json"
    if not path.exists():
        return None
    out = {}
    for name, v in load(path).items():
        first, repeat = [c for c in v["slowest"] if c["repeat"] == 0], [c for c in v["slowest"] if c["repeat"] == 1]
        out[name] = {
            "sequences": v["sequences"],
            "slowest_within_request_cached_tokens": sum(c["cached_later_questions"] for c in first),
            "slowest_across_call_cached_tokens": sum(c["cached_first_question"] or 0 for c in v["slowest"]),
            "slowest_identical_repeat_cached_tokens": sum(c["cached_later_questions"] + (c["cached_first_question"] or 0)
                                                          for c in repeat),
            "slowest_prompt_tokens": sum(c["prompt_tokens"] for c in v["slowest"]),
            "slowest_shared_prefix_tokens": sorted({c["shared_prefix_median"] for c in first}),
            "slowest_ms": dist([c["ms"] for c in v["slowest"]])}
    return out


def transformers_section():
    path = OUT / "transformers.json"
    if not path.exists():
        return None
    data = load(path)
    out = {k: v for k, v in data.items() if k != "groups"}
    for group, rows in data["groups"].items():
        out[group] = {
            "llama_ms": [r["llama_ms"] for r in rows], "packed16_ms": [r["set16"]["ms"] for r in rows],
            "one_at_a_time_ms": [r["set1"]["ms"] for r in rows],
            "llama_tokens": [r["llama_tokens"] for r in rows], "packed16_tokens": [r["set16"]["input_tokens"] for r in rows],
            "questions": [r["questions"] for r in rows],
            "flips_vs_pplx": {"llama": sum(r["llama_vs_pplx_flips"] for r in rows),
                              "packed16": sum(r["set16"]["flips_vs_pplx"] for r in rows),
                              "one_at_a_time": sum(r["set1"]["flips_vs_pplx"] for r in rows)},
            "flips_vs_llama": {"packed16": sum(r["set16"]["flips_vs_llama"] for r in rows),
                               "one_at_a_time": sum(r["set1"]["flips_vs_llama"] for r in rows)},
            "answers": sum(r["questions"] for r in rows)}
    return out


def main():
    report = {
        "replay_ubsweep": replay_section("ubsweep"),
        "replay_flags": replay_section("flags"),
        "promptcache": promptcache_section(),
        "transformers": transformers_section(),
        "reuse": reuse_section(),
        "agreement": agreement_section(),
        "mistralrs": mistralrs_section(),
        "packing": packing_section(),
        "scenario_runs": scenario_runs(),
        "clips": clips_section(),
        "bargein": bargein_section(),
    }
    REPORT.parent.mkdir(exist_ok=True)
    REPORT.write_text(json.dumps(report, indent=1) + "\n", encoding="utf-8")
    print(f"wrote {REPORT}")


if __name__ == "__main__":
    main()
