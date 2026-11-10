from __future__ import annotations

import asyncio
import base64
import os
import time
import threading
from dataclasses import dataclass
from collections.abc import AsyncIterator, Iterator
from typing import Protocol

from .audio import ASR_PCM, TTS_PCM, PcmFormat, make_test_tone


_DASHSCOPE_SDK_LOCK = threading.Lock()
COSYVOICE_MODEL = "cosyvoice-v3.5-flash"
QWEN_VC_MODEL = "qwen3-tts-vc-2026-01-22"
QWEN_VOICE_PREFIX = "qwen-tts-vc-"


def workspace_id_is_valid(workspace_id: str | None) -> bool:
    return bool(workspace_id and workspace_id.startswith("ws-") and
                all(char.isalnum() or char == "-" for char in workspace_id))


def _workspace_url(workspace_id: str | None, *, websocket: bool = False) -> str:
    if not workspace_id:
        return ("wss://dashscope.aliyuncs.com/api-ws/v1/inference" if websocket else
                "https://dashscope.aliyuncs.com/api/v1")
    if not workspace_id_is_valid(workspace_id):
        raise ValueError("DASHSCOPE_WORKSPACE_ID must be the ws-* prefix from API Host")
    scheme = "wss" if websocket else "https"
    path = "/api-ws/v1/inference" if websocket else "/api/v1"
    return f"{scheme}://{workspace_id}.cn-beijing.maas.aliyuncs.com{path}"


def _safe_provider_code(result) -> str:
    value = result.get('code') if isinstance(result,dict) else getattr(result, "code", None)
    if value is None:
        status = result.get('status_code') if isinstance(result,dict) else getattr(result, "status_code", None)
        value = f"HTTP_{status}" if isinstance(status, int) else "provider_error"
    value = str(value)
    if not value or len(value) > 64 or value.lower().startswith(('sk-','ws-','qwen-tts-vc-')) or not all(char.isascii() and (char.isalnum() or char in "._-") for char in value):
        return "provider_error"
    return value


def speech_error_details(adapter) -> dict:
    """Only code/status, never raw exceptions, API response/message, key or voice ID."""
    code=_safe_provider_code({'code':getattr(adapter,'last_error_code',None)})
    status=getattr(adapter,'last_error_status',None)
    return {'provider_code':code,'http_status':status if type(status) is int and 100<=status<=599 else None}


class SpeechRecognizer(Protocol):
    async def transcribe(self, pcm: bytes, pcm_format: PcmFormat = ASR_PCM) -> str: ...


class SpeechSynthesizer(Protocol):
    async def synthesize(self, text: str) -> tuple[bytes, PcmFormat]: ...


@dataclass(frozen=True)
class RecognitionResult:
    text: str = ""
    status: str = "ok"
    error: str | None = None


@dataclass(frozen=True)
class SynthesisResult:
    pcm: bytes = b""
    pcm_format: PcmFormat = ASR_PCM
    status: str = "ok"  # synthesis result; playback outcome belongs to the sink/session
    error: str | None = None


@dataclass(frozen=True)
class SpeechOptions:
    language: str = "auto"
    voice: str | None = None


class ASRProvider(Protocol):
    async def recognize(self, pcm: bytes, pcm_format: PcmFormat) -> RecognitionResult: ...


class TTSProvider(Protocol):
    async def render(self, text: str, options: SpeechOptions) -> SynthesisResult: ...


class RecognizerAdapter:
    """Typed provider -> existing VoiceSession interface, no vendor selection."""
    def __init__(self, provider: ASRProvider):
        self.provider = provider

    async def transcribe(self, pcm: bytes, pcm_format: PcmFormat = ASR_PCM) -> str:
        result = await self.provider.recognize(pcm, pcm_format)
        if result.status != "ok" or result.error:
            raise RuntimeError("asr_unavailable")
        return result.text


class SynthesizerAdapter:
    def __init__(self, provider: TTSProvider, options: SpeechOptions = SpeechOptions()):
        self.provider = provider
        self.options = options

    async def synthesize(self, text: str) -> tuple[bytes, PcmFormat]:
        result = await self.provider.render(text, self.options)
        if result.status != "ok" or result.error or not result.pcm:
            raise RuntimeError("tts_unavailable")
        return result.pcm, result.pcm_format


