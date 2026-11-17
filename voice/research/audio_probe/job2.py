"""One GPU job: LFM3 ASR of the synthetic backchannel clips, then the local d1-Omni 2B and 600M over every probe set.

Run under the GPU lease and quiet_run. Per model it records GPU memory (nvidia-smi total used, before the server, after
load, after the run), then runs eot (audio, live-view text), bc (audio, text), kind (audio, text) and the gate set.
"""
import asyncio
import json
import subprocess
import sys
import time
import urllib.request

from voice.research.audio_probe import asr_run, bc_text, liquid as L, run_api, run_gate

GGUF = L.HOME / "gguf" / "d1-omni"
MODELS = {
    "2b": (GGUF / "d1-Omni-2B-Q8_0.gguf", GGUF / "mmproj-d1-Omni-2B-Q8_0.gguf"),
    "600m": (GGUF / "d1-omni-600M-Q8_0.gguf", GGUF / "mmproj-d1-omni-600M-Q8_0.gguf"),
}
PORT = 18196


def vram():
    out = subprocess.run(["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader,nounits"],
                         capture_output=True, text=True).stdout.strip()
    return int(out.splitlines()[0])


def healthy(proc, port, timeout=240):
    end = time.time() + timeout
    while time.time() < end:
        if proc.poll() is not None:
            raise RuntimeError(f"server exited {proc.returncode}")
        try:
            if urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=2).status == 200:
                return
        except OSError:
            pass
        time.sleep(0.5)
    raise RuntimeError("not healthy")


async def sapi_asr():
    man = [json.loads(l) for l in (asr_run.CLIPS / "manifest.jsonl").read_text().splitlines()]
    out = {}
    for r in man:
        if r.get("kind") == "sapi":
            out[r["file"]] = (await asr_run.transcribe(asr_run.CLIPS / r["file"], False))["text"]
    prev = json.loads((L.OUT / "transcripts_asr.json").read_text())
    prev.update(out)
    (L.OUT / "transcripts_asr.json").write_text(json.dumps(prev, indent=0))
    print("sapi asr", len(out), flush=True)


def with_server(cmd, port, fn, log):
    proc = subprocess.Popen(cmd, stdout=open(L.OUT / log, "w"), stderr=subprocess.STDOUT)
    try:
        healthy(proc, port)
        return fn()
    finally:
        proc.terminate()
        proc.wait(timeout=30)


def local_run(name):
    print("local", name, "vram before", vram(), flush=True)
    asr = json.loads((L.OUT / "transcripts_asr.json").read_text())
    live = json.loads((L.OUT / "transcripts_live.json").read_text())
    kind_tx = json.loads((L.OUT / "transcripts_kind.json").read_text())
    tag = (PORT, name)
    run_api.main("eot", "audio", None, tag)
    run_api.main("eot", "text", live, tag)
    run_api.main("bc", "audio", None, tag)
    run_api.main("bc", "text", bc_text.build(), tag)
    run_api.main("kind", "audio", None, tag)
    run_api.main("kind", "text", kind_tx, tag)
    run_gate.run(tag, name)
    print("local", name, "vram after", vram(), flush=True)


def main():
    cmd = [str(asr_run.SERVER), "-m", str(asr_run.GGUF / "LFM3-Audio-2B-Q4_K_M.gguf"), "-mm",
           str(asr_run.GGUF / "mmproj-LFM3-Audio-2B-Q8_0.gguf"), "--host", "127.0.0.1", "--port", str(asr_run.PORT),
           "--temp", "0", "-np", "1", "-ngl", "99"]
    with_server(cmd, asr_run.PORT, lambda: asyncio.run(sapi_asr()), "asr_server2.log")
    mem = {}
    for name, (model, mmproj) in MODELS.items():
        base = vram()
        cmd = [str(asr_run.SERVER), "-m", str(model), "--mmproj", str(mmproj), "--host", "127.0.0.1", "--port",
               str(PORT), "-ngl", "99", "-np", "1", "-b", "8192", "-ub", "2048", "-fa", "on", "-c", "8192"]

        def go():
            mem[name] = {"before_mib": base}
            probe = run_api.Local(PORT)
            probe.call({"state": "warm up", "questions": {"s": {"type": "noul", "instructions": "Is this text?"}}})
            mem[name]["after_load_mib"] = vram()
            local_run(name)
            mem[name]["after_run_mib"] = vram()

        with_server(cmd, PORT, go, f"d1_{name}_server.log")
    (L.OUT / "vram.json").write_text(json.dumps(mem, indent=1))
    print(mem)


if __name__ == "__main__":
    main()
