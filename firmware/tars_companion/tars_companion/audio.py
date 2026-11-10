from __future__ import annotations

import asyncio
import math
import queue
import struct
import wave
from collections import deque
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class PcmFormat:
    sample_rate: int = 16_000
    channels: int = 1
    sample_width: int = 2
    frame_ms: int = 100

    @property
    def frame_bytes(self) -> int:
        return self.sample_rate * self.channels * self.sample_width * self.frame_ms // 1000


ASR_PCM = PcmFormat()
TTS_PCM = PcmFormat(sample_rate=24_000)


def make_test_tone(duration_ms: int = 300, frequency: int = 440, *,
                   pcm_format: PcmFormat = ASR_PCM, volume_pct: int = 5) -> bytes:
    """Local diagnostic tone, not synthesized speech or a cloned voice."""
    if pcm_format.channels != 1 or pcm_format.sample_width != 2:
        raise ValueError("test tone requires PCM16 mono")
    if not 20 <= duration_ms <= 5000 or not 20 <= frequency < pcm_format.sample_rate // 2 or not 0 <= volume_pct <= 100:
        raise ValueError("invalid tone parameters")
    count = pcm_format.sample_rate * duration_ms // 1000
    ramp = max(1, pcm_format.sample_rate // 100)
    samples = [round(32767 * volume_pct / 100 * min(1, i / ramp, (count - 1 - i) / ramp)
                     * math.sin(2 * math.pi * frequency * i / pcm_format.sample_rate)) for i in range(count)]
    return struct.pack(f"<{count}h", *samples)


def pcm16_rms(data: bytes) -> float:
    if len(data) % 2:
        raise ValueError("PCM16 data must contain complete samples")
    if not data:
        return 0.0
    samples = struct.unpack(f"<{len(data) // 2}h", data)
    return math.sqrt(sum(value * value for value in samples) / len(samples))


class AudioSource(Protocol):
    async def read_frame(self) -> bytes: ...


class AudioSink(Protocol):
    async def play(self, pcm: bytes, pcm_format: PcmFormat, cancel: asyncio.Event) -> None: ...


class UtteranceCollector:
    """Small energy VAD: pre-roll, speech start, trailing silence, and hard duration cap."""

    def __init__(self, *, threshold: float = 500.0, silence_ms: int = 700,
                 max_ms: int = 10_000, pre_roll_ms: int = 300, start_frames: int = 2,
                 pcm_format: PcmFormat = ASR_PCM):
        self.threshold = threshold
        self.format = pcm_format
        self.silence_frames = max(1, silence_ms // pcm_format.frame_ms)
        self.max_frames = max(1, max_ms // pcm_format.frame_ms)
        self.pre_roll_frames = max(0, pre_roll_ms // pcm_format.frame_ms)
        self.start_frames = max(1, start_frames)

    async def collect(self, source: AudioSource) -> bytes:
        pre_roll: deque[bytes] = deque(maxlen=self.pre_roll_frames)
        candidates: list[bytes] = []
        utterance: list[bytes] = []
        silent = 0
        while len(utterance) < self.max_frames:
            frame = await source.read_frame()
            if len(frame) != self.format.frame_bytes:
                raise ValueError(f"expected {self.format.frame_bytes}-byte audio frame")
            voiced = pcm16_rms(frame) >= self.threshold
            if not utterance:
                if voiced:
                    candidates.append(frame)
                    if len(candidates) < self.start_frames:
                        continue
                    utterance.extend(pre_roll)
                    utterance.extend(candidates)
                    candidates.clear()
                    continue
                else:
                    candidates.clear()
                    pre_roll.append(frame)
                    continue
            utterance.append(frame)
            silent = 0 if voiced else silent + 1
            if silent >= self.silence_frames:
                break
        return b"".join(utterance)


class MemoryAudioSource:
    def __init__(self, frames: list[bytes]):
        self.frames: asyncio.Queue[bytes] = asyncio.Queue()
        for frame in frames:
            self.frames.put_nowait(frame)

    async def read_frame(self) -> bytes:
        if self.frames.empty():
            raise EOFError("audio source exhausted")
        return await self.frames.get()


class MemoryAudioSink:
    def __init__(self):
        self.played: list[tuple[bytes, PcmFormat]] = []

    async def play(self, pcm: bytes, pcm_format: PcmFormat, cancel: asyncio.Event) -> None:
        if not cancel.is_set():
            self.played.append((pcm, pcm_format))


class SoundDeviceMicrophone:
    def __init__(self, pcm_format: PcmFormat = ASR_PCM, device: int | str | None = None):
        self.format = pcm_format
        self.device = device
        self._stream = None
        self._frames: queue.Queue[bytes] = queue.Queue(maxsize=2)
        self._callback_failed = False

    def _on_audio(self, indata, frames, time_info, status) -> None:
        if status or frames * self.format.channels * self.format.sample_width != len(indata):
            self._callback_failed = True
        data = bytes(indata)
        try:
            self._frames.put_nowait(data)
        except queue.Full:
            # Cloud processing may be busy while capture continues. Retain the
            # newest frame instead of treating expected idle-time loss as overflow.
            try:
                self._frames.get_nowait()
            except queue.Empty:
                pass
            try:
                self._frames.put_nowait(data)
            except queue.Full:
                pass

    async def read_frame(self) -> bytes:
        if self._stream is None:
            try:
                import sounddevice as sd
            except ImportError as exc:
                raise RuntimeError("install voice dependencies: pip install -e .[voice]") from exc
            stream = sd.RawInputStream(samplerate=self.format.sample_rate,
                                       blocksize=self.format.sample_rate * self.format.frame_ms // 1000,
                                       channels=self.format.channels, dtype="int16", device=self.device,
                                       callback=self._on_audio)
            try:
                stream.start()
            except Exception:
                # A failed PortAudio start can still leave a native device handle
                # allocated. Close that handle before exposing the original error.
                stream.close()
                raise
            self._stream = stream
        # Avoid an indefinitely blocked executor worker. On Windows, Ctrl+C
        # cancels this coroutine but asyncio waits for that worker at shutdown.
        while True:
            try:
                data = self._frames.get_nowait()
                break
            except queue.Empty:
                await asyncio.sleep(0.01)
        if self._callback_failed:
            self._callback_failed = False
            raise RuntimeError("microphone input overflow")
        return data

    def close(self) -> None:
        stream, self._stream = self._stream, None
        if stream is None:
            return
        try:
            stream.stop()
        finally:
            # Closing must not be skipped when PortAudio reports a stop error.
            stream.close()

    def flush(self) -> None:
        """Discard frames captured while TARS was speaking to avoid echo carry-over."""
        while True:
            try:
                self._frames.get_nowait()
            except queue.Empty:
                break
        self._callback_failed = False


class SoundDeviceSpeaker:
    def __init__(self, device: int | str | None = None):
        self.device = device

    async def play(self, pcm: bytes, pcm_format: PcmFormat, cancel: asyncio.Event) -> None:
        async def chunks() -> AsyncIterator[bytes]:
            yield pcm

        await self.play_stream(chunks(), pcm_format, cancel)

    async def play_stream(self, chunks: AsyncIterator[bytes], pcm_format: PcmFormat,
                          cancel: asyncio.Event) -> None:
        try:
            import sounddevice as sd
        except ImportError as exc:
            raise RuntimeError("install voice dependencies: pip install -e .[voice]") from exc
        stream = sd.RawOutputStream(samplerate=pcm_format.sample_rate, channels=pcm_format.channels,
                                    dtype="int16", device=self.device)
        try:
            stream.start()
        except Exception:
            stream.close()
            raise
        try:
            buffered = bytearray()
            # Keep a small cushion against network jitter without making the
            # first audible response wait for half a second of PCM.
            block_bytes = pcm_format.frame_bytes * 2
            async for chunk in chunks:
                if cancel.is_set():
                    break
                if len(chunk) % pcm_format.sample_width:
                    raise ValueError("streaming PCM chunk is not sample aligned")
                buffered.extend(chunk)
                while len(buffered) >= block_bytes:
                    block = bytes(buffered[:block_bytes])
                    del buffered[:block_bytes]
                    await asyncio.to_thread(stream.write, block)
            if buffered and not cancel.is_set():
                await asyncio.to_thread(stream.write, bytes(buffered))
        finally:
            try:
                stream.stop()
            finally:
                stream.close()


class WavAudioSource:
    """Repeatable file source for ASR/VAD debugging; accepts only the exact expected PCM format."""

    def __init__(self, path: str, pcm_format: PcmFormat = ASR_PCM):
        self.format = pcm_format
        self._wav = wave.open(path, "rb")
        actual = (self._wav.getframerate(), self._wav.getnchannels(), self._wav.getsampwidth(),
                  self._wav.getcomptype())
        expected = (pcm_format.sample_rate, pcm_format.channels, pcm_format.sample_width, "NONE")
        if actual != expected:
            self._wav.close()
            raise ValueError(f"WAV must be PCM16 mono {pcm_format.sample_rate} Hz")
        if self._wav.getnframes() == 0:
            self._wav.close()
            raise ValueError("WAV contains no audio")

    async def read_frame(self) -> bytes:
        samples = self.format.sample_rate * self.format.frame_ms // 1000
        data = self._wav.readframes(samples)
        if not data:
            raise EOFError("WAV source exhausted")
        if len(data) < self.format.frame_bytes:
            data += bytes(self.format.frame_bytes - len(data))
        return data

    def close(self) -> None:
        self._wav.close()


class WavAudioSink:
    def __init__(self, path: str):
        self.path = path

    async def play(self, pcm: bytes, pcm_format: PcmFormat, cancel: asyncio.Event) -> None:
        async def chunks() -> AsyncIterator[bytes]:
            yield pcm

        await self.play_stream(chunks(), pcm_format, cancel)

    async def play_stream(self, chunks: AsyncIterator[bytes], pcm_format: PcmFormat,
                          cancel: asyncio.Event) -> None:
        if cancel.is_set():
            return
        with wave.open(self.path, "wb") as output:
            output.setnchannels(pcm_format.channels)
            output.setsampwidth(pcm_format.sample_width)
            output.setframerate(pcm_format.sample_rate)
            async for pcm in chunks:
                if cancel.is_set():
                    break
                output.writeframesraw(pcm)
