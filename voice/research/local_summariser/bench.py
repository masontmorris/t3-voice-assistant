"""Can LFM3-1.7B write the thread summaries on the RTX 3060? GPU commands run inside the lease and quiet_run.

    python -m voice.research.local_summariser.bench freeze             # CPU: histories from a read-only T3 database
    python -m voice.research.local_summariser.bench views              # CPU: thread views at each size, tokenized
    python -m voice.research.local_summariser.bench ladder             # GPU: one run of each prompt and output step
    python -m voice.research.local_summariser.bench arms --step S7     # GPU: each input size, three seeds, cache probe
    python -m voice.research.local_summariser.bench voice --step S7 --arm h4k   # GPU: memory, first audio, writer
    python -m voice.research.local_summariser.bench form               # CPU: the code form check of every saved gist
    python -m voice.research.local_summariser.bench --set fresh follow-up          # GPU: S7 to S10 and R1, 3 seeds
    python -m voice.research.local_summariser.bench --set fresh overlap --step S7  # GPU: first audio, one summary
    python -m voice.research.local_summariser.bench gate               # GPU: the local d1-2B's truth gate on saved gists

`--set run1`, the default, is unit 11's 30 frozen threads. `--set fresh` is a second 30, none of them read before,
frozen by `summary_eval freeze --exclude` into $VOICE_HOME/evals/local-summariser/fresh.

Every GPU command loads LFM3-Audio at the live call's settings and d1-2B at the local decider's beside the 1.7B, so
latency is measured on the card as a call shares it. The frozen states and their histories name real threads, so
they and every output stay under $VOICE_HOME/evals/local-summariser and never enter the repo. No request leaves the
machine: the line check runs on the local d1-2B, and the cloud client's transport raises if anything calls it.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sqlite3
import statistics
import threading
import time
import urllib.error
import urllib.request
from contextlib import ExitStack
from itertools import cycle
from pathlib import Path

import voice.t3.pick.model as cloud
from voice.live.t3_tasks import status_act
from voice.local_decider.client import VOICE_HOME, D1Client, LlamaServer, d1_server
from voice.local_decider.gpu import gpu_used_mib
from voice.research.local_renderer.bench import INSTRUCTIONS, LFM_ARGS, LFM_SLOTS, block, summary
from voice.research.local_summariser import gate as truth_gate
from voice.research.local_summariser.view import FOLLOW_UP, LADDER, REASONING, SHORTER, STEPS, Message, Step, form, gist_of, \
    messages, replies_view, schema, thread_view
from voice.t3.eval.summary_eval import thread_state
from voice.t3.speech import Lane, Speech
from voice.t3.verify import Decider

OUT = VOICE_HOME / "evals" / "local-summariser"
FROZEN = VOICE_HOME / "evals" / "local-renderer" / "run1"
ROUND5 = VOICE_HOME / "evals" / "u11-acts" / "round5" / "summaries.json"
DB = Path.home() / ".t3" / "userdata" / "statev2.sqlite"
CLIPS = VOICE_HOME / "evidence" / "live" / "m1-dress-rehearsal-7f9b9bcb04" / "clips"
MODEL = VOICE_HOME / "gguf" / "LFM3-1.7B-Q4_K_M.gguf"
SETS = {"run1": (FROZEN, OUT), "fresh": (OUT / "fresh", OUT / "fresh"), "third": (OUT / "third", OUT / "third"),
        "fourth": (OUT / "fourth", OUT / "fourth")}
STATES_DIR, WORK = SETS["run1"]
REPLIES_BUDGET = 1_000
# The overlap trial starts a summary this long before the speech ends, or this long after it ends: the median from
# the end of speech to the T3 ask's task over 620 recorded turns.
LISTEN_LEAD_S = 0.5
AFTER_END_S = 0.13
ARMS = {"last": None, "h1k": 1_000, "h4k": 4_000, "h8k": 8_000, "h16k": 16_000}
SEEDS = (1, 2, 3)
PORTS = {"lfm": 8095, "d1": 8096, "summariser": 8098}
# The cache probe reads only long views, where reuse would matter: about 4k tokens of JSON and up.
PROBE_CHARS = 16_000
CARD = {"temperature": 0.1, "top_k": 50, "repeat_penalty": 1.1}


def no_openrouter(*_args, **_kwargs):
    raise RuntimeError("this bench makes no OpenRouter call")


def flags(context: int) -> list[str]:
    return ["-m", str(MODEL), "-ngl", "99", "-np", "1", "-c", str(context), "-fa", "on"]


class Summariser:
    """The 1.7B on llama-server's chat route with a step's sampling and output form, keeping the server's timings."""

    def __init__(self, port: int):
        self.url = f"http://127.0.0.1:{port}"
        self.ban = [[self.tokens("<think>", special=True)[0], False]]

    def _post(self, path: str, body: dict, timeout: float = 120) -> dict:
        request = urllib.request.Request(self.url + path, data=json.dumps(body).encode(),
                                         headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.load(response)

    def tokens(self, text: str, special: bool = False) -> list:
        return self._post("/tokenize", {"content": text, "parse_special": special})["tokens"]

    def complete(self, chat: list[dict], step: Step, seed: int) -> dict:
        body = {"messages": chat, "max_tokens": step.max_tokens, "seed": seed, "cache_prompt": True,
                **(CARD if step.sampling else {"temperature": 0})}
        if step.thinking:
            body["chat_template_kwargs"] = {"enable_thinking": True}
        else:
            body.update(chat_template_kwargs={"enable_thinking": False}, logit_bias=self.ban)
        if step.json:
            body["response_format"] = {"type": "json_schema", "json_schema": {"schema": schema(step)}}
        started = time.perf_counter()
        payload = self._post("/v1/chat/completions", body)
        ms = round((time.perf_counter() - started) * 1000, 1)
        choice = payload["choices"][0]
        usage, timings = payload.get("usage") or {}, payload.get("timings") or {}
        return {"text": choice["message"].get("content") or "", "finish": choice.get("finish_reason"), "ms": ms,
                "reasoning": choice["message"].get("reasoning_content"),
                "prompt_tokens": usage.get("prompt_tokens"), "output_tokens": usage.get("completion_tokens"),
                "evaluated": timings.get("prompt_n"), "cached": timings.get("cache_n"),
                "prompt_ms": timings.get("prompt_ms"), "output_ms": timings.get("predicted_ms")}


def load() -> tuple[list[dict], dict[str, dict]]:
    states = json.loads((STATES_DIR / "states.json").read_text(encoding="utf-8"))
    labels = {label["thread_id"]: label for label in
              json.loads((STATES_DIR / "labels.json").read_text(encoding="utf-8"))}
    return states, labels


def freeze(args) -> int:
    """Each frozen thread's messages up to its frozen moment: the state's `as_of` inside a run, else its run's end."""
    states, _ = load()
    db = sqlite3.connect(f"file:{args.db.as_posix()}?mode=ro", uri=True)
    out = {}
    for raw in states:
        thread_id = raw["thread_id"]
        cutoff = raw["as_of"]
        if cutoff is None:
            (payload,), = db.execute("select payload_json from orchestration_v2_projection_runs where run_id = ?",
                                     (raw["run"]["run_id"],)).fetchall()
            run = json.loads(payload)
            cutoff = run.get("completedAt") or run["requestedAt"]
        rows = db.execute("select role, payload_json from orchestration_v2_projection_messages where thread_id = ? "
                          "and streaming = 0 and role in ('user', 'assistant') order by created_at", (thread_id,))
        history = []
        for role, payload in rows:
            message = json.loads(payload)
            if (message.get("text") or "").strip() and message["updatedAt"] <= cutoff:
                history.append({"role": role, "text": message["text"], "at": message["updatedAt"]})
        out[thread_id] = history
    WORK.mkdir(parents=True, exist_ok=True)
    (WORK / "histories.json").write_text(json.dumps(out, indent=1), encoding="utf-8")
    print(json.dumps({"threads": len(out), "messages": sum(len(h) for h in out.values()),
                      "chars": sum(len(m["text"]) for h in out.values() for m in h)}))
    return 0


def views(args) -> int:
    """Every thread's view at every size, sized with the 1.7B's own tokenizer on a CPU server."""
    states, _ = load()
    histories = json.loads((WORK / "histories.json").read_text(encoding="utf-8"))
    with LlamaServer(["-m", str(MODEL), "-ngl", "0", "-c", "512"], PORTS["summariser"], WORK / "logs" / "tokenizer.log"):
        model = Summariser(PORTS["summariser"])
        cache: dict[str, int] = {}

        def count(text: str) -> int:
            if text not in cache:
                cache[text] = len(model.tokens(text))
            return cache[text]

        out = {}
        for raw in states:
            state = thread_state(raw)
            history = [Message(m["role"], m["text"]) for m in histories[raw["thread_id"]]]
            out[raw["thread_id"]] = {arm: thread_view(state, history, budget, count) for arm, budget in ARMS.items()}
            out[raw["thread_id"]]["replies"] = replies_view(state, history, REPLIES_BUDGET, count)
            print(json.dumps({arm: (len(v["messages"]), sum(count(m["text"]) for m in v["messages"]))
                              for arm, v in out[raw["thread_id"]].items() if arm in ARMS}), flush=True)
    (WORK / "views.json").write_text(json.dumps(out, indent=1), encoding="utf-8")
    return 0


def summarise(model: Summariser, step: Step, arm: str, seed: int, path: Path) -> list[dict]:
    states, labels = load()
    views_ = json.loads((WORK / "views.json").read_text(encoding="utf-8"))
    rows = []
    for raw in states:
        state, ask = thread_state(raw), labels[raw["thread_id"]]["ask"]
        try:
            record = model.complete(messages(step, state, views_[raw["thread_id"]][arm], ask), step, seed)
        except urllib.error.HTTPError as exc:
            # llama-server refuses some outputs its chat parser cannot read; the live path would get no gist either.
            record = {"text": "", "ms": None, "prompt_tokens": None, "output_tokens": None,
                      "server_error": f"{exc.code}: {exc.read().decode(errors='replace')[:300]}"}
        gist, error = gist_of(record["text"], step)
        rows.append({"thread_id": raw["thread_id"], "kind": raw["kind"], "ask": ask, "gist": gist,
                     "error": record.get("server_error") or error, **record})
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(rows, indent=1), encoding="utf-8")
    print(json.dumps({"file": path.name, **{f"{key}_median": statistics.median(r[key] for r in rows if r[key] is not None)
                                            for key in ("ms", "prompt_tokens", "output_tokens")},
                      "server_errors": sum("server_error" in r for r in rows)}), flush=True)
    return rows


