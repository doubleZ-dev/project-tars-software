"""Bounded half-duplex MOCK audio channel; no USB/Wi-Fi device implementation.

This is the replaceable PC audio boundary while native USB audio wire framing,
flow control and measured throughput remain unaccepted. Control uses TarsClient
independently and never shares this queue.
"""
from __future__ import annotations

import asyncio
from .audio import ASR_PCM, PcmFormat


class MockBodyAudioChannel:
    def __init__(self, capacity_frames: int = 8):
        self.capture = asyncio.Queue(maxsize=capacity_frames)
        self.connected = True
        self.epoch = 0
        self.mode = "idle"
        self.played = bytearray()

    def _clear(self):
        while not self.capture.empty():
            self.capture.get_nowait()

    def disconnect(self):
        self.connected = False
        self.epoch += 1
        self.mode = "idle"
        self._clear()

    def reconnect(self):
        self._clear()
        self.connected = True
        self.epoch += 1
        self.mode = "idle"

    def begin(self, mode: str) -> int:
        if not self.connected:
            raise ConnectionError("body_audio_disconnected")
        if self.mode != "idle" or mode not in {"capture", "playback"}:
            raise RuntimeError("half_duplex_busy")
        self.mode = mode
        return self.epoch

    def end(self, epoch: int):
        if epoch == self.epoch:
            self.mode = "idle"
            self._clear()

    def feed_capture(self, frame: bytes):
        if not self.connected or self.mode == "playback":
            raise RuntimeError("capture_not_allowed")
        if len(frame) != ASR_PCM.frame_bytes:
            raise ValueError("invalid_pcm_frame")
        try:
            self.capture.put_nowait(frame)
        except asyncio.QueueFull:
            raise BufferError("capture_overflow") from None

    def check(self, epoch: int, mode: str):
        if not self.connected or epoch != self.epoch:
            raise ConnectionError("body_audio_session_lost")
        if self.mode != mode:
            raise RuntimeError("half_duplex_state_changed")


class BodyAudioSource:
    def __init__(self, channel: MockBodyAudioChannel):
        self.channel = channel
        self.epoch = -1

    async def start_capture(self):
        self.epoch = self.channel.begin("capture")

    async def stop_capture(self):
        if self.channel.mode == "capture":
            self.channel.end(self.epoch)

    async def read_frame(self) -> bytes:
        while True:
            self.channel.check(self.epoch, "capture")
            try:
                frame = await asyncio.wait_for(self.channel.capture.get(), .05)
            except TimeoutError:
                continue
            self.channel.check(self.epoch, "capture")
            return frame


class BodyAudioSink:
    def __init__(self, channel: MockBodyAudioChannel):
        self.channel = channel
        self.status = "idle"

    async def play(self, pcm: bytes, pcm_format: PcmFormat, cancel: asyncio.Event):
        if (pcm_format.sample_rate, pcm_format.channels, pcm_format.sample_width) != (16000, 1, 2) or len(pcm) % 2:
            raise ValueError("body_audio_requires_16k_pcm16_mono")
        if len(pcm) > 320_000:
            raise ValueError("playback_exceeds_10s_cap")
        epoch = self.channel.begin("playback")
        self.status = "playing"
        self.channel.played.clear()
        try:
            for offset in range(0, len(pcm), 640):
                self.channel.check(epoch, "playback")
                if cancel.is_set():
                    self.status = "cancelled"
                    return
                self.channel.played.extend(pcm[offset:offset + 640])
                await asyncio.sleep(0)  # yield to control/estop; no real device or real-time claim
            self.status = "completed"
        except BaseException:
            self.status = "failed"
            raise
        finally:
            self.channel.end(epoch)
