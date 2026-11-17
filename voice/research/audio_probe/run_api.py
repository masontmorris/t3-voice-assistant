"""Run the probe's clip sets through the hosted d1.2: audio only, then (where a transcript exists) text only and both.

python -m voice.research.audio_probe.run_api <set> <mode> [transcripts.json] [--local PORT NAME]
  set   eot | bc | kind
  mode  audio | text | both
Transcripts map clip file name to text (LFM3 live ASR of that clip). Rows go to results/<set>_<mode>.jsonl.
"""
import json
import sys
from pathlib import Path

from voice.research.audio_probe import liquid as L, questions as Q

CLIPS = L.OUT / "clips"
RESULTS = L.OUT / "results"
SPEC = {"eot": (Q.EOT, "finished", "done"), "bc": (Q.BC, "floor", "backchannel"), "kind": (None, "s1_kind", None)}


class Local:
    """A local llama-server /v1/systemone. Audio goes in `files`; same answers shape as the hosted API."""

    def __init__(self, port):
        import http.client
        self.conn = http.client.HTTPConnection("127.0.0.1", port, timeout=120)

    def call(self, body, label=""):
        import time
        body = dict(body)
        if "audio" in body:
            body["files"] = body.pop("audio")
        t = time.perf_counter()
        self.conn.request("POST", "/v1/systemone", json.dumps(body), {"Content-Type": "application/json"})
        r = self.conn.getresponse()
        raw = r.read()
        ms = (time.perf_counter() - t) * 1000
        resp = json.loads(raw)
        return {"status": r.status, "ms": ms, "bytes": len(raw), "usage": resp.get("usage"), "cost": 0, "response": resp}


def main(set_name, mode, transcripts=None, local=None):
    RESULTS.mkdir(exist_ok=True)
    rows = [json.loads(l) for l in (CLIPS / "manifest.jsonl").read_text().splitlines() if l]
    rows = [r for r in rows if r["set"] == set_name]
    client = Local(local[0]) if local else L.Client()
    out = RESULTS / f"{set_name}_{mode}{'_' + local[1] if local else ''}.jsonl"
    done = {json.loads(l)["file"] for l in out.read_text().splitlines()} if out.exists() else set()
    with out.open("a") as f:
        for r in rows:
            if r["file"] in done:
                continue
            text = (transcripts or {}).get(r["file"]) if mode != "audio" else None
            if mode != "audio" and text is None:
                continue
            questions, q, opt = SPEC[set_name]
            if set_name == "kind":
                questions = Q.kind(text if mode != "audio" else None)
            body = {"questions": questions}
            if mode in ("audio", "both"):
                x, rate = L.read_wav(CLIPS / r["file"])
                body["audio"] = [L.wav_b64(x, rate)]
            ctx = "The assistant is currently speaking its reply aloud." if set_name == "bc" else None
            if mode == "audio":
                body["state"] = {"context": ctx} if ctx else None
            else:
                state = {"transcript": text, "transcript_is_final": False}
                if ctx:
                    state["context"] = ctx
                body["state"] = state
            rec = client.call(body, f"{set_name}-{mode}")
            ans = rec["response"].get("answers", {}).get(q) if rec["status"] == 200 else None
            f.write(json.dumps({**r, "mode": mode, "status": rec["status"], "ms": rec["ms"], "bytes": rec["bytes"],
                                "tokens": (rec.get("usage") or {}).get("input_tokens"), "cost": rec.get("cost"),
                                "probs": ans and ans["probabilities"], "choice": ans and ans["choice"],
                                "text": text}) + "\n")
            f.flush()


if __name__ == "__main__":
    argv = sys.argv[1:]
    local = None
    if "--local" in argv:
        i = argv.index("--local")
        local = (int(argv[i + 1]), argv[i + 2])
        argv = argv[:i] + argv[i + 3:]
    t = json.loads(Path(argv[2]).read_text()) if len(argv) > 2 else None
    main(argv[0], argv[1], t, local)