class Stack:
    """LFM3-Audio and d1-2B as on a call, plus the 1.7B at `context`, each stopped on exit by the PID it started."""

    def __init__(self, logs: Path, context: int):
        self.logs, self.context, self.vram = logs, context, {"idle": gpu_used_mib()}

    def __enter__(self):
        from voice.prototype.realtime import Server
        self.logs.mkdir(parents=True, exist_ok=True)
        self.stack = ExitStack().__enter__()
        self.stack.enter_context(Server(PORTS["lfm"], self.logs / "lfm3-audio.log", LFM_ARGS, LFM_SLOTS))
        self.vram["lfm3_audio"] = gpu_used_mib()
        self.stack.enter_context(d1_server(port=PORTS["d1"], log_path=self.logs / "d1.log"))
        self.vram["lfm3_audio_d1"] = gpu_used_mib()
        self.summariser = self.stack.enter_context(
            LlamaServer(flags(self.context), PORTS["summariser"], self.logs / f"summariser-{self.context}.log"))
        self.vram[f"all_three_c{self.context}"] = gpu_used_mib()
        return self

    def __exit__(self, *exc):
        return self.stack.__exit__(*exc)


def ladder(args) -> int:
    out = WORK / "ladder"
    with Stack(out / "logs", 8192) as stack:
        model = Summariser(PORTS["summariser"])
        for step in LADDER:
            print(f"{step.name}: adds {step.change}", flush=True)
            summarise(model, step, "last", 1, out / f"{step.name}.json")
    (out / "vram.json").write_text(json.dumps(stack.vram, indent=1), encoding="utf-8")
    return 0


