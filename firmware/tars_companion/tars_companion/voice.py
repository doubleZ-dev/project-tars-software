from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from .audio import AudioSink, AudioSource, UtteranceCollector
from .brain import BrainResponse, TarsBrain
from .speech import SpeechRecognizer, SpeechSynthesizer
from .observability import CircuitBreaker, VoiceEvent, VoiceObserver


@dataclass(frozen=True)
class VoiceTurn:
    transcript: str
    response: BrainResponse
    speech_error: str | None = None


@dataclass(frozen=True)
class _WakeMatch:
    text: str
    precise: bool


class VoiceStageError(RuntimeError):
    """A deliberately detail-free boundary error; provider messages may contain credentials."""

    def __init__(self, stage: str, code: str):
        self.stage = stage
        self.code = code
        super().__init__(f"{stage}:{code}")


class VoiceSession:
    _FUZZY_WAKE_ALIASES = ("TAS", "task", "Pars", "Pass", "帕尔斯")

    def __init__(self, brain: TarsBrain, recognizer: SpeechRecognizer,
                 synthesizer: SpeechSynthesizer, sink: AudioSink,
                 collector: UtteranceCollector | None = None, wake_word: str | None = None,
                 asr_timeout: float = 30.0, tts_timeout: float = 30.0,
                 playback_timeout: float = 60.0, max_speech_chars: int = 300,
                 observer: VoiceObserver | None = None,
                 asr_breaker: CircuitBreaker | None = None,
                 tts_breaker: CircuitBreaker | None = None,
                 conversation_window_s: float = 30.0,
                 clock: Callable[[], float] = time.monotonic,
                 dialogue_publisher: Callable[[str, str], Awaitable[object]] | None = None,
                 interaction_publisher: Callable[[str], Awaitable[object]] | None = None,
                 capture_timeout: float = 30.0, brain_timeout: float = 20.0):
        self.brain = brain
        self.recognizer = recognizer
        self.synthesizer = synthesizer
        self.sink = sink
        self.collector = collector or UtteranceCollector()
        self.wake_word = wake_word
        self.asr_timeout = asr_timeout
        self.tts_timeout = tts_timeout
        self.playback_timeout = playback_timeout
        self.max_speech_chars = max_speech_chars
        self.observer = observer
        self.asr_breaker = asr_breaker or CircuitBreaker()
        self.tts_breaker = tts_breaker or CircuitBreaker()
        self.conversation_window_s = max(0.0, min(float(conversation_window_s), 300.0))
        self._clock = clock
        self.dialogue_publisher = dialogue_publisher
        self.interaction_publisher = interaction_publisher
        self.interaction_state = "IDLE"
        self.capture_timeout = capture_timeout
        self.brain_timeout = brain_timeout
        self._conversation_until = 0.0
        self._playback_cancel = asyncio.Event()
        self._playback_task: asyncio.Task[None] | None = None
        self._turn_lock = asyncio.Lock()

    def barge_in(self) -> None:
        """Stop current speech immediately. The caller may then start a new capture."""
        self._playback_cancel.set()

    async def run_turn(self, source: AudioSource) -> VoiceTurn:
        async with self._turn_lock:
            try:
                start = getattr(source, "start_capture", None)
                if callable(start):
                    try:
                        await asyncio.wait_for(start(), 5)
                    except Exception:
                        raise VoiceStageError("capture", "start_failed") from None
                await self._interaction("LISTENING")
                return await self._run_turn(source)
            finally:
                self._playback_cancel.set()
                stop = getattr(source, "stop_capture", None)
                if callable(stop):
                    try:
                        await asyncio.wait_for(stop(), 1)
                    except Exception:
                        pass
                await self._interaction("IDLE")

    async def _interaction(self, state: str) -> None:
        self.interaction_state = state
        if self.interaction_publisher:
            try:
                await asyncio.wait_for(self.interaction_publisher(state), 1)
            except Exception:
                self._emit("interaction", "error", "publish_failed")

    async def _run_turn(self, source: AudioSource) -> VoiceTurn:
        turn_started = time.perf_counter()
        capture_started = time.perf_counter()
        try:
            pcm = await asyncio.wait_for(self.collector.collect(source), self.capture_timeout)
            stop = getattr(source, "stop_capture", None)
            if callable(stop):
                await asyncio.wait_for(stop(), 1)
        except Exception:
            raise VoiceStageError("capture", "input_failed") from None
        self._emit("capture", "ok", duration_ms=self._elapsed(capture_started))
        await self._interaction("THINKING")
        if not self.asr_breaker.allow():
            self._emit("asr", "skipped", "circuit_open")
            raise VoiceStageError("asr", "circuit_open")
        asr_started = time.perf_counter()
        try:
            transcript = (await asyncio.wait_for(self.recognizer.transcribe(pcm), self.asr_timeout)).strip()
        except TimeoutError:
            self.asr_breaker.failure(); self._emit("asr", "error", "timeout", self._elapsed(asr_started))
            raise VoiceStageError("asr", "timeout") from None
        except Exception:
            self.asr_breaker.failure(); self._emit("asr", "error", "provider_error", self._elapsed(asr_started))
            raise VoiceStageError("asr", "provider_error") from None
        if not transcript:
            self._emit("asr", "error", "empty_transcript", self._elapsed(asr_started))
            raise VoiceStageError("asr", "empty_transcript")
        self.asr_breaker.success(); self._emit("asr", "ok", duration_ms=self._elapsed(asr_started))
        command_text = transcript
        precise_wake = True
        if self.wake_word:
            separators = " \t，,：:。.!！?？"
            wake_match = self._addressed_text(transcript, self.wake_word, separators)
            if wake_match is None:
                if self._clock() >= self._conversation_until:
                    self._emit("turn", "skipped", "wake_word_missing", self._elapsed(turn_started))
                    return VoiceTurn(transcript, BrainResponse("未检测到唤醒词。", should_speak=False))
                command_text = transcript
                precise_wake = False
            else:
                command_text = wake_match.text
                precise_wake = wake_match.precise
            self._extend_conversation_window()
            if not command_text:
                return VoiceTurn(transcript, BrainResponse("我在。", should_speak=True))
        await self._publish_dialogue("user", command_text)
        brain_started = time.perf_counter()
        try:
            response = await asyncio.wait_for(
                self.brain.handle_text(command_text, precise_wake=precise_wake), self.brain_timeout)
        except Exception:
            self._emit("brain", "error", "processing_error", self._elapsed(brain_started))
            self._emit("turn", "error", "processing_error", self._elapsed(turn_started))
            raise VoiceStageError("brain", "processing_error") from None
        self._emit("brain", "ok", duration_ms=self._elapsed(brain_started))
        if response.text.strip():
            await self._publish_dialogue("tars", response.text)
        if response.should_speak and response.text.strip():
            error = await self._speak(response.text)
            if error is None:
                self._extend_conversation_window()
            self._emit("turn", "error" if error else "ok", error or "none", self._elapsed(turn_started))
            return VoiceTurn(transcript, response, error)
        self._emit("tts", "skipped", "speech_suppressed")
        self._emit("turn", "ok", duration_ms=self._elapsed(turn_started))
        return VoiceTurn(transcript, response)

    async def retry_speech(self, turn: VoiceTurn) -> VoiceTurn:
        """Retry only TTS/playback; never re-enters Brain or replays a physical command."""
        if not turn.response.should_speak or not turn.response.text.strip():
            return turn
        async with self._turn_lock:
            try:
                await self._interaction("THINKING")
                error = await self._speak(turn.response.text)
                if error is None:
                    self._extend_conversation_window()
            finally:
                await self._interaction("IDLE")
        return VoiceTurn(turn.transcript, turn.response, error)

    def _extend_conversation_window(self) -> None:
        if self.wake_word:
            self._conversation_until = self._clock() + self.conversation_window_s

    async def _publish_dialogue(self, role: str, text: str) -> None:
        """Best-effort display output; a screen fault must not break a voice turn."""
        if not self.dialogue_publisher or not text.strip():
            return
        try:
            await self.dialogue_publisher(role, text.strip())
        except Exception:
            pass

    async def _speak(self, text: str) -> str | None:
        self._playback_cancel = asyncio.Event()
        text = text[:self.max_speech_chars]
        if not self.tts_breaker.allow():
            self._emit("tts", "skipped", "circuit_open")
            return "tts_circuit_open"
        stream_audio = getattr(self.synthesizer, "stream", None)
        play_stream = getattr(self.sink, "play_stream", None)
        pcm_format = getattr(self.synthesizer, "pcm_format", None)
        if callable(stream_audio) and callable(play_stream) and pcm_format is not None:
            return await self._speak_streaming(text, stream_audio, play_stream, pcm_format)
        tts_started = time.perf_counter()
        try:
            audio, pcm_format = await asyncio.wait_for(self.synthesizer.synthesize(text), self.tts_timeout)
        except TimeoutError:
            self.tts_breaker.failure(); self._emit("tts", "error", "timeout", self._elapsed(tts_started))
            return "tts_timeout"
        except Exception:
            self.tts_breaker.failure(); self._emit("tts", "error", "provider_error", self._elapsed(tts_started))
            return "tts_failed"
        self.tts_breaker.success(); self._emit("tts", "ok", duration_ms=self._elapsed(tts_started))
        if self._playback_cancel.is_set():
            self._emit("playback", "cancelled", "before_start")
            return "playback_cancelled"
        playback_started = time.perf_counter()
        await self._interaction("SPEAKING")
        self._playback_task = asyncio.create_task(
            self.sink.play(audio, pcm_format, self._playback_cancel), name="tars-voice-playback")
        try:
            await asyncio.wait_for(self._playback_task, self.playback_timeout)
        except TimeoutError:
            self._playback_cancel.set()
            self._emit("playback", "error", "timeout", self._elapsed(playback_started))
            return "playback_timeout"
        except Exception:
            self._emit("playback", "error", "backend_error", self._elapsed(playback_started))
            return "playback_failed"
        finally:
            self._playback_task = None
        if self._playback_cancel.is_set():
            self._emit("playback", "cancelled", "barge_in", self._elapsed(playback_started))
            return "playback_cancelled"
        self._emit("playback", "ok", duration_ms=self._elapsed(playback_started))
        return None

    async def _speak_streaming(self, text, stream_audio, play_stream, pcm_format) -> str | None:
        """Start playback on the first provider PCM chunk instead of full buffering."""
        tts_started = time.perf_counter()
        chunks = stream_audio(text)
        try:
            first = await asyncio.wait_for(anext(chunks), self.tts_timeout)
        except (TimeoutError, StopAsyncIteration):
            self.tts_breaker.failure(); self._emit("tts", "error", "timeout", self._elapsed(tts_started))
            return "tts_timeout"
        except Exception:
            self.tts_breaker.failure(); self._emit("tts", "error", "provider_error", self._elapsed(tts_started))
            return "tts_failed"
        self.tts_breaker.success(); self._emit("tts", "ok", duration_ms=self._elapsed(tts_started))
        if self._playback_cancel.is_set():
            self._emit("playback", "cancelled", "before_start")
            return "playback_cancelled"

        async def with_first():
            yield first
            async for chunk in chunks:
                yield chunk

        playback_started = time.perf_counter()
        await self._interaction("SPEAKING")
        self._playback_task = asyncio.create_task(
            play_stream(with_first(), pcm_format, self._playback_cancel), name="tars-voice-playback")
        try:
            await asyncio.wait_for(self._playback_task, self.playback_timeout)
        except TimeoutError:
            self._playback_cancel.set()
            self._emit("playback", "error", "timeout", self._elapsed(playback_started))
            return "playback_timeout"
        except Exception:
            self._emit("playback", "error", "backend_error", self._elapsed(playback_started))
            return "playback_failed"
        finally:
            self._playback_task = None
        if self._playback_cancel.is_set():
            self._emit("playback", "cancelled", "barge_in", self._elapsed(playback_started))
            return "playback_cancelled"
        self._emit("playback", "ok", duration_ms=self._elapsed(playback_started))
        return None

    @staticmethod
    def _elapsed(started: float) -> int:
        return max(0, round((time.perf_counter() - started) * 1000))

    @staticmethod
    def _ends_with_cjk(text: str) -> bool:
        """Chinese wake aliases may be directly followed by an unspaced command."""
        if not text:
            return False
        codepoint = ord(text[-1])
        return (0x3400 <= codepoint <= 0x4DBF or 0x4E00 <= codepoint <= 0x9FFF or
                0xF900 <= codepoint <= 0xFAFF)

    @classmethod
    def _addressed_text(cls, transcript: str, wake_words: str,
                        separators: str) -> _WakeMatch | None:
        """Remove an edge wake alias and retain whether ASR heard it precisely."""
        normalized = transcript.strip(separators)
        configured_aliases = [word.strip() for word in wake_words.split("|") if word.strip()]
        fuzzy_folded = {word.casefold() for word in cls._FUZZY_WAKE_ALIASES}
        precise_aliases = [word for word in configured_aliases
                           if word.casefold() not in fuzzy_folded]
        fuzzy_aliases = list(dict.fromkeys(
            [*cls._FUZZY_WAKE_ALIASES,
             *(word for word in configured_aliases if word.casefold() in fuzzy_folded)]))
        candidates = [normalized]
        folded = normalized.casefold()
        for greeting in ("你好", "您好", "嗨", "嘿", "hello", "hi", "hey",
                         "所以", "那么", "然后", "好", "好的"):
            end = len(greeting)
            if folded.startswith(greeting.casefold()) and (
                    len(normalized) == end or normalized[end] in separators):
                remainder = normalized[end:].lstrip(separators)
                if remainder:
                    candidates.append(remainder)
                break
        for precise, aliases in ((True, precise_aliases), (False, fuzzy_aliases)):
            for candidate in candidates:
                candidate_folded = candidate.casefold()
                for word in aliases:
                    if candidate_folded.startswith(word.casefold()) and (
                            len(candidate) == len(word) or candidate[len(word)] in separators or
                            cls._ends_with_cjk(candidate[len(word)]) or
                            cls._ends_with_cjk(word)):
                        return _WakeMatch(candidate[len(word):].lstrip(separators), precise)
                for word in aliases:
                    start = len(candidate) - len(word)
                    if start >= 0 and candidate_folded.endswith(word.casefold()) and (
                            start == 0 or candidate[start - 1] in separators or
                            cls._ends_with_cjk(candidate[start - 1]) or
                            cls._ends_with_cjk(word)):
                        return _WakeMatch(candidate[:start].rstrip(separators), precise)
        return None

    def _emit(self, stage: str, outcome: str, code: str = "none", duration_ms: int = 0) -> None:
        if not self.observer:
            return
        try:
            self.observer.emit(VoiceEvent.create(stage, outcome, code, duration_ms))
        except Exception:
            pass