class MockASR:
    def __init__(self, text: str = "介绍一下你的任务"):
        self.text = text

    async def recognize(self, pcm: bytes, pcm_format: PcmFormat) -> RecognitionResult:
        if not pcm or len(pcm) % 2 or pcm_format.sample_width != 2 or pcm_format.channels != 1:
            return RecognitionResult(status="error", error="invalid_pcm")
        return RecognitionResult(self.text)


class MockTTS:
    async def render(self, text: str, options: SpeechOptions) -> SynthesisResult:
        if not text.strip():
            return SynthesisResult(status="error", error="empty_text")
        return SynthesisResult(make_test_tone())


class UnconfiguredASR:
    async def recognize(self, pcm: bytes, pcm_format: PcmFormat) -> RecognitionResult:
        return RecognitionResult(status="unconfigured", error="asr_not_configured")


class UnconfiguredTTS:
    async def render(self, text: str, options: SpeechOptions) -> SynthesisResult:
        return SynthesisResult(status="unconfigured", error="tts_not_configured")


def create_offline_speech(*, mock: bool = False, text: str = "介绍一下你的任务",
                          options: SpeechOptions = SpeechOptions()):
    """Unified Stage 4 entry: only explicit mock or clearly unconfigured.
    Existing cloud adapters remain opt-in in the historical voice CLI."""
    asr_name = "mock" if mock else os.environ.get("TARS_ASR_BACKEND", "unconfigured")
    tts_name = "mock" if mock else os.environ.get("TARS_TTS_BACKEND", "unconfigured")
    if asr_name not in {"mock", "unconfigured"} or tts_name not in {"mock", "unconfigured"}:
        raise ValueError("ASR/TTS backend not installed; select mock or unconfigured")
    return (RecognizerAdapter(MockASR(text) if asr_name == "mock" else UnconfiguredASR()),
            SynthesizerAdapter(MockTTS() if tts_name == "mock" else UnconfiguredTTS(), options))


@dataclass
class ScriptedRecognizer:
    transcript: str

    async def transcribe(self, pcm: bytes, pcm_format: PcmFormat = ASR_PCM) -> str:
        return self.transcript


class RecordingSynthesizer:
    def __init__(self, audio: bytes = b"\0\0" * 1600):
        self.audio = audio
        self.texts: list[str] = []

    async def synthesize(self, text: str) -> tuple[bytes, PcmFormat]:
        self.texts.append(text)
        return self.audio, TTS_PCM


class DashScopeFunAsr:
    """Fun-ASR real-time SDK adapter. Credentials are read only at construction."""

    def __init__(self, api_key: str | None = None, *, workspace_id: str | None = None,
                 model: str = "fun-asr-realtime", pace_frames: bool = False):
        self.api_key = api_key or os.environ.get("DASHSCOPE_API_KEY")
        if not self.api_key:
            raise ValueError("DASHSCOPE_API_KEY is not configured")
        self.workspace_id = workspace_id or os.environ.get("DASHSCOPE_WORKSPACE_ID")
        self.model = model
        self.pace_frames = pace_frames
        self.last_error_code: str | None = None
        self.last_error_status: int | None = None

    async def transcribe(self, pcm: bytes, pcm_format: PcmFormat = ASR_PCM) -> str:
        if pcm_format != ASR_PCM:
            raise ValueError("Fun-ASR MVP requires PCM16 mono 16 kHz")
        return await asyncio.to_thread(self._transcribe_sync, pcm)

    def _transcribe_sync(self, pcm: bytes) -> str:
        try:
            import dashscope
            from dashscope.audio.asr import Recognition, RecognitionCallback, RecognitionResult
        except ImportError as exc:
            raise RuntimeError("install voice dependencies: pip install -e .[voice]") from exc
        self.last_error_code=None;self.last_error_status=None
        try:
            with _DASHSCOPE_SDK_LOCK:
                return self._transcribe_locked(dashscope, Recognition, RecognitionCallback, RecognitionResult, pcm)
        except Exception:
            if not self.last_error_code:self.last_error_code='asr_request_error'
            raise RuntimeError('Fun-ASR request failed') from None

    def _transcribe_locked(self, dashscope, Recognition, RecognitionCallback, RecognitionResult,
                           pcm: bytes) -> str:
        dashscope.api_key = self.api_key
        dashscope.base_websocket_api_url = _workspace_url(self.workspace_id, websocket=True)
        sentences: list[str] = []
        errors: list[str] = []
        self.last_error_code = None
        owner = self

        class Callback(RecognitionCallback):
            def on_event(self, result):
                sentence = result.get_sentence()
                if sentence and RecognitionResult.is_sentence_end(sentence):
                    text = sentence.get("text", "").strip()
                    if text:
                        sentences.append(text)

            def on_error(self, result):
                code = _safe_provider_code(result)
                owner.last_error_code = code
                status=getattr(result,'status_code',None)
                if type(status) is int:owner.last_error_status=status
                errors.append(code)

        recognition = Recognition(model=self.model, format="pcm", sample_rate=ASR_PCM.sample_rate,
                                  semantic_punctuation_enabled=False, callback=Callback())
        recognition.start()
        try:
            for offset in range(0, len(pcm), ASR_PCM.frame_bytes):
                recognition.send_audio_frame(pcm[offset:offset + ASR_PCM.frame_bytes])
                if self.pace_frames:
                    time.sleep(ASR_PCM.frame_ms / 1000)
        finally:
            recognition.stop()
        if errors:
            raise RuntimeError("Fun-ASR request failed")
        if not sentences:
            self.last_error_code='empty_transcript'
            raise RuntimeError("Fun-ASR returned no final sentence")
        return "".join(sentences)


