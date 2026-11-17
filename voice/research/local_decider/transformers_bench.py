"""Time d1-2B's Transformers `system_one` on recorded requests, beside llama.cpp's answers to the same requests.

    <venv with torch and transformers>/python -m voice.research.local_decider.transformers_bench [--repeats 5]

The Transformers path packs up to `set_size` questions after one copy of the state (16 by default), where llama.cpp
reads the state once per question. Each request runs at the default group size and at 1, alternating, so the gap
between the two is what packing buys. The weights load offline from the Hugging Face cache, read-only. Copied remote
code goes to this run's folder. Run it after `bench replay` (it reads that run's rows) and inside the GPU lease.
"""

import argparse
import json
import os
import statistics
import sys
import time
from pathlib import Path

OUT = Path(os.environ.get("VOICE_HOME", Path.home() / "voice")) / "evidence" / "local-decider"
os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("HF_MODULES_CACHE", str(OUT / "hf-modules"))
sys.dont_write_bytecode = True

from voice.local_decider.gpu import gpu_used_mib  # noqa: E402
from voice.research.local_decider.bench import CACHES, compact, load, same  # noqa: E402

REPO_ID = "LiquidAI/d1-2B"
SET_SIZES = (None, 1)


def picks():
    """The 5 largest and the 5 nearest the median of the first replay run, with llama.cpp's answers and latency."""
    rows = [json.loads(line) for line in (OUT / "replay-ubsweep.jsonl").read_text(encoding="utf-8").splitlines()]
    ref = [r for r in rows if r["config"] == "ub2048" and r["status"] == 200]
    first = {r["file"]: r for r in ref if r["round"] == 0}
    ms = {f: statistics.median(r["ms"] for r in ref if r["file"] == f) for f in first}
    ranked = sorted(first.values(), key=lambda r: r["input_tokens"])
    mid = len(ranked) // 2
    chosen = {"slowest": ranked[-5:][::-1], "median": ranked[mid - 2:mid + 3]}
    return {group: [{"file": r["file"], "source": r["source"], "questions": r["questions"],
                     "llama_tokens": r["input_tokens"], "llama_ms": ms[r["file"]], "llama": r["answers"]}
                    for r in rs] for group, rs in chosen.items()}


def moved(a, b):
    out = 0.0
    for name, x in a.items():
        y = b.get(name)
        if not isinstance(y, dict):
            continue
        if x.get("type") == "choice":
            out = max(out, max(abs(p - y["probabilities"].get(k, 0)) for k, p in x["probabilities"].items()))
        else:
            out = max(out, abs(x["noul"] - y["noul"]))
    return out


def flips(a, b):
    ca, cb = compact(a), compact(b)
    return sum(1 for k in ca if k in cb and not same(ca[k], cb[k]))


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--repeats", type=int, default=5)
    args = parser.parse_args()
    import torch
    from transformers import AutoModel

    idle = gpu_used_mib()
    started = time.perf_counter()
    model = AutoModel.from_pretrained(REPO_ID, trust_remote_code=True, dtype=torch.bfloat16,
                                      local_files_only=True).to("cuda")
    load_s = time.perf_counter() - started
    loaded = gpu_used_mib()
    kernels = "flash"
    if not torch.backends.cuda.is_flash_attention_available():
        # The remote code picks flash kernels on any Ampere GPU without asking whether this torch build has them, and
        # the Windows CUDA wheel does not. Its portable path is masked scaled_dot_product_attention.
        for name, module in list(sys.modules.items()):
            if name.startswith("transformers_modules.") and name.endswith(".attention") and hasattr(module, "_fused"):
                module._fused = lambda q: False
        kernels = "portable"
    chosen = picks()
    bodies = {p["file"]: load(CACHES[p["source"]] / p["file"])["request"] for g in chosen.values() for p in g}

    def ask(f, set_size):
        body = bodies[f]
        torch.cuda.synchronize()
        t = time.perf_counter()
        out = model.system_one(body["state"], body["questions"], set_size=set_size)
        torch.cuda.synchronize()
        return (time.perf_counter() - t) * 1000, out

    for f in list(bodies)[:2]:
        for size in SET_SIZES:
            ask(f, size)
    torch.cuda.reset_peak_memory_stats()
    timings = {(f, s): [] for f in bodies for s in SET_SIZES}
    answers = {}
    for rep in range(args.repeats):
        for f in bodies:
            for size in (SET_SIZES if rep % 2 == 0 else SET_SIZES[::-1]):
                ms, out = ask(f, size)
                timings[(f, size)].append(ms)
                answers.setdefault((f, size), out)
    peak_alloc = torch.cuda.max_memory_allocated() / 2 ** 20
    peak_total = gpu_used_mib()

    result = {"model": REPO_ID, "torch": torch.__version__, "kernels": kernels, "load_s": round(load_s, 2), "idle_mib": idle,
              "loaded_mib": loaded, "after_runs_mib": peak_total, "torch_peak_allocated_mib": round(peak_alloc, 1),
              "repeats": args.repeats, "groups": {}}
    for group, rows in chosen.items():
        out = []
        for p in rows:
            f = p["file"]
            pplx = load(CACHES[p["source"]] / f)["response"]["answers"]
            row = {"file": f, "questions": p["questions"], "llama_tokens": p["llama_tokens"],
                   "llama_ms": round(p["llama_ms"], 1), "llama_vs_pplx_flips": flips(p["llama"], pplx)}
            for size in SET_SIZES:
                key = "set16" if size is None else f"set{size}"
                tf = answers[(f, size)]["answers"]
                row[key] = {"ms": round(statistics.median(timings[(f, size)]), 1),
                            "ms_range": [round(min(timings[(f, size)]), 1), round(max(timings[(f, size)]), 1)],
                            "input_tokens": answers[(f, size)]["usage"]["input_tokens"],
                            "flips_vs_llama": flips(tf, p["llama"]), "max_move_vs_llama": round(moved(tf, p["llama"]), 4),
                            "flips_vs_pplx": flips(tf, pplx)}
            out.append(row)
            print(json.dumps(row), flush=True)
        result["groups"][group] = out
    (OUT / "transformers.json").write_text(json.dumps(result, indent=1) + "\n", encoding="utf-8")
    print(json.dumps({k: v for k, v in result.items() if k != "groups"}))


if __name__ == "__main__":
    main()