def cache_probe(model: Summariser, step: Step, arm: str) -> list[dict]:
    """Per thread: the view less its newest message, then the full view, then the full view with another question.
    The second request shares the first's prefix up to that message, and the third the second's up to the question."""
    states, labels = load()
    views_ = json.loads((WORK / "views.json").read_text(encoding="utf-8"))
    rows = []
    for raw in states:
        state, view = thread_state(raw), views_[raw["thread_id"]][arm]
        if len(view["messages"]) < 2 or len(json.dumps(view)) < PROBE_CHARS:
            continue
        earlier = {**view, "messages": view["messages"][:-1]}
        ask = labels[raw["thread_id"]]["ask"]
        row = {"thread_id": raw["thread_id"]}
        for name, v, q in (("earlier", earlier, ask), ("appended", view, ask), ("new_question", view, None)):
            row[name] = model.complete(messages(step, state, v, q), step, 1)
        rows.append(row)
    return rows


def arms(args) -> int:
    step, out = STEPS[args.step], WORK / "arms"
    with Stack(out / "logs", 32768) as stack:
        model = Summariser(PORTS["summariser"])
        for arm in ARMS:
            for seed in SEEDS:
                print(f"{arm} seed {seed}: {step.name}", flush=True)
                summarise(model, step, arm, seed, out / f"{arm}-s{seed}.json")
        probe = cache_probe(model, step, args.probe_arm)
        (out / "cache-probe.json").write_text(json.dumps(probe, indent=1), encoding="utf-8")
    (out / "vram.json").write_text(json.dumps(stack.vram, indent=1), encoding="utf-8")
    return 0