class DashScopeCosyVoice:
    def __init__(self, voice: str | None = None, api_key: str | None = None, *,
                 workspace_id: str | None = None, model: str = COSYVOICE_MODEL):
        self.api_key = api_key or os.environ.get("DASHSCOPE_API_KEY")
        self.voice = voice or os.environ.get("DASHSCOPE_VOICE_ID")
        self.workspace_id = workspace_id or os.environ.get("DASHSCOPE_WORKSPACE_ID")
        self.model = model
        if not self.api_key:
            raise ValueError("DASHSCOPE_API_KEY is not configured")
        if not self.voice:
            raise ValueError("DASHSCOPE_VOICE_ID is not configured")

    async def synthesize(self, text: str) -> tuple[bytes, PcmFormat]:
        return await asyncio.to_thread(self._synthesize_sync, text)

    def _synthesize_sync(self, text: str) -> tuple[bytes, PcmFormat]:
        try:
            import dashscope
            from dashscope.audio.tts_v2 import HttpSpeechSynthesizer
        except ImportError as exc:
            raise RuntimeError("install voice dependencies: pip install -e .[voice]") from exc
        with _DASHSCOPE_SDK_LOCK:
            dashscope.api_key = self.api_key
            dashscope.base_http_api_url = _workspace_url(self.workspace_id)
            chunks = HttpSpeechSynthesizer.call(model=self.model, text=text, voice=self.voice,
                                               format="pcm", sample_rate=TTS_PCM.sample_rate,
                                               stream=True, api_key=self.api_key)
            audio = bytearray()
            for chunk in chunks:
                if not getattr(chunk, "audio_url", None) and getattr(chunk, "audio_data", None):
                    audio.extend(chunk.audio_data)
        if not audio:
            raise RuntimeError("CosyVoice returned no audio")
        return bytes(audio), TTS_PCM


def _field(value, name: str):
    if isinstance(value, dict):
        return value.get(name)
    return getattr(value, name, None)


