"""Shared setup for the Stage 1 scripts: offline, pinned model loading and the token ids the scripts need."""

import json
import os
import platform
import sys
import time
from pathlib import Path

VOICE_HOME = Path(os.environ.get("VOICE_HOME", Path.home() / "voice"))
os.environ.setdefault("HF_HOME", str(VOICE_HOME / "hf"))
os.environ["HF_HUB_OFFLINE"] = "1"
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")

import torch  # noqa: E402
from transformers import AutoModelForMultimodalLM, AutoProcessor  # noqa: E402

MODEL_ID = "LiquidAI/LFM3-Audio-2B"
REVISION = "2d6c1f99c129b3ef07a8d168bc0ff08e2bdb93f1"
AUDIO_DIR = VOICE_HOME / "evidence" / "stage1" / "audio"
EVIDENCE_DIR = VOICE_HOME / "evidence" / "stage1"
SR_IN, SR_OUT = 16_000, 24_000
SPEECH_SAMPLING = dict(audio_temperature=1.0, audio_top_k=128, audio_repetition_penalty=1.1, audio_repetition_window=4096)


def sync(device):
    if device == "cuda":
        torch.cuda.synchronize()


def load(device, dtype):
    t0 = time.perf_counter()
    model = AutoModelForMultimodalLM.from_pretrained(
        MODEL_ID, revision=REVISION, dtype=dtype, device_map=device, trust_remote_code=True
    ).eval()
    processor = AutoProcessor.from_pretrained(MODEL_ID, revision=REVISION, trust_remote_code=True)
    codec = processor.audio_tokenizer.to(device=device).eval()
    sync(device)
    return model, processor, codec, time.perf_counter() - t0


def environment(device, dtype):
    info = {
        "python": sys.version.split()[0],
        "torch": torch.__version__,
        "device": device,
        "dtype": str(dtype),
        "threads": torch.get_num_threads(),
        "cpu": platform.processor(),
        "hf_hub_offline": os.environ["HF_HUB_OFFLINE"],
        "revision": REVISION,
    }
    if device == "cuda":
        info["gpu"] = torch.cuda.get_device_name(0)
        info["cuda"] = torch.version.cuda
    return info


def write_json(name, payload):
    EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
    path = EVIDENCE_DIR / name
    path.write_text(json.dumps(payload, indent=2))
    print(f"wrote {path}")
    return path


import sys as _sys  # noqa: E402

import librosa  # noqa: E402
from transformers.cache_utils import DynamicCache  # noqa: E402
from transformers.generation.streamers import BaseStreamer  # noqa: E402

CHUNK_SAMPLES = 1_600  # a 100 ms microphone chunk


def load_wav(path):
    waveform, _ = librosa.load(str(path), sr=SR_IN)
    return waveform


def mic_chunks(waveform, realtime, clock):
    """Yields (index, samples, audio_end_s) and, when realtime, holds each chunk until its audio has 'arrived'."""
    for index, start in enumerate(range(0, len(waveform), CHUNK_SAMPLES)):
        piece = waveform[start:start + CHUNK_SAMPLES]
        arrival = (start + len(piece)) / SR_IN
        if realtime:
            wait = arrival - clock()
            if wait > 0:
                time.sleep(wait)
        yield index, piece, arrival


class Session:
    """One conversation held in a single KV cache: text is appended, audio is prefilled while it streams in."""

    def __init__(self, model, processor, codec, device):
        self.model, self.processor, self.codec, self.device = model, processor, codec, device
        self.tok = processor.tokenizer
        self.cache = DynamicCache(config=model.config.text_config)
        self.mask = torch.ones((1, 0), dtype=torch.long, device=device)
        self.encoder_cache = None
        self.feature_cache = None
        self.codec_module = _sys.modules[type(codec).__module__]

    def ids(self, text):
        return self.tok(text, add_special_tokens=False, return_tensors="pt").input_ids.to(self.device)

    def append_text(self, text):
        """Prefill text tokens into the cache without generating."""
        input_ids = self.ids(text)
        n = input_ids.shape[1]
        past = self.mask.shape[1]
        self.mask = torch.cat((self.mask, self.mask.new_ones((1, n))), dim=-1)
        self.model.model.language_model(
            inputs_embeds=self.model.get_input_embeddings()(input_ids),
            past_key_values=self.cache,
            attention_mask=self.mask,
            position_ids=torch.arange(past, past + n, device=self.device)[None],
            use_cache=True,
        )

    def stream_audio(self, samples, is_final=False):
        """Feed one microphone chunk: mel features, then completed encoder tokens into the cache. Returns tokens added."""
        features, self.feature_cache = self.processor.feature_extractor.stream(samples, self.feature_cache, is_final=is_final)
        out = self.model.prefill_audio(
            features.input_features.to(self.device),
            past_key_values=self.cache,
            encoder_cache=self.encoder_cache,
            attention_mask=self.mask,
            is_final=is_final,
        )
        added = out.attention_mask.shape[1] - self.mask.shape[1]
        self.encoder_cache, self.mask = out.encoder_cache, out.attention_mask
        if is_final:
            self.encoder_cache, self.feature_cache = None, None
        return added

    def generate(self, suffix, streamer=None, audio_streamer=None, max_new_tokens=512, **kwargs):
        """Append `suffix` and generate from the cache. Rebuilds the mask from the cache even after an interruption."""
        input_ids = self.ids(suffix)
        mask = torch.cat((self.mask, self.mask.new_ones((1, input_ids.shape[1]))), dim=-1)
        try:
            out = self.model.generate(
                input_ids=input_ids,
                attention_mask=mask,
                past_key_values=self.cache,
                max_new_tokens=max_new_tokens,
                streamer=streamer,
                audio_streamer=audio_streamer,
                return_dict_in_generate=True,
                **kwargs,
            )
        finally:
            self.mask = torch.ones((1, self.cache.get_seq_length()), dtype=torch.long, device=self.device)
        return out


