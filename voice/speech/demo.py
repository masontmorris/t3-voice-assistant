"""Stage 1 demo: stream a WAV in as a live microphone, print timestamped partial transcripts, and save the spoken reply.

Each 100 ms chunk goes to two contexts on the same model: a live ASR context that prints partial transcripts, and the
voice-chat context, whose cache is prefilled while the user is still speaking. When the clip ends, the reply streams
out as text and speech. Times are seconds from the start of the input audio.

Usage: python demo.py input.wav [--out reply.wav] [--device cuda|cpu] [--fast]
  --out defaults to $VOICE_HOME/evidence/stage1/demo_reply.wav, outside the repo.
  --fast feeds the clip as fast as the model takes it instead of at real-time pace.
"""

import argparse
import time
from pathlib import Path

import soundfile as sf
import torch

import lfm

SYSTEM = "<|startoftext|><|im_start|>system\nRespond with interleaved text and audio.<|im_end|>\n<|im_start|>user\n"


class Printer(lfm.Recorder):
    def __init__(self, clock, tok, echo):
        super().__init__(clock, "text")
        self.tok, self.echo = tok, echo

    def put(self, value):
        before = len(self.tokens)
        super().put(value)
        if self.echo and len(self.tokens) > before:
            piece = self.tok.decode(self.tokens[before:], skip_special_tokens=True)
            if piece:
                print(f"[{self.events[-1]:6.2f}s] reply text: {piece!r}", flush=True)


def run_turn(model, processor, codec, device, wav, realtime=True, echo=True, max_new_tokens=400):
    """Streams one WAV through both contexts and returns the timeline and the reply waveform."""
    tok = processor.tokenizer
    asr = lfm.LiveASR(model, processor, device)
    chat = lfm.Session(model, processor, codec, device)
    chat.append_text(SYSTEM)
    waveform = lfm.load_wav(wav)
    audio_s = len(waveform) / lfm.SR_IN
    t0 = time.perf_counter()
    clock = lambda: time.perf_counter() - t0  # noqa: E731
    first_partial, lags = None, []
    for _, piece, arrival in lfm.mic_chunks(waveform, realtime, clock):
        texts = asr.feed(piece)
        chat.stream_audio(piece)
        lfm.sync(device)
        lags.append(clock() - arrival)
        if any(t.strip() for t in texts):
            first_partial = first_partial or clock()
            if echo:
                print(f"[{clock():6.2f}s] partial (audio to {arrival:5.2f}s): {asr.text}", flush=True)
    asr.feed([], is_final=True)
    chat.stream_audio([], is_final=True)
    lfm.sync(device)
    final_s = clock()
    if echo:
        print(f"[{final_s:6.2f}s] final transcript (audio ended at {audio_s:.2f}s): {asr.text}", flush=True)
    text_rec = Printer(clock, tok, echo)
    audio_rec = lfm.Recorder(clock, "audio", session=chat)
    chat.generate("<|im_end|>\n<|im_start|>assistant\n<|mixed_start|>", streamer=text_rec, audio_streamer=audio_rec,
                  max_new_tokens=max_new_tokens, **lfm.SPEECH_SAMPLING)
    lfm.sync(device)
    end = clock()
    reply = audio_rec.waveform()
    return {
        "audio_s": audio_s,
        "first_partial_s": first_partial,
        "chunk_lag_median_s": sorted(lags)[len(lags) // 2],
        "chunk_lag_max_s": max(lags),
        "final_transcript_after_audio_end_s": final_s - audio_s,
        "first_text_after_audio_end_s": text_rec.events[0] - audio_s if text_rec.events else None,
        "first_audio_after_audio_end_s": audio_rec.first_pcm_s - audio_s if audio_rec.first_pcm_s else None,
        "speech_s": 0 if reply is None else len(reply) / lfm.SR_OUT,
        "end_s": end,
        "transcript": asr.text,
        "reply_text": lfm.text_of(tok, text_rec.tokens),
    }, reply


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("wav")
    parser.add_argument("--out", default=str(lfm.EVIDENCE_DIR / "demo_reply.wav"))
    parser.add_argument("--device", default="cuda" if torch.cuda.is_available() else "cpu")
    parser.add_argument("--fast", action="store_true")
    parser.add_argument("--max-new-tokens", type=int, default=400)
    args = parser.parse_args()
    dtype = torch.bfloat16 if args.device == "cuda" else torch.float32
    model, processor, codec, load_s = lfm.load(args.device, dtype)
    print(f"loaded {lfm.MODEL_ID}@{lfm.REVISION[:8]} on {args.device} ({dtype}) in {load_s:.1f} s", flush=True)
    timeline, reply = run_turn(model, processor, codec, args.device, args.wav, realtime=not args.fast, max_new_tokens=args.max_new_tokens)
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    sf.write(args.out, reply, lfm.SR_OUT)
    print(f"[{timeline['end_s']:6.2f}s] done. first reply audio {timeline['first_audio_after_audio_end_s']:.2f}s after the input ended; "
          f"{timeline['speech_s']:.2f}s of speech; reply text: {timeline['reply_text']!r}")
    print(f"saved {args.out}")