class DashScopeQwenVoice:
    """Qwen3-TTS voice-clone adapter returning raw PCM16 mono 24 kHz."""

    pcm_format = TTS_PCM

    def __init__(self, voice: str | None = None, api_key: str | None = None, *,
                 workspace_id: str | None = None, model: str = QWEN_VC_MODEL,
                 language_type: str = "Chinese"):
        self.api_key = api_key or os.environ.get("DASHSCOPE_API_KEY")
        self.voice = voice or os.environ.get("DASHSCOPE_VOICE_ID")
        self.workspace_id = workspace_id or os.environ.get("DASHSCOPE_WORKSPACE_ID")
        self.model = model
        self.language_type = language_type
        self.last_error_code: str | None = None
        self.last_error_status: int | None = None
        if not self.api_key:
            raise ValueError("DASHSCOPE_API_KEY is not configured")
        if not self.voice:
            raise ValueError("DASHSCOPE_VOICE_ID is not configured")
        if self.model != QWEN_VC_MODEL:
            raise ValueError(f"this release supports Qwen voice model {QWEN_VC_MODEL}")

    async def synthesize(self, text: str) -> tuple[bytes, PcmFormat]:
        return await asyncio.to_thread(self._synthesize_sync, text)

    def _synthesize_sync(self, text: str) -> tuple[bytes, PcmFormat]:
        audio = bytearray()
        for data in self._stream_sync(text):
            audio.extend(data)
        if not audio:
            self.last_error_code='empty_audio'
            raise RuntimeError("Qwen TTS returned no audio")
        return bytes(audio), TTS_PCM

    def _stream_sync(self, text: str) -> Iterator[bytes]:
        self.last_error_code=None;self.last_error_status=None
        try:
            import dashscope
        except ImportError as exc:
            raise RuntimeError("install voice dependencies: pip install -e .[voice]") from exc
        try:
            with _DASHSCOPE_SDK_LOCK:
                dashscope.api_key = self.api_key
                dashscope.base_http_api_url = _workspace_url(self.workspace_id)
                chunks = dashscope.MultiModalConversation.call(
                    model=self.model, api_key=self.api_key, text=text, voice=self.voice,
                    language_type=self.language_type, stream=True)
                for chunk in chunks:
                    status = _field(chunk, "status_code")
                    if status is not None and int(status) != 200:
                        self.last_error_status=int(status)
                        self.last_error_code=_safe_provider_code(chunk)
                        raise RuntimeError("Qwen TTS request failed")
                    output = _field(chunk, "output")
                    audio_result = _field(output, "audio") if output is not None else None
                    encoded = _field(audio_result, "data") if audio_result is not None else None
                    if encoded:
                        data = base64.b64decode(encoded, validate=True)
                        if len(data) % TTS_PCM.sample_width:
                            raise RuntimeError("Qwen TTS returned invalid PCM")
                        if data:
                            yield data
        except Exception as exc:
            if not self.last_error_code:
                self.last_error_code={'ConnectionError':'network_error','ConnectTimeout':'network_timeout',
                                      'ReadTimeout':'network_timeout','TimeoutError':'network_timeout',
                                      'SSLError':'tls_error','ProxyError':'proxy_error',
                                      'InvalidApiKey':'InvalidApiKey','InvalidParameter':'InvalidParameter'}.get(type(exc).__name__,'tts_request_error')
            raise RuntimeError("Qwen TTS request failed") from None

    async def stream(self, text: str) -> AsyncIterator[bytes]:
        """Bridge provider PCM chunks to playback without buffering the full reply."""
        loop = asyncio.get_running_loop()
        queue_: asyncio.Queue[bytes | Exception | None] = asyncio.Queue()

        def publish(item: bytes | Exception | None) -> None:
            try:
                loop.call_soon_threadsafe(queue_.put_nowait, item)
            except RuntimeError:
                pass

        def produce() -> None:
            try:
                for data in self._stream_sync(text):
                    publish(data)
            except Exception as exc:
                publish(exc)
            finally:
                publish(None)

        threading.Thread(target=produce, name="qwen-tts-stream", daemon=True).start()
        received = False
        while True:
            item = await queue_.get()
            if item is None:
                break
            if isinstance(item, Exception):
                raise item
            received = True
            yield item
        if not received:
            self.last_error_code='empty_audio'
            raise RuntimeError("Qwen TTS returned no audio")


def create_dashscope_synthesizer(voice: str | None = None, api_key: str | None = None, *,
                                 workspace_id: str | None = None,
                                 model: str | None = None) -> SpeechSynthesizer:
    """Select the provider adapter without exposing credentials or voice identifiers."""
    resolved_voice = voice or os.environ.get("DASHSCOPE_VOICE_ID")
    resolved_model = model or os.environ.get("DASHSCOPE_TTS_MODEL")
    if resolved_voice and resolved_voice.startswith(QWEN_VOICE_PREFIX):
        if resolved_model and resolved_model != QWEN_VC_MODEL:
            raise ValueError(f"Qwen voice requires DASHSCOPE_TTS_MODEL={QWEN_VC_MODEL}")
        return DashScopeQwenVoice(resolved_voice, api_key, workspace_id=workspace_id,
                                  model=QWEN_VC_MODEL)
    if resolved_model == QWEN_VC_MODEL:
        return DashScopeQwenVoice(resolved_voice, api_key, workspace_id=workspace_id,
                                  model=resolved_model)
    if resolved_model and resolved_model != COSYVOICE_MODEL:
        raise ValueError("unsupported DASHSCOPE_TTS_MODEL")
    return DashScopeCosyVoice(resolved_voice, api_key, workspace_id=workspace_id,
                              model=resolved_model or COSYVOICE_MODEL)
