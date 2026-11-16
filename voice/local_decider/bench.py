"""Bench d1-2B on llama.cpp and mistral.rs against the segmenter's recorded decider requests. GPU: run inside the
lease and quiet_run.

    python -m voice.local_decider.bench replay [--configs ub8192,ub2048,ub512,ub8192-nofa] [--rounds 3]
    python -m voice.local_decider.bench replay --configs mr16,mr1 --tag mistralrs [--paged-attn on]
    python -m voice.local_decider.bench packing [--requests 3] [--sizes default,8,4,2,1] [--concurrent]
    python -m voice.local_decider.bench promptcache [--variants default,prompt-cache,...] [--rounds 3]
    python -m voice.local_decider.bench reuse
    python -m voice.local_decider.bench bargein CLIP.wav [--lfm-ctx 32768]

replay sends every recorded decisions request once per config per round, configs alternating, and records latency,
input tokens, the answers, startup time and VRAM. `mr` configs run mistral.rs and name the `set_size` sent with each
request (`mr16` is the model's own default, `mr1` is one question per state read); the other config names are
llama.cpp flag sets. packing sends the largest requests at several set sizes and, with --concurrent, two requests at
once. promptcache runs debug-logged servers under several prompt-reuse flag
sets and counts the tokens llama.cpp reports reusing, within one request and across calls. reuse replays the same
sequences twice per request, the full request
and the answer-reuse request, and compares the reused answers with the full ones. bargein streams a clip into LFM3,
asks for a spoken reply, and sends d1 requests while LFM3 speaks, alternating with replies that have no d1 load.

Everything is written under $VOICE_HOME/evidence/local-decider/, since the prototype's requests hold Mason's
transcripts.
"""

import argparse
import asyncio
import json
import re
import statistics
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from voice.local_decider.client import D1Client, MistralRsServer, VOICE_HOME, d1_server
from voice.local_decider.reuse import ReusingClient

REPO = Path(__file__).resolve().parents[1]
CACHES = {"prototype": VOICE_HOME / "evidence" / "prototype" / "decider-cache", "fork": REPO / "segmentation" / "cache"}
RUNS = {"original": REPO / "segmentation" / "results" / "stream-h",
        "heldout": REPO / "segmentation" / "results" / "stream-h-heldout",
        "clips": VOICE_HOME / "evidence" / "hybrid" / "run-h-c"}
OUT = VOICE_HOME / "evidence" / "local-decider"
CONFIGS = {
    "ub8192": ("-b", "8192", "-ub", "8192", "-fa", "on"),
    "ub4096": ("-b", "8192", "-ub", "4096", "-fa", "on"),
    "ub2048": ("-b", "8192", "-ub", "2048", "-fa", "on"),
    "ub512": ("-b", "8192", "-ub", "512", "-fa", "on"),
    "ub8192-nofa": ("-b", "8192", "-ub", "8192", "-fa", "off"),
    "ub8192-c16k": ("-b", "8192", "-ub", "8192", "-fa", "on", "-c", "16384"),
}
# mistral.rs config name to the `set_size` sent with each request. None is the model's own default, 16 questions per
# state read (mistralrs-core/src/gguf/lfm3_d1_config.rs, DEFAULT_SET_SIZE).
MISTRALRS = {"mr16": None, "mr8": 8, "mr4": 4, "mr2": 2, "mr1": 1}


def gpu_used_mib():
    out = subprocess.run(["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader,nounits"],
                         capture_output=True, text=True, check=True).stdout
    return int(out.split()[0])


class VramPeak:
    """Polls total GPU memory in the background. Per-process figures read N/A under WDDM (measured)."""

    def __init__(self, period_s=0.25):
        self.period_s, self.peak, self._stop = period_s, 0, threading.Event()

    def __enter__(self):
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        return self

    def _run(self):
        while not self._stop.is_set():
            self.peak = max(self.peak, gpu_used_mib())
            self._stop.wait(self.period_s)

    def __exit__(self, *exc):
        self._stop.set()
        self._thread.join()