def writer_lines(writer_url: str, gists: list[dict], path: Path) -> None:
    """Each gist as the live path speaks it: in the thread_status act, worded by the realtime writer, then checked by
    code and by the local d1-2B. Saved before any count."""
    from voice.t3.realtime_writer import RealtimeWriter
    states, labels = load()
    by_id = {raw["thread_id"]: raw for raw in states}
    decider = Decider(D1Client(f"http://127.0.0.1:{PORTS['d1']}"))

    async def main():
        rows = []
        async with RealtimeWriter(writer_url) as writer:
            speech = Speech(None, lambda thread_id: None, decider, Lane(writer))
            loop = asyncio.get_running_loop()
            for item in gists:
                raw = by_id[item["thread_id"]]
                state, ask = thread_state(raw), labels[item["thread_id"]]["ask"]
                act = status_act(state, ask)
                if item["gist"]:
                    act = act.with_slots(summary=item["gist"])
                rendered = await loop.run_in_executor(None, speech.render, (act,), [{"user": ask}])
                rows.append({"thread_id": item["thread_id"], "gist": item["gist"], "line": rendered.text,
                             "raw": rendered.raw, "problems": rendered.problems, "error": rendered.error,
                             "failed": list(rendered.verdict.failed) if rendered.verdict else None})
        return rows

    rows = asyncio.run(main())
    path.write_text(json.dumps(rows, indent=1), encoding="utf-8")
    print(json.dumps({"file": path.name, "spoken": sum(r["line"] is not None for r in rows), "of": len(rows)}),
          flush=True)


