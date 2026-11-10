from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable, Protocol


ALLOWED_STAGES = {"capture", "asr", "brain", "tts", "playback", "turn"}
ALLOWED_OUTCOMES = {"ok", "error", "skipped", "cancelled"}
ALLOWED_CODES = {"none", "circuit_open", "timeout", "provider_error", "empty_transcript",
                 "wake_word_missing", "speech_suppressed", "before_start", "backend_error",
                 "barge_in", "processing_error"}


@dataclass(frozen=True)
class VoiceEvent:
    stage: str
    outcome: str
    code: str
    duration_ms: int
    timestamp_ms: int

    @classmethod
    def create(cls, stage: str, outcome: str, code: str = "none",
               duration_ms: int = 0) -> "VoiceEvent":
        if stage not in ALLOWED_STAGES or outcome not in ALLOWED_OUTCOMES or code not in ALLOWED_CODES:
            raise ValueError("invalid voice event classification")
        return cls(stage, outcome, code, max(0, duration_ms), int(time.time() * 1000))


class VoiceObserver(Protocol):
    def emit(self, event: VoiceEvent) -> None: ...


class SafeJsonlObserver:
    """Writes an allowlisted schema: never transcript, response text, IDs, or exceptions."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def emit(self, event: VoiceEvent) -> None:
        line = json.dumps(asdict(event), ensure_ascii=True, separators=(",", ":"))
        with self.path.open("a", encoding="utf-8", newline="\n") as stream:
            stream.write(line + "\n")


class MetricsObserver:
    def __init__(self):
        self.counts: dict[str, int] = {}
        self.duration_ms: dict[str, int] = {}

    def emit(self, event: VoiceEvent) -> None:
        key = f"{event.stage}.{event.outcome}.{event.code}"
        self.counts[key] = self.counts.get(key, 0) + 1
        self.duration_ms[event.stage] = self.duration_ms.get(event.stage, 0) + event.duration_ms

    def summary(self) -> dict[str, dict[str, int]]:
        return {"counts": dict(self.counts), "duration_ms": dict(self.duration_ms)}


class CompositeObserver:
    def __init__(self, *observers: VoiceObserver):
        self.observers = observers

    def emit(self, event: VoiceEvent) -> None:
        for observer in self.observers:
            try:
                observer.emit(event)
            except Exception:
                pass


class CircuitBreaker:
    def __init__(self, failure_threshold: int = 3, cooldown_s: float = 30.0,
                 clock: Callable[[], float] = time.monotonic):
        if failure_threshold < 1 or cooldown_s < 0:
            raise ValueError("invalid circuit breaker settings")
        self.failure_threshold = failure_threshold
        self.cooldown_s = cooldown_s
        self.clock = clock
        self.failures = 0
        self.opened_at: float | None = None

    def allow(self) -> bool:
        if self.opened_at is None:
            return True
        if self.clock() - self.opened_at >= self.cooldown_s:
            self.opened_at = None
            self.failures = max(0, self.failure_threshold - 1)
            return True
        return False

    def success(self) -> None:
        self.failures = 0
        self.opened_at = None

    def failure(self) -> None:
        self.failures += 1
        if self.failures >= self.failure_threshold:
            self.opened_at = self.clock()
