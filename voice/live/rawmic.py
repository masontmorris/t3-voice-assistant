"""Microphone capture that bypasses the audio driver's own effects.

Windows applies the driver's capture effects (a laptop's built-in echo cancellation, noise suppression and gain
control) to every shared-mode stream, so the client's own echo canceller received a signal the driver had already
processed. WASAPI's raw stream option asks the audio engine to skip those effects. Raw streams run at the device's
own rate, so the capture is decimated to the client's 16 kHz here.
"""

import numpy as np

RAW_STREAM_OPTION = 1


def wasapi_input(sd, wanted):
    """The WASAPI input device for `wanted` (None for the default, an index, or part of a name), or None."""
    apis = sd.query_hostapis()
    wasapi = next((i for i, api in enumerate(apis) if "WASAPI" in api["name"]), None)
    if wasapi is None:
        return None
    devices = sd.query_devices()
    if wanted is None:
        index = apis[wasapi]["default_input_device"]
        return index if index is not None and index >= 0 else None
    if isinstance(wanted, int):
        name = devices[wanted]["name"]
        if devices[wanted]["hostapi"] == wasapi:
            return wanted
    else:
        name = wanted
    for index, device in enumerate(devices):
        if device["hostapi"] == wasapi and device["max_input_channels"] > 0 and (
                device["name"].startswith(name) or name.lower() in device["name"].lower()):
            return index
    return None


def lowpass_taps(factor, count=63):
    """A Hann-windowed sinc low-pass with its cutoff at 0.9 of the decimated Nyquist frequency."""
    cutoff = 0.9 / factor
    n = np.arange(count) - (count - 1) / 2
    taps = cutoff * np.sinc(cutoff * n) * np.hanning(count)
    return taps / taps.sum()


class Decimator:
    """Integer-factor decimation of a sample stream that arrives in blocks of any length."""

    def __init__(self, factor):
        self.factor = factor
        self.taps = lowpass_taps(factor) if factor > 1 else None
        self.history = np.zeros(0 if factor == 1 else len(self.taps) - 1)
        self.phase = 0

    def __call__(self, block):
        samples = np.asarray(block, dtype=np.float64)
        if self.factor == 1:
            return samples
        padded = np.concatenate([self.history, samples])
        filtered = np.convolve(padded, self.taps, mode="valid")
        self.history = padded[len(padded) - (len(self.taps) - 1):]
        out = filtered[self.phase::self.factor]
        self.phase = (self.phase - len(filtered)) % self.factor
        return out


def open_raw(sd, wanted, rate, block_samples, on_frame):
    """A started-ready raw input stream that calls `on_frame(pcm16_bytes)` with mono 16-bit frames of
    `block_samples` at `rate`, plus a description of the device; or (None, reason) when raw capture is unavailable."""
    index = wasapi_input(sd, wanted)
    if index is None:
        return None, "no WASAPI input for this device"
    native = int(sd.query_devices(index)["default_samplerate"])
    if native % rate:
        return None, f"device rate {native} Hz is not a multiple of {rate} Hz"
    decimate = Decimator(native // rate)
    pending = np.zeros(0)
    settings = sd.WasapiSettings()
    settings._streaminfo.streamOption = RAW_STREAM_OPTION

    def callback(indata, _frames, _time, _status):
        nonlocal pending
        mono = np.frombuffer(indata, dtype="<i2").reshape(-1, channels).mean(axis=1)
        pending = np.concatenate([pending, decimate(mono)])
        while len(pending) >= block_samples:
            frame, pending = pending[:block_samples], pending[block_samples:]
            on_frame(np.clip(np.rint(frame), -32768, 32767).astype("<i2").tobytes())

    for channels in range(sd.query_devices(index)["max_input_channels"], 0, -1):
        try:
            stream = sd.RawInputStream(samplerate=native, channels=channels, dtype="int16",
                                       blocksize=block_samples * (native // rate), device=index,
                                       extra_settings=settings, callback=callback)
        except sd.PortAudioError as exc:
            reason = str(exc)
            continue
        return stream, f"raw WASAPI capture: {sd.query_devices(index)['name']} at {native} Hz, {channels} ch"
    return None, f"raw WASAPI capture refused: {reason}"