def voice(args) -> int:
    """Memory with the 1.7B at 16k and 32k, then at 8k, which holds the last-reply prompt: LFM3-Audio's first audio
    with the 1.7B idle and summarising at the chosen input size, and the gists through the live writer and the line
    check, particle-1's round 5 gists first as the baseline."""
    step, out = STEPS[args.step], WORK / "voice"
    logs = out / "logs"
    result: dict = {"step": step.name, "arm": args.arm, "vram_mib": {}}
    for context in (16384, 32768):
        with Stack(logs, context) as stack:
            result["vram_mib"][context] = stack.vram
    clips = sorted(CLIPS.glob("*.wav"))
    rows: list[dict] = []
    with Stack(logs, 8192) as stack:
        result["vram_mib"][8192] = stack.vram
        model = Summariser(PORTS["summariser"])
        states, labels = load()
        views_ = json.loads((WORK / "views.json").read_text(encoding="utf-8"))
        requests = cycle([messages(step, thread_state(raw), views_[raw["thread_id"]][args.arm],
                                   labels[raw["thread_id"]]["ask"]) for raw in states])

        class Load:
            def complete(self, chat):
                return model.complete(chat, step, 1)

        asyncio.run(block(rows, "alone", PORTS["lfm"], clips))
        for r in range(2):
            for i, clip in enumerate(clips):
                order = ("idle", "summarising") if (i + r) % 2 == 0 else ("summarising", "idle")
                for condition in order:
                    asyncio.run(block(rows, "rendering" if condition == "summarising" else condition, PORTS["lfm"],
                                      [clip], Load(), requests))
                    rows[-1]["condition"] = condition
        writer_url = f"ws://127.0.0.1:{PORTS['lfm']}/v1/realtime"
        particle = [{"thread_id": r["thread_id"], "gist": r["summary"]} for r in
                    json.loads(ROUND5.read_text(encoding="utf-8"))]
        writer_lines(writer_url, particle, out / "writer-particle1.json")
        for seed in SEEDS:
            gists = json.loads((WORK / "arms" / f"{args.arm}-s{seed}.json").read_text(encoding="utf-8"))
            writer_lines(writer_url, gists, out / f"writer-{args.arm}-s{seed}.json")
        stack.vram["after_writer"] = gpu_used_mib()
    result["first_audio_s"] = {c: summary(r["first_audio_s"] for r in rows if r["condition"] == c)
                               for c in ("alone", "idle", "summarising")}
    result["summary_ms_beside_reply"] = summary(ms for r in rows for ms in r["render_ms"])
    result["rows"] = rows
    (out / "voice.json").write_text(json.dumps(result, indent=1), encoding="utf-8")
    print(json.dumps({k: result[k] for k in ("vram_mib", "first_audio_s", "summary_ms_beside_reply")}, indent=1))
    return 0


def form_all(args) -> int:
    """The code form check of every saved gist, written beside the outputs."""
    states, _ = load()
    by_id = {raw["thread_id"]: thread_state(raw) for raw in states}
    files = {}
    if (STATES_DIR / "summaries.json").exists():
        s0 = json.loads((STATES_DIR / "summaries.json").read_text(encoding="utf-8"))
        files["ladder/S0"] = [{"thread_id": r["thread_id"], "gist": r["summary"], "error": None} for r in s0]
    for path in sorted([*(WORK / "ladder").glob("S*.json"), *(WORK / "arms").glob("*-s*.json"),
                        *(WORK / "follow-up").glob("*-s*.json")]):
        files[f"{path.parent.name}/{path.stem}"] = json.loads(path.read_text(encoding="utf-8"))
    table = {}
    for name, rows in files.items():
        problems = [[r["error"]] if r.get("error") else form(r["gist"], by_id[r["thread_id"]]) for r in rows]
        words = [len((r["gist"] or "").split()) for r in rows]
        table[name] = {"pass": sum(not p for p in problems), "of": len(rows), "words_median": statistics.median(words),
                       "problems": [p for p in problems]}
        print(json.dumps({"file": name, "pass": table[name]["pass"], "of": len(rows),
                          "words_median": table[name]["words_median"]}))
    (WORK / "form.json").write_text(json.dumps(table, indent=1), encoding="utf-8")
    return 0


def follow_up(args) -> int:
    """The second set's arms: S7 and its three variants on the last reply, and R1 on the replies view."""
    out = WORK / "follow-up"
    with Stack(out / "logs", 8192) as stack:
        model = Summariser(PORTS["summariser"])
        arms = {"fresh": (STEPS["S7"], *FOLLOW_UP), "third": (STEPS["S9"], *SHORTER), "fourth": (STEPS["S11"], *REASONING)}
        for step in arms[args.set]:
            print(f"{step.name}: {step.change}", flush=True)
            for seed in SEEDS:
                path = out / f"{step.name}-s{seed}.json"
                if not path.exists():
                    summarise(model, step, "replies" if step.replies else "last", seed, path)
    (out / "vram.json").write_text(json.dumps(stack.vram, indent=1), encoding="utf-8")
    return 0


