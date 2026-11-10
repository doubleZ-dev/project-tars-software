"""Bounded PCM conditioning for body audio; no device gain-pin manipulation."""
import asyncio
import math
import struct
from functools import lru_cache
from .audio import ASR_PCM, PcmFormat


def samples(pcm: bytes):
    if len(pcm) % 2:
        raise ValueError("unaligned PCM16")
    return [v[0] for v in struct.iter_unpack("<h", pcm)]


def pack(values):
    return b"".join(struct.pack("<h", max(-32768, min(32767, round(v)))) for v in values)


@lru_cache(maxsize=2048)
def _kernel(rate, phase, phases):
    frac = phase / phases
    cutoff = min(1., 16000 / rate) * .9
    weights = []
    for k in range(-16, 17):
        x = k - frac
        sinc = cutoff if abs(x) < 1e-10 else math.sin(math.pi * cutoff * x) / (math.pi * x)
        weights.append(sinc * (.5 + .5 * math.cos(math.pi * x / 17)))
    total = sum(weights)
    return tuple(w / total for w in weights)


def to_body_pcm(pcm: bytes, fmt: PcmFormat) -> bytes:
    if fmt.sample_width != 2 or fmt.channels not in (1, 2) or not 8000 <= fmt.sample_rate <= 48000:
        raise ValueError("unsupported PCM; require PCM16 mono/stereo 8..48kHz")
    if len(pcm) > fmt.sample_rate * fmt.channels * 2 * 60 or len(pcm) % (2 * fmt.channels):
        raise ValueError("invalid or excessive synthesis PCM")
    data = samples(pcm)
    if fmt.channels == 2:
        data = [(data[i] + data[i + 1]) / 2 for i in range(0, len(data), 2)]
    if not data or fmt.sample_rate == 16000:
        return pack(data)
    # Windowed-sinc low-pass prevents 24kHz Qwen output aliasing at 16kHz.
    divisor = math.gcd(fmt.sample_rate, 16000)
    step, phases = fmt.sample_rate // divisor, 16000 // divisor
    result = []
    for i in range(len(data) * 16000 // fmt.sample_rate):
        centre, phase = divmod(i * step, phases)
        weights = _kernel(fmt.sample_rate, phase, phases)
        result.append(sum(data[max(0, min(len(data) - 1, centre + k - 16))] * w
                          for k, w in enumerate(weights)))
    return pack(result)


def normalize(pcm: bytes, max_gain=8., target_rms=3277.) -> bytes:
    data = samples(pcm)
    if not data:
        return pcm
    mean = sum(data) / len(data)
    data = [v - mean for v in data]
    rms = math.sqrt(sum(v * v for v in data) / len(data))
    peak = max(abs(v) for v in data)
    # Do not turn digital silence/noise into loud output.
    gain = min(max_gain, target_rms / rms, 26213 / peak) if rms >= 64 else 1.
    return pack(v * gain for v in data)


class BodyMicrophone:
    def __init__(self, link, *, gain=4., warmup_ms=2000):
        if not 1 <= gain <= 8 or not 0 <= warmup_ms <= 3000:
            raise ValueError("invalid microphone conditioning")
        self.link, self.gain, self.warmup_ms = link, gain, warmup_ms
        self.previous_x = self.previous_y = 0.

    async def start_capture(self):
        await self.link.start_capture()
        interaction = getattr(self.link, "interaction", None)
        if callable(interaction):
            await interaction("THINKING")  # settling is not yet ready for speech
        for _ in range(math.ceil(self.warmup_ms / ASR_PCM.frame_ms)):
            await self.link.read_frame()  # observed I2S startup settling, never fed to VAD/ASR
        self.previous_x = self.previous_y = 0.

    async def read_frame(self):
        pcm = await self.link.read_frame()
        values = []
        for x in samples(pcm):
            y = x - self.previous_x + .995 * self.previous_y
            self.previous_x, self.previous_y = x, y
            values.append(y * self.gain)
        return pack(values)

    async def stop_capture(self):
        await self.link.stop_capture()


class BodySpeaker:
    def __init__(self, link, *, volume_pct=10):
        if not 1 <= volume_pct <= 20:
            raise ValueError("body output must be 1..20 percent")
        self.link, self.volume_pct = link, volume_pct

    async def play(self, pcm: bytes, fmt: PcmFormat, cancel: asyncio.Event):
        pcm = await asyncio.to_thread(lambda: normalize(to_body_pcm(pcm, fmt)))
        for offset in range(0, len(pcm), 256000):  # eight seconds; board cap is ten
            if cancel.is_set():
                break
            await self.link.play(pcm[offset:offset + 256000], ASR_PCM, cancel,
                                 volume_pct=self.volume_pct)