class Recorder(BaseStreamer):
    """Text or audio streamer that timestamps every item; optionally decodes speech incrementally and can interrupt."""

    def __init__(self, clock, kind, session=None, decode_every=6, stop_after=None):
        self.clock, self.kind, self.session = clock, kind, session
        self.events, self.tokens, self.frames, self.pcm = [], [], [], []
        self.decode_every, self.stop_after = decode_every, stop_after
        self.pending, self.codec_cache, self.skip_prompt = [], None, kind == "text"
        self.first_pcm_s = None

    def put(self, value):
        if self.skip_prompt:
            self.skip_prompt = False
            return
        now = self.clock()
        if self.kind == "text":
            self.tokens.append(int(value.reshape(-1)[0]))
            self.events.append(now)
            return
        self.frames.append(value)
        self.events.append(now)
        if self.session is not None:
            self.pending.append(value)
            if len(self.frames) == 1 or len(self.pending) >= self.decode_every:
                self._decode(finalize=False)
        if self.stop_after is not None and len(self.frames) >= self.stop_after:
            raise Interrupted(now)

    def _decode(self, finalize):
        if self.codec_cache is None:
            self.codec_cache = self.session.codec_module.LiquidCodecStreamingCache(self.session.codec.config)
        if self.codec_cache.finished:
            return
        codes = torch.stack(self.pending, dim=-1)[None] if self.pending else torch.empty((1, self.session.codec.config.n_q, 0), dtype=torch.long, device=self.session.device)
        with torch.no_grad():
            audio = self.session.codec.decode(codes, streaming_cache=self.codec_cache, finalize=finalize).audio_values
        self.pending = []
        if audio.shape[-1]:
            sync(self.session.device)
            if self.first_pcm_s is None:
                self.first_pcm_s = self.clock()
            self.pcm.append(audio[0].float().cpu())

    def end(self):
        if self.kind == "audio" and self.session is not None and (self.codec_cache is not None or self.pending):
            self._decode(finalize=True)

    def waveform(self):
        return torch.cat(self.pcm).numpy() if self.pcm else None


class Interrupted(Exception):
    def __init__(self, at):
        super().__init__(f"interrupted at {at:.3f}")
        self.at = at


def text_of(tok, tokens):
    return tok.decode(tokens, skip_special_tokens=True)


class LiveASR:
    """The card's live ASR loop, one 0.96 s block of encoder tokens at a time, as a stateful object."""

    PROMPT = "<|startoftext|><|im_start|>system\nPerform live ASR.<|im_end|>\n<|im_start|>assistant\n"

    def __init__(self, model, processor, device):
        self.model, self.processor, self.device = model, processor, device
        tok = processor.tokenizer
        self.tok = tok
        self.prompt = tok(self.PROMPT, add_special_tokens=False, return_tensors="pt").input_ids.to(device)
        self.suppress = tok.convert_tokens_to_ids(["<|mixed_end|>", "<|endoftext|>"])
        self.stop = tok.convert_tokens_to_ids(["<|endoftext|>", "<|im_start|>"])
        self.chunk_frames = 8 * model.config.interleaved_n_audio
        self.state, self.feature_cache, self.text = None, None, ""
        self.mel = torch.empty((1, processor.feature_extractor.feature_size, 0), device=device)

    def _block(self, frames, is_final=False):
        out = self.model.transcribe_live(
            frames, self.state, input_ids=self.prompt if self.state is None else None, is_final=is_final,
            max_new_tokens=96 if is_final else 48, suppress_token_ids=self.suppress, stop_token_ids=self.stop,
        )
        self.state = out.state
        piece = self.tok.decode(out.sequences[0], skip_special_tokens=True)
        self.text += piece
        return piece

    def feed(self, samples, is_final=False):
        """Feeds one mic chunk. Returns the text pieces of every block it completed."""
        features, self.feature_cache = self.processor.feature_extractor.stream(samples, self.feature_cache, is_final=is_final)
        self.mel = torch.cat((self.mel, features.input_features.to(self.device)), dim=-1)
        pieces = []
        while self.mel.shape[-1] >= self.chunk_frames and not (is_final and self.mel.shape[-1] == self.chunk_frames):
            pieces.append(self._block(self.mel[..., :self.chunk_frames]))
            self.mel = self.mel[..., self.chunk_frames:]
        if is_final:
            pieces.append(self._block(self.mel, is_final=True))
        return pieces