async def overlap_trial(lfm_port: int, clip: Path, when: str | None, summarise_once) -> dict:
    """One spoken reply to `clip`, with one summary started mid-utterance ("listening"), just after the speech ends
    ("after_end"), or not at all. Times are on the session's clock, from `response.create`."""
    from voice.prototype.realtime import Session, read_pcm16
    pcm, rate, _ = read_pcm16(clip)
    t0 = time.perf_counter()
    voice = await Session.open(f"ws://127.0.0.1:{lfm_port}/v1/realtime", "voice", lambda: time.perf_counter() - t0)
    created = await voice.wait_for("session.created")
    out_rate = created["session"]["audio"]["output"]["format"]["rate"]
    session = {"type": "realtime", "output_modalities": ["text", "audio"], "instructions": INSTRUCTIONS}
    if rate != 16_000:
        session["audio"] = {"input": {"format": {"type": "audio/pcm", "rate": rate}}}
    await voice.send(type="session.update", session=session)
    await voice.wait_for("session.updated")
    loop, record = asyncio.get_running_loop(), {}

    def run():
        record["start"] = voice.clock()
        record["ms"] = summarise_once()["ms"]
        record["end"] = voice.clock()

    async def fire_at(at: float):
        await asyncio.sleep(max(0.0, at - voice.clock()))
        await loop.run_in_executor(None, run)

    job = None
    if when == "listening":
        job = asyncio.create_task(fire_at(voice.clock() + len(pcm) / 2 / rate - LISTEN_LEAD_S))
    await voice.stream(pcm, rate)
    await voice.send(type="input_audio_buffer.commit")
    mark = len(voice.events)
    asked = voice.clock()
    if when == "after_end":
        job = asyncio.create_task(fire_at(asked + AFTER_END_S))
    await voice.send(type="response.create")
    await voice.wait_for("response.done", "error", after=mark)
    if job is not None:
        await job
    audio = [e for e in voice.events[mark:] if e["type"] == "response.output_audio.delta"]
    await voice.close()
    first = audio[0]["_t"] - asked if audio else None
    row = {"condition": when or "idle", "clip": clip.name, "first_audio_s": first,
           "reply_audio_s": sum(e["delta"]["bytes"] for e in audio) / 2 / out_rate}
    if record:
        row.update(summary_ms=record["ms"], summary_start_s=record["start"] - asked,
                   summary_end_s=record["end"] - asked,
                   running_at_first_audio=first is not None and record["start"] - asked < first < record["end"] - asked)
    return row


def overlap(args) -> int:
    """LFM3-Audio's first audio with one summary started while it listens or just after the speech ends, against none,
    rotating the order clip by clip over two rounds."""
    step, out = STEPS[args.step], WORK / "overlap"
    states, labels = load()
    views_ = json.loads((WORK / "views.json").read_text(encoding="utf-8"))
    requests = cycle([messages(step, thread_state(raw), views_[raw["thread_id"]]["last"],
                               labels[raw["thread_id"]]["ask"]) for raw in states])
    clips = sorted(CLIPS.glob("*.wav"))
    rows = []
    with Stack(out / "logs", 8192):
        model = Summariser(PORTS["summariser"])
        model.complete(next(requests), step, 1)
        conditions = (None, "listening", "after_end")
        for r in range(2):
            for i, clip in enumerate(clips):
                for k in range(3):
                    when = conditions[(i + r + k) % 3]
                    row = asyncio.run(overlap_trial(PORTS["lfm"], clip, when,
                                                    lambda: model.complete(next(requests), step, 1)))
                    rows.append(row)
                    print(json.dumps(row), flush=True)
    result = {"step": step.name, "listen_lead_s": LISTEN_LEAD_S, "after_end_s": AFTER_END_S,
              "first_audio_s": {c: summary(r["first_audio_s"] for r in rows if r["condition"] == c)
                                for c in ("idle", "listening", "after_end")},
              "running_at_first_audio": {c: sum(r.get("running_at_first_audio", False) for r in rows
                                                if r["condition"] == c) for c in ("listening", "after_end")},
              "rows": rows}
    (out / "overlap.json").write_text(json.dumps(result, indent=1), encoding="utf-8")
    print(json.dumps({k: result[k] for k in ("first_audio_s", "running_at_first_audio")}, indent=1))
    return 0


