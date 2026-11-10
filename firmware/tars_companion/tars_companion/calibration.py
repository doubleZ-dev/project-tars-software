from __future__ import annotations

import struct
from dataclasses import dataclass

from .audio import ASR_PCM, AudioSource, PcmFormat, pcm16_rms


@dataclass(frozen=True)
class CalibrationResult:
    noise_rms: float
    p95_rms: float
    threshold: float
    clipping_ratio: float
    usable: bool
    warnings: tuple[str, ...]


def calibrate_frames(frames: list[bytes], *, pcm_format: PcmFormat = ASR_PCM,
                     multiplier: float = 3.0, minimum: float = 250.0,
                     maximum: float = 4000.0) -> CalibrationResult:
    if len(frames) < 10:
        raise ValueError("calibration requires at least 1 second of audio")
    rms_values: list[float] = []
    clipped = 0
    sample_count = 0
    for frame in frames:
        if len(frame) != pcm_format.frame_bytes:
            raise ValueError("invalid calibration frame size")
        rms_values.append(pcm16_rms(frame))
        samples = struct.unpack(f"<{len(frame) // 2}h", frame)
        sample_count += len(samples)
        clipped += sum(abs(value) >= 32760 for value in samples)
    ordered = sorted(rms_values)
    median = ordered[len(ordered) // 2]
    p95 = ordered[min(len(ordered) - 1, int((len(ordered) - 1) * 0.95))]
    threshold = min(maximum, max(minimum, p95 * multiplier))
    clipping_ratio = clipped / sample_count if sample_count else 0.0
    warnings: list[str] = []
    if clipping_ratio > 0.001:
        warnings.append("input_clipping")
    if p95 > max(800.0, median * 3.0):
        warnings.append("speech_or_transient_detected")
    if threshold >= maximum:
        warnings.append("noise_too_high")
    return CalibrationResult(median, p95, threshold, clipping_ratio, not warnings, tuple(warnings))


async def calibrate_source(source: AudioSource, seconds: float = 2.0,
                           pcm_format: PcmFormat = ASR_PCM, warmup_ms: int = 500) -> CalibrationResult:
    if not 1.0 <= seconds <= 10.0:
        raise ValueError("calibration duration must be 1..10 seconds")
    count = max(10, round(seconds * 1000 / pcm_format.frame_ms))
    for _ in range(max(0, warmup_ms // pcm_format.frame_ms)):
        await source.read_frame()
    frames = [await source.read_frame() for _ in range(count)]
    return calibrate_frames(frames, pcm_format=pcm_format)