def load(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def corpus():
    """Every recorded decisions request with a 200 answer, as (source, file name, record), oldest first per source."""
    rows = []
    for source, directory in CACHES.items():
        records = [(p.name, load(p)) for p in sorted(directory.glob("decisions-*.json"))]
        records = [(n, r) for n, r in records if r["status"] == 200 and isinstance((r["response"] or {}).get("answers"), dict)]
        rows += [(source, n, r) for n, r in sorted(records, key=lambda nr: nr[1]["timing"]["started_at"] or "")]
    return rows


def sequences():
    """The hybrid's request sequences, in call order, per utterance: {set: {utterance: [cache file names]}}."""
    out = {}
    for name, run in RUNS.items():
        seqs = {}
        for path in sorted(run.glob("*.json")):
            if path.name == "summary.json":
                continue
            record = load(path)
            if name == "clips":
                points = [e for e in record.get("timeline", []) if e["kind"] == "decision_point"]
            else:
                points = record.get("decision_points", [])
            files = [c["file"] for p in points for c in p["calls"] if c["status"] == 200]
            if files:
                seqs[path.stem] = files
        out[name] = seqs
    return out


def find(file_name):
    for directory in CACHES.values():
        if (directory / file_name).exists():
            return load(directory / file_name)
    raise LookupError(file_name)


def compact(answers):
    """The decision each answer carries: the chosen option, or P(true) rounded to 3 places."""
    return {k: (a.get("choice") if a.get("type") == "choice" else round(a.get("noul", -1), 3))
            for k, a in answers.items() if isinstance(a, dict)}


def started(cfg, log, paged_attn="off"):
    """The server a config names: mistral.rs when MISTRALRS holds it, else llama.cpp's flags from CONFIGS."""
    if cfg in MISTRALRS:
        return MistralRsServer(log_path=log, paged_attn=paged_attn)
    return d1_server(log_path=log, flags=CONFIGS[cfg])


def replay(args):
    rows_path, servers_path = OUT / f"replay-{args.tag}.jsonl", OUT / f"replay-{args.tag}-servers.json"
    items = corpus()
    servers = []
    with rows_path.open("w", encoding="utf-8") as rows:
        for rnd in range(args.rounds):
            for cfg in args.configs:
                idle = gpu_used_mib()
                log = OUT / "logs" / f"replay-{args.tag}-{cfg}-paged-{args.paged_attn}-r{rnd}.log"
                with started(cfg, log, args.paged_attn) as server:
                    loaded = gpu_used_mib()
                    client = D1Client(f"http://127.0.0.1:{server.port}", set_size=MISTRALRS.get(cfg))
                    t0 = time.perf_counter()
                    errors = 0
                    with VramPeak() as vram:
                        for source, name, record in items:
                            out = client.call("decisions", record["request"], record["label"])
                            ok = out["status"] == 200 and isinstance(out["response"].get("answers"), dict)
                            errors += not ok
                            row = {"config": cfg, "round": rnd, "source": source, "file": name,
                                   "server": "llama.cpp" if cfg in CONFIGS else "mistral.rs",
                                   "set_size": MISTRALRS.get(cfg), "paged_attn": args.paged_attn,
                                   "chars": len(json.dumps(record["request"])), "questions": len(record["request"]["questions"]),
                                   "status": out["status"], "ms": round(out["timing"]["ms"], 2),
                                   "input_tokens": (out["response"].get("usage") or {}).get("input_tokens")}
                            if ok:
                                row["answers"] = out["response"]["answers"] if rnd == 0 else compact(out["response"]["answers"])
                            else:
                                row["error"] = json.dumps(out["response"])[:300]
                            rows.write(json.dumps(row) + "\n")
                    wall = time.perf_counter() - t0
                servers.append({"config": cfg, "round": rnd, "startup_s": round(server.startup_s, 3), "idle_mib": idle,
                                "loaded_mib": loaded, "peak_mib": vram.peak, "calls": len(items), "errors": errors,
                                "wall_s": round(wall, 2), "calls_per_s": round(len(items) / wall, 2),
                                "questions_per_s": round(sum(len(r["request"]["questions"]) for _, _, r in items) / wall, 2),
                                "paged_attn": args.paged_attn})
                print(json.dumps(servers[-1]), flush=True)
    servers_path.write_text(json.dumps(servers, indent=1) + "\n", encoding="utf-8")


def packing(args):
    """The largest recorded requests at several set sizes, plus two requests at once.

    mistral.rs reads the state once per group of `set_size` questions (DecisionPrompt::prepare, step_by(size)), so the
    input tokens it reports should fall as the group count falls while the question count stays. It runs one decision
    sequence at a time (pipeline/mod.rs, ModelCategory::Decision), so two requests at once should take about as long as
    the same two back to back.
    """
    items = corpus()
    picked = sorted(items, key=lambda row: -len(row[2]["request"]["questions"]))[:args.requests]
    sizes = [None if s == "default" else int(s) for s in args.sizes.split(",")]
    rows = []
    with MistralRsServer(log_path=OUT / "logs" / f"packing-paged-{args.paged_attn}.log",
                         paged_attn=args.paged_attn) as server:
        url = f"http://127.0.0.1:{server.port}"
        warm = D1Client(url)
        warm.call("decisions", picked[-1][2]["request"], "warmup")
        warm.close()
        for source, name, record in picked:
            questions = len(record["request"]["questions"])
            for size in sizes:
                client = D1Client(url, set_size=size)
                out = client.call("decisions", record["request"], "packing")
                client.close()
                row = {"source": source, "file": name, "questions": questions, "set_size": size,
                       "groups": -(-questions // (size or 16)), "ms": round(out["timing"]["ms"], 1),
                       "input_tokens": (out["response"].get("usage") or {}).get("input_tokens"),
                       "status": out["status"]}
                rows.append(row)
                print(json.dumps(row), flush=True)
        if args.concurrent:
            largest = [record["request"] for _, _, record in picked[:2]]
            back = time.perf_counter()
            for body in largest:
                client = D1Client(url)
                client.call("decisions", body, "back-to-back")
                client.close()
            back_s = time.perf_counter() - back

            def once(body):
                client = D1Client(url)
                out = client.call("decisions", body, "concurrent")
                client.close()
                return round(out["timing"]["ms"], 1)

            both = time.perf_counter()
            with ThreadPoolExecutor(2) as pool:
                ms = list(pool.map(once, largest))
            both_s = time.perf_counter() - both
            print(json.dumps({"back_to_back_s": round(back_s, 3), "concurrent_s": round(both_s, 3), "ms": ms,
                              "startup_s": round(server.startup_s, 2)}), flush=True)
            rows.append({"back_to_back_s": round(back_s, 3), "concurrent_s": round(both_s, 3), "ms": ms,
                         "startup_s": round(server.startup_s, 2)})
    (OUT / "packing.json").write_text(json.dumps(rows, indent=1) + "\n", encoding="utf-8")


NEW_PROMPT = re.compile(r"task (\d+) \| new prompt, .*task\.n_tokens = (\d+)")
CACHED = re.compile(r"task (\d+) \| cached n_tokens = (\d+)")
SIMILAR = re.compile(r"checking sim = [\d.]+ \((\d+)/(\d+)\)")
REUSE_FLAGS = {
    "default": (),
    "prompt-cache": ("--cache-ram", "4096"),
    "checkpoints": ("--checkpoint-min-step", "0", "--ctx-checkpoints", "128"),
    "prompt-cache+checkpoints": ("--cache-ram", "4096", "--checkpoint-min-step", "0", "--ctx-checkpoints", "128"),
    "prompt-cache+checkpoints+swa-full": ("--cache-ram", "4096", "--checkpoint-min-step", "0",
                                          "--ctx-checkpoints", "128", "--swa-full"),
    "prompt-cache+cache-reuse": ("--cache-ram", "4096", "--cache-reuse", "16"),
}


def server_tasks(log):
    """Every prompt the server evaluated, in order: its tokens, the tokens it reused, and its longest common prefix
    with what the slot held before it (the "checking sim" line the server logs while picking the slot)."""
    tasks, lcp = [], None
    for line in log.read_text(encoding="utf-8", errors="replace").splitlines():
        if m := SIMILAR.search(line):
            lcp = int(m.group(1))
        elif m := NEW_PROMPT.search(line):
            tasks.append({"task": int(m.group(1)), "tokens": int(m.group(2)), "lcp_prev": lcp, "cached": None})
            lcp = None
        elif (m := CACHED.search(line)) and tasks and tasks[-1]["task"] == int(m.group(1)):
            tasks[-1]["cached"] = int(m.group(2))
    return tasks


def split_by_call(tasks, calls):
    """One server task per question, in call order (measured: the task count equals the question count)."""
    if len(tasks) != sum(c["questions"] for c in calls):
        return None
    out, i = [], 0
    for c in calls:
        mine, i = tasks[i:i + c["questions"]], i + c["questions"]
        rest = mine[1:]
        out.append({**c, "prompt_tokens": sum(t["tokens"] for t in mine),
                    "cached_first_question": mine[0]["cached"],
                    "cached_later_questions": sum(t["cached"] or 0 for t in rest),
                    "later_questions_with_cache": sum(1 for t in rest if t["cached"]),
                    "shared_prefix_median": statistics.median(t["lcp_prev"] for t in rest)
                    if rest and all(t["lcp_prev"] is not None for t in rest) else None})
    return out


def slowest(n=5):
    """The n requests with the most input tokens in the first replay run, at the card's flags."""
    rows = [json.loads(line) for line in (OUT / "replay-ubsweep.jsonl").read_text(encoding="utf-8").splitlines()]
    rows = [r for r in rows if r["config"] == "ub8192" and r["round"] == 0]
    return [r["file"] for r in sorted(rows, key=lambda r: -r["input_tokens"])[:n]]


def promptcache(args):
    """Reuse within one request (question to question) and across calls, read from the server's per-task counts.

    Workload `sequences` is the hybrid's own calls in order, the across-call case. Workload `slowest` sends each of
    the 5 largest requests twice in a row: the first send shows within-request reuse, the second shows the best case
    for across-call reuse, an identical request. Flag sets alternate within each round.
    """
    seqs = sequences()
    order = [f for s in ("original", "heldout") for files in seqs[s].values() for f in files]
    big = slowest()
    results = {name: {"sequences": None, "slowest": []} for name in args.variants}
    for rnd in range(args.rounds):
        for name in args.variants:
            log = OUT / "logs" / f"promptcache-{name}-r{rnd}.log"
            with d1_server(log_path=log, flags=(*CONFIGS["ub2048"], "-lv", "4", *REUSE_FLAGS[name])) as server:
                client = D1Client(f"http://127.0.0.1:{server.port}")
                calls = []

                def send(f, workload, repeat):
                    body = find(f)["request"]
                    out = client.call("decisions", body, "pc")
                    calls.append({"workload": workload, "file": f, "repeat": repeat, "round": rnd,
                                  "questions": len(body["questions"]), "ms": round(out["timing"]["ms"], 1),
                                  "status": out["status"],
                                  "answers": compact((out["response"] or {}).get("answers") or {})})

                if rnd == 0:
                    for f in order:
                        send(f, "sequences", 0)
                for f in big:
                    send(f, "slowest", 0)
                    send(f, "slowest", 1)
            time.sleep(0.5)
            per_call = split_by_call(server_tasks(log), calls)
            if per_call is None:
                raise RuntimeError(f"{name}: server tasks do not line up with questions, see {log}")
            if rnd == 0:
                seq = [c for c in per_call if c["workload"] == "sequences"]
                results[name]["sequences"] = {
                    "calls": len(seq), "prompt_tokens": sum(c["prompt_tokens"] for c in seq),
                    "cached_first_question": sum(c["cached_first_question"] or 0 for c in seq),
                    "cached_later_questions": sum(c["cached_later_questions"] for c in seq),
                    "total_ms": round(sum(c["ms"] for c in seq), 1)}
            results[name]["slowest"] += [{k: v for k, v in c.items() if k != "answers" or rnd == 0}
                                         for c in per_call if c["workload"] == "slowest"]
            print(name, rnd, json.dumps(results[name]["sequences"]),
                  [(c["ms"], c["cached_later_questions"], c["cached_first_question"]) for c in per_call
                   if c["workload"] == "slowest"], flush=True)
    (OUT / "promptcache.json").write_text(json.dumps(results, indent=1) + "\n", encoding="utf-8")


def reuse(args):
    """Per request: the full call and the reuse call, alternating which goes first. Reused answers are scored
    against the full call's answers to the same request."""
    seqs = sequences()
    rows_path = OUT / "reuse.jsonl"
    with d1_server(log_path=OUT / "logs" / "reuse.log", flags=CONFIGS["ub2048"]) as server, \
            rows_path.open("w", encoding="utf-8") as rows:
        client = D1Client(f"http://127.0.0.1:{server.port}")
        reuser = ReusingClient(client)
        k = 0
        for set_name, by_utt in seqs.items():
            for utt, files in by_utt.items():
                label = f"{set_name}/{utt}"
                for i, f in enumerate(files):
                    body = find(f)["request"]
                    calls = [("full", client), ("reuse", reuser)]
                    if k % 2:
                        calls.reverse()
                    k += 1
                    got = {kind: c.call("decisions", body, label) for kind, c in calls}
                    full, mixed, info = got["full"], got["reuse"], got["reuse"]["reuse"]
                    reused = compact({n: mixed["response"]["answers"][n] for n in info["reused"]})
                    truth = compact({n: full["response"]["answers"][n] for n in info["reused"]})
                    rows.write(json.dumps({
                        "set": set_name, "utterance": utt, "index": i, "file": f,
                        "questions": len(body["questions"]), "asked": len(info["asked"]), "reused": len(reused),
                        "full_ms": round(full["timing"]["ms"], 2), "reuse_ms": round(mixed["timing"]["ms"], 2),
                        "full_tokens": full["response"]["usage"]["input_tokens"],
                        "reuse_tokens": mixed["response"]["usage"]["input_tokens"],
                        "reuse_errors": sorted(n for n in reused if not same(reused[n], truth[n])),
                        "reused_names": info["reused"]}) + "\n")
    print(f"wrote {rows_path}")


def same(a, b):
    """Two compact answers make the same decision: the same option, or P(true) on the same side of 0.5."""
    if isinstance(a, str) or isinstance(b, str):
        return a == b
    return (a >= 0.5) == (b >= 0.5)


async def _bargein(args):
    from voice.prototype.realtime import Server, Session, read_pcm16
    requests = [find(f)["request"] for files in sequences()["original"].values() for f in files]
    lfm_log = OUT / "logs" / f"bargein-lfm-c{args.lfm_ctx or 'default'}.log"
    extra = ("-c", str(args.lfm_ctx)) if args.lfm_ctx else ()
    rows = []
    with Server(args.lfm_port, lfm_log, extra) as lfm, \
            d1_server(log_path=OUT / "logs" / "bargein-d1.log", flags=CONFIGS["ub2048"]) as d1:
        both_mib = gpu_used_mib()
        client = D1Client(f"http://127.0.0.1:{d1.port}")
        pcm, rate, audio_s = read_pcm16(args.clip)
        loop = asyncio.get_running_loop()
        for trial in range(args.trials):
            for load_d1 in (trial % 2 == 0, trial % 2 == 1):
                t0 = time.perf_counter()
                voice = await Session.open(f"ws://127.0.0.1:{args.lfm_port}/v1/realtime", "voice",
                                           lambda: time.perf_counter() - t0)
                created = await voice.wait_for("session.created")
                out_rate = created["session"]["audio"]["output"]["format"]["rate"]
                session = {"type": "realtime", "output_modalities": ["text", "audio"],
                           "instructions": "You are a friendly voice assistant. Answer in two or three sentences."}
                if rate != 16_000:
                    session["audio"] = {"input": {"format": {"type": "audio/pcm", "rate": rate}}}
                await voice.send(type="session.update", session=session)
                await voice.wait_for("session.updated")
                await voice.stream(pcm, rate)
                await voice.send(type="input_audio_buffer.commit")
                mark = len(voice.events)
                asked = voice.clock()
                await voice.send(type="response.create")
                speaking = asyncio.Event()
                d1_ms = []

                def hammer():
                    i = 0
                    while not speaking.is_set():
                        r = client.call("decisions", requests[i % len(requests)], "bargein")
                        d1_ms.append(round(r["timing"]["ms"], 2))
                        i += 1

                worker = loop.run_in_executor(None, hammer) if load_d1 else None
                with VramPeak() as vram:
                    done = await voice.wait_for("response.done", "error", after=mark)
                speaking.set()
                if worker:
                    await worker
                audio = [e for e in voice.events[mark:] if e["type"] == "response.output_audio.delta"]
                pcm_out = sum(e["delta"]["bytes"] for e in audio)
                rows.append({"trial": trial, "d1_load": load_d1, "first_audio_s": audio[0]["_t"] - asked if audio else None,
                             "reply_s": done["_t"] - asked, "reply_audio_s": pcm_out / 2 / out_rate,
                             "d1_calls": len(d1_ms), "d1_ms": d1_ms, "peak_mib": vram.peak, "status": done["type"]})
                print(json.dumps({k: v for k, v in rows[-1].items() if k != "d1_ms"}), flush=True)
                await voice.close()
    (OUT / f"bargein-c{args.lfm_ctx or 'default'}.json").write_text(
        json.dumps({"both_loaded_mib": both_mib, "lfm_startup_s": lfm.startup_s, "rows": rows}, indent=1) + "\n",
        encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("replay")
    p.add_argument("--configs", type=lambda s: s.split(","), default=["ub8192", "ub2048", "ub512", "ub8192-nofa"])
    p.add_argument("--rounds", type=int, default=3)
    p.add_argument("--tag", default="sweep")
    p.add_argument("--paged-attn", choices=["off", "on"], default="off",
                   help="mistral.rs only; llama.cpp uses -fa inside each config")
    p = sub.add_parser("packing")
    p.add_argument("--requests", type=int, default=3, help="how many of the largest recorded requests")
    p.add_argument("--sizes", default="default,8,4,2,1")
    p.add_argument("--concurrent", action="store_true", help="also time two requests at once")
    p.add_argument("--paged-attn", choices=["off", "on"], default="off")
    p = sub.add_parser("promptcache")
    p.add_argument("--variants", type=lambda s: s.split(","), default=list(REUSE_FLAGS))
    p.add_argument("--rounds", type=int, default=3)
    sub.add_parser("reuse")
    p = sub.add_parser("bargein")
    p.add_argument("clip", type=Path)
    p.add_argument("--lfm-ctx", type=int)
    p.add_argument("--lfm-port", type=int, default=8095)
    p.add_argument("--trials", type=int, default=6)
    args = parser.parse_args()
    (OUT / "logs").mkdir(parents=True, exist_ok=True)
    if args.command == "bargein":
        asyncio.run(_bargein(args))
    else:
        {"replay": replay, "packing": packing, "promptcache": promptcache, "reuse": reuse}[args.command](args)


if __name__ == "__main__":
    main()