# The gists the gate is asked about: last-reply arms only, by set.
GATE_SOURCES = {"run1": ("arms", ("last",)), "fresh": ("follow-up", ("S7", "S9")), "third": ("follow-up", ("S9", "S11")),
                "fourth": ("follow-up", ("S11", "S13"))}


def gate_items(sets) -> list[dict]:
    """Every distinct parsed gist of the last-reply arms, per set and thread."""
    items, seen = [], set()
    for name in sets:
        folder, arms = GATE_SOURCES[name]
        states_dir, work = SETS[name]
        states = {raw["thread_id"]: raw for raw in json.loads((states_dir / "states.json").read_text(encoding="utf-8"))}
        for arm in arms:
            for seed in SEEDS:
                for row in json.loads((work / folder / f"{arm}-s{seed}.json").read_text(encoding="utf-8")):
                    key = (name, row["thread_id"], row["gist"])
                    if row["gist"] and key not in seen:
                        seen.add(key)
                        items.append({"set": name, "thread_id": row["thread_id"], "gist": row["gist"],
                                      "state": states[row["thread_id"]]})
    return items


def gate(args) -> int:
    """Ask each wording about each distinct gist, one request each, with all three models loaded."""
    out = OUT / "gate"
    sets = ("fourth",) if args.set == "fourth" else ("run1", "fresh", "third")
    items = gate_items(sets)
    rows = []
    with Stack(out / "logs", 8192):
        decider = D1Client(f"http://127.0.0.1:{PORTS['d1']}")
        for item in items:
            state = thread_state(item["state"])
            for wording in truth_gate.WORDINGS:
                record = decider.call("decisions", truth_gate.request(state, item["gist"], wording), label="gate")
                answer = ((record.get("response") or {}).get("answers") or {}).get(wording.name) or {}
                rows.append({"set": item["set"], "thread_id": item["thread_id"], "gist": item["gist"],
                             "wording": wording.name, "status": record["status"], "p_yes": answer.get("noul"),
                             "ms": round(record["timing"]["ms"], 1)})
        print(json.dumps({"items": len(items), "requests": len(rows),
                          "errors": sum(r["p_yes"] is None for r in rows),
                          "ms_median": statistics.median(r["ms"] for r in rows)}), flush=True)
    out.mkdir(parents=True, exist_ok=True)
    (out / ("answers-fourth.json" if args.set == "fourth" else "answers.json")).write_text(json.dumps(rows, indent=1),
                                                                                           encoding="utf-8")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--set", choices=SETS, default="run1")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("freeze").add_argument("--db", type=Path, default=DB)
    sub.add_parser("views")
    sub.add_parser("ladder")
    command = sub.add_parser("arms")
    command.add_argument("--step", choices=STEPS, required=True)
    command.add_argument("--probe-arm", choices=ARMS, default="h16k")
    command = sub.add_parser("voice")
    command.add_argument("--step", choices=STEPS, required=True)
    command.add_argument("--arm", choices=ARMS, required=True)
    sub.add_parser("form")
    sub.add_parser("follow-up")
    sub.add_parser("overlap").add_argument("--step", choices=STEPS, required=True)
    sub.add_parser("gate")
    args = parser.parse_args(argv)
    global STATES_DIR, WORK
    STATES_DIR, WORK = SETS[args.set]
    cloud._post = no_openrouter
    return {"freeze": freeze, "views": views, "ladder": ladder, "arms": arms, "voice": voice, "form": form_all,
            "follow-up": follow_up, "overlap": overlap, "gate": gate}[args.command](args)


if __name__ == "__main__":
    raise SystemExit(main())
