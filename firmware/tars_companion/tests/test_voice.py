import asyncio
import json
import struct
import unittest
import tempfile
import types
import sys
import urllib.error
from pathlib import Path
from unittest.mock import AsyncMock, call, patch

from tars_companion.audio import (ASR_PCM, MemoryAudioSink, MemoryAudioSource, SoundDeviceMicrophone,
                                  SoundDeviceSpeaker,
                                  TTS_PCM, UtteranceCollector, pcm16_rms)
from tars_companion.brain import BrainResponse, TarsBrain
from tars_companion.client import TarsClient
from tars_companion.llm import ScriptedProvider
from tars_companion.simulator import SimulatedTarsDevice
from tars_companion.speech import RecordingSynthesizer, ScriptedRecognizer, workspace_id_is_valid
from tars_companion.transport import memory_transport_pair
from tars_companion.voice import VoiceSession
from tars_companion.voice import VoiceStageError
from tars_companion.voice_doctor import run_checks
from tars_companion.observability import CircuitBreaker, MetricsObserver, SafeJsonlObserver, VoiceEvent
from tars_companion.voice_cli import _audio_device


def frame(level: int) -> bytes:
    return struct.pack("<h", level) * (ASR_PCM.frame_bytes // 2)


class AudioTests(unittest.IsolatedAsyncioTestCase):
    def test_audio_device_index_is_parsed_as_integer(self):
        self.assertEqual(_audio_device("2"), 2)
        self.assertEqual(_audio_device("Philips SPA2100"), "Philips SPA2100")

    async def test_idle_microphone_read_is_immediately_cancellable(self):
        microphone = SoundDeviceMicrophone()
        microphone._stream = object()
        task = asyncio.create_task(microphone.read_frame())
        await asyncio.sleep(0.02)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await asyncio.wait_for(task, 0.2)

    async def test_vad_keeps_preroll_and_stops_after_silence(self):
        frames = [frame(0)] * 3 + [frame(1200)] * 2 + [frame(0)] * 7
        pcm = await UtteranceCollector(threshold=500).collect(MemoryAudioSource(frames))
        self.assertEqual(len(pcm), len(frames) * ASR_PCM.frame_bytes)
        self.assertEqual(pcm16_rms(frame(1200)), 1200)

    async def test_invalid_frame_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "audio frame"):
            await UtteranceCollector().collect(MemoryAudioSource([b"short"]))

    async def test_sounddevice_microphone_uses_callback_mode(self):
        expected = frame(321)

        class FakeStream:
            def __init__(self, **kwargs):
                self.callback = kwargs["callback"]
                self.kwargs = kwargs

            def start(self):
                self.callback(expected, ASR_PCM.sample_rate * ASR_PCM.frame_ms // 1000,
                              None, None)

            def stop(self): pass
            def close(self): pass

        fake_sd = types.SimpleNamespace(RawInputStream=FakeStream)
        microphone = SoundDeviceMicrophone(device=33)
        with patch.dict(sys.modules, {"sounddevice": fake_sd}):
            actual = await microphone.read_frame()
        self.assertEqual(actual, expected)
        self.assertEqual(microphone._stream.kwargs["device"], 33)
        microphone.close()

    async def test_sounddevice_closes_stream_when_start_fails(self):
        streams = []

        class FakeStream:
            def __init__(self, **kwargs):
                self.closed = False
                streams.append(self)

            def start(self):
                raise RuntimeError("driver refused stream")

            def close(self):
                self.closed = True

        microphone = SoundDeviceMicrophone(device=26)
        fake_sd = types.SimpleNamespace(RawInputStream=FakeStream)
        with patch.dict(sys.modules, {"sounddevice": fake_sd}):
            with self.assertRaisesRegex(RuntimeError, "driver refused"):
                await microphone.read_frame()
        self.assertTrue(streams[0].closed)
        self.assertIsNone(microphone._stream)

    def test_sounddevice_close_survives_stop_error(self):
        class FakeStream:
            closed = False

            def stop(self):
                raise RuntimeError("already stopped")

            def close(self):
                self.closed = True

        microphone = SoundDeviceMicrophone()
        stream = FakeStream()
        microphone._stream = stream
        with self.assertRaisesRegex(RuntimeError, "already stopped"):
            microphone.close()
        self.assertTrue(stream.closed)
        self.assertIsNone(microphone._stream)

    async def test_sounddevice_speaker_closes_when_start_fails(self):
        streams = []

        class FakeStream:
            def __init__(self, **kwargs):
                self.closed = False
                streams.append(self)

            def start(self):
                raise RuntimeError("output start failed")

            def close(self):
                self.closed = True

        fake_sd = types.SimpleNamespace(RawOutputStream=FakeStream)
        with patch.dict(sys.modules, {"sounddevice": fake_sd}):
            with self.assertRaisesRegex(RuntimeError, "output start failed"):
                await SoundDeviceSpeaker().play(b"\0\0", TTS_PCM, asyncio.Event())
        self.assertTrue(streams[0].closed)

    async def test_sounddevice_speaker_closes_when_stop_fails(self):
        streams = []

        class FakeStream:
            def __init__(self, **kwargs):
                self.closed = False
                streams.append(self)

            def start(self): pass
            def write(self, data): pass
            def stop(self): raise RuntimeError("output stop failed")
            def close(self): self.closed = True

        fake_sd = types.SimpleNamespace(RawOutputStream=FakeStream)
        with patch.dict(sys.modules, {"sounddevice": fake_sd}):
            with self.assertRaisesRegex(RuntimeError, "output stop failed"):
                await SoundDeviceSpeaker().play(b"\0\0", TTS_PCM, asyncio.Event())
        self.assertTrue(streams[0].closed)


class SlowSink:
    def __init__(self):
        self.cancelled = False

    async def play(self, pcm, pcm_format, cancel):
        while not cancel.is_set():
            await asyncio.sleep(0.001)
        self.cancelled = True


class FailOnceSynthesizer:
    def __init__(self):
        self.calls = 0

    async def synthesize(self, text):
        self.calls += 1
        if self.calls == 1:
            raise RuntimeError("provider detail must not escape")
        return b"\1\0" * 100, TTS_PCM


class FakeClock:
    def __init__(self):
        self.value = 0.0

    def __call__(self):
        return self.value

    def advance(self, seconds):
        self.value += seconds


class ClockAdvancingSink(MemoryAudioSink):
    def __init__(self, clock, seconds):
        super().__init__()
        self.clock = clock
        self.seconds = seconds

    async def play(self, pcm, pcm_format, cancel):
        self.clock.advance(self.seconds)
        await super().play(pcm, pcm_format, cancel)


class SequenceBrain:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    async def handle_text(self, text, *, precise_wake=True):
        self.calls.append((text, precise_wake))
        return self.responses.pop(0)


class RaisingRecognizer:
    def __init__(self):
        self.calls = 0

    async def transcribe(self, pcm, pcm_format=ASR_PCM):
        self.calls += 1
        raise RuntimeError("auth sk-phase7-secret workspace ws-secret")


class GateRecognizer:
    def __init__(self):
        self.calls = 0
        self.active = 0
        self.max_active = 0
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def transcribe(self, pcm, pcm_format=ASR_PCM):
        self.calls += 1
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        if self.calls == 1:
            self.entered.set()
            await self.release.wait()
        self.active -= 1
        return "你好"


class AlwaysFailSynthesizer:
    def __init__(self):
        self.calls = 0

    async def synthesize(self, text):
        self.calls += 1
        raise RuntimeError("SECRET_EXCEPTION_SENTINEL Bearer secret-token")


class VoiceSessionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        pc, device_side = memory_transport_pair()
        self.device = SimulatedTarsDevice(device_side, motion_delay=0.001)
        await self.device.start()
        self.client = TarsClient(pc)
        await self.client.start()
        await self.client.arm()

    async def asyncTearDown(self):
        await self.client.close()
        await self.device.close()

    def source(self):
        return MemoryAudioSource([frame(1000), frame(1000), *([frame(0)] * 7)])

    async def test_voice_motion_uses_existing_safety_path_and_speaks_result(self):
        tts = RecordingSynthesizer(b"\1\0" * 100)
        sink = MemoryAudioSink()
        session = VoiceSession(TarsBrain(self.client), ScriptedRecognizer("前进两步"), tts, sink)
        turn = await session.run_turn(self.source())
        self.assertEqual(turn.transcript, "前进两步")
        self.assertEqual(self.device.execution_count, 1)
        self.assertIn("前进 2 个步态循环已完成", tts.texts[0])
        self.assertEqual(sink.played[0], (b"\1\0" * 100, TTS_PCM))

    async def test_streaming_tts_uses_first_chunk_playback_path(self):
        class StreamingSynthesizer:
            pcm_format = TTS_PCM

            async def synthesize(self, text):
                raise AssertionError("full buffering path must not run")

            async def stream(self, text):
                yield b"\x01\x00"
                yield b"\x02\x00"

        class StreamingSink:
            def __init__(self):
                self.chunks = []

            async def play(self, pcm, pcm_format, cancel):
                raise AssertionError("buffered playback path must not run")

            async def play_stream(self, chunks, pcm_format, cancel):
                self.chunks = [chunk async for chunk in chunks]

        sink = StreamingSink()
        session = VoiceSession(TarsBrain(self.client), ScriptedRecognizer("你好"),
                               StreamingSynthesizer(), sink)
        self.assertIsNone(await session._speak("测试"))
        self.assertEqual(sink.chunks, [b"\x01\x00", b"\x02\x00"])

    async def test_barge_in_cancels_current_playback(self):
        sink = SlowSink()
        session = VoiceSession(TarsBrain(self.client), ScriptedRecognizer("你好"),
                               RecordingSynthesizer(), sink)
        task = asyncio.create_task(session.run_turn(self.source()))
        for _ in range(100):
            if session._playback_task:
                break
            await asyncio.sleep(0.001)
        session.barge_in()
        await asyncio.wait_for(task, 1)
        self.assertTrue(sink.cancelled)

    async def test_wake_word_blocks_unaddressed_motion(self):
        tts = RecordingSynthesizer()
        sink = MemoryAudioSink()
        session = VoiceSession(TarsBrain(self.client), ScriptedRecognizer("前进两步"), tts, sink,
                               wake_word="TARS")
        turn = await session.run_turn(self.source())
        self.assertFalse(turn.response.should_speak)
        self.assertEqual(self.device.execution_count, 0)
        self.assertEqual(tts.texts, [])
        self.assertEqual(sink.played, [])

    async def test_wake_word_is_removed_before_motion_parser(self):
        session = VoiceSession(TarsBrain(self.client), ScriptedRecognizer("TARS，前进一步"),
                               RecordingSynthesizer(), MemoryAudioSink(), wake_word="TARS")
        await session.run_turn(self.source())
        self.assertEqual(self.device.execution_count, 1)

    async def test_chinese_wake_word_allows_unspaced_command(self):
        session = VoiceSession(TarsBrain(self.client), ScriptedRecognizer("塔斯前进一步"),
                               RecordingSynthesizer(), MemoryAudioSink(), wake_word="TARS|塔斯")
        await session.run_turn(self.source())
        self.assertEqual(self.device.execution_count, 1)

    async def test_asr_tas_alias_cannot_authorize_motion(self):
        session = VoiceSession(TarsBrain(self.client), ScriptedRecognizer("前进一步，TAS。"),
                               RecordingSynthesizer(), MemoryAudioSink(), wake_word="TARS|TAS|塔斯")
        turn = await session.run_turn(self.source())
        self.assertIn("需要精确说出 TARS", turn.response.text)
        self.assertEqual(self.device.execution_count, 0)

    async def test_common_asr_wake_confusions_are_accepted_for_chat(self):
        provider = ScriptedProvider("冷笑话已装载。")
        for transcript in ("所以，task，给我来个冷笑话", "Pars, tell me a joke",
                           "Pass，讲个笑话", "帕尔斯讲个笑话"):
            with self.subTest(transcript=transcript):
                session = VoiceSession(TarsBrain(self.client, provider=provider),
                                       ScriptedRecognizer(transcript), RecordingSynthesizer(),
                                       MemoryAudioSink(), wake_word="TARS|TAS|塔斯")
                turn = await session.run_turn(self.source())
                self.assertEqual(turn.response.text, "冷笑话已装载。")
        self.assertEqual(self.device.execution_count, 0)

    async def test_common_asr_wake_confusions_cannot_authorize_privileged_intents(self):
        cases = ("task，前进一步", "Pars, move forward", "Pass，急停", "帕尔斯解除急停")
        for transcript in cases:
            with self.subTest(transcript=transcript):
                session = VoiceSession(TarsBrain(self.client), ScriptedRecognizer(transcript),
                                       RecordingSynthesizer(), MemoryAudioSink(),
                                       wake_word="TARS|TAS|塔斯")
                turn = await session.run_turn(self.source())
                self.assertIn("需要精确说出 TARS", turn.response.text)
                self.assertIsNone(turn.response.command_id)
            self.assertEqual(self.device.execution_count, 0)

    async def test_followup_chat_works_inside_conversation_window_without_wake_word(self):
        recognizer = ScriptedRecognizer("TARS，介绍一下你自己")
        session = VoiceSession(TarsBrain(self.client), recognizer,
                               RecordingSynthesizer(), MemoryAudioSink(), wake_word="TARS|塔斯")
        first = await session.run_turn(self.source())
        self.assertTrue(first.response.should_speak)
        recognizer.transcript = "你还在吗，来一个冷笑话"
        second = await session.run_turn(self.source())
        self.assertTrue(second.response.should_speak)

    async def test_accepted_turn_publishes_user_and_tars_dialogue(self):
        publisher = AsyncMock()
        session = VoiceSession(SequenceBrain(BrainResponse("收到。", should_speak=False)),
                               ScriptedRecognizer("TARS, 检查状态"), RecordingSynthesizer(),
                               MemoryAudioSink(), wake_word="TARS|塔斯",
                               dialogue_publisher=publisher)
        await session.run_turn(self.source())
        self.assertEqual(publisher.await_args_list,
                         [call("user", "检查状态"), call("tars", "收到。")])

    async def test_dialogue_display_failure_does_not_break_voice_turn(self):
        publisher = AsyncMock(side_effect=RuntimeError("display offline"))
        session = VoiceSession(SequenceBrain(BrainResponse("继续。", should_speak=False)),
                               ScriptedRecognizer("TARS, 继续"), RecordingSynthesizer(),
                               MemoryAudioSink(), wake_word="TARS|塔斯",
                               dialogue_publisher=publisher)
        turn = await session.run_turn(self.source())
        self.assertEqual(turn.response.text, "继续。")

    async def test_followup_motion_still_requires_precise_wake(self):
        recognizer = ScriptedRecognizer("TARS，介绍一下你自己")
        session = VoiceSession(TarsBrain(self.client), recognizer,
                               RecordingSynthesizer(), MemoryAudioSink(), wake_word="TARS|塔斯")
        await session.run_turn(self.source())
        recognizer.transcript = "前进一步"
        followup = await session.run_turn(self.source())
        self.assertIn("需要精确说出", followup.response.text)
        self.assertEqual(self.device.execution_count, 0)

    async def test_successful_long_playback_restarts_window_from_completion(self):
        clock = FakeClock()
        brain = SequenceBrain(BrainResponse("较长回答。"),
                              BrainResponse("跟进收到。", should_speak=False))
        recognizer = ScriptedRecognizer("TARS，介绍一下你自己")
        session = VoiceSession(brain, recognizer, RecordingSynthesizer(),
                               ClockAdvancingSink(clock, 35), wake_word="TARS|塔斯",
                               conversation_window_s=30, clock=clock)
        first = await session.run_turn(self.source())
        self.assertIsNone(first.speech_error)
        self.assertEqual(clock.value, 35)
        self.assertEqual(session._conversation_until, 65)
        clock.advance(1)
        recognizer.transcript = "再讲一个"
        followup = await session.run_turn(self.source())
        self.assertEqual(followup.response.text, "跟进收到。")
        self.assertEqual(brain.calls[-1], ("再讲一个", False))

    async def test_each_valid_silent_followup_slides_window_forward(self):
        clock = FakeClock()
        silent = BrainResponse("", should_speak=False)
        brain = SequenceBrain(silent, silent, silent)
        recognizer = ScriptedRecognizer("TARS，开始聊天")
        session = VoiceSession(brain, recognizer, RecordingSynthesizer(), MemoryAudioSink(),
                               wake_word="TARS|塔斯", conversation_window_s=30, clock=clock)
        await session.run_turn(self.source())
        self.assertEqual(session._conversation_until, 30)
        clock.advance(20)
        recognizer.transcript = "继续"
        await session.run_turn(self.source())
        self.assertEqual(session._conversation_until, 50)
        clock.advance(20)
        recognizer.transcript = "还要继续"
        await session.run_turn(self.source())
        self.assertEqual(session._conversation_until, 70)
        self.assertEqual([precise for _, precise in brain.calls], [True, False, False])

    async def test_tts_error_keeps_input_triggered_window_extension(self):
        clock = FakeClock()
        brain = SequenceBrain(BrainResponse("第一次。"),
                              BrainResponse("第二次。", should_speak=False))
        recognizer = ScriptedRecognizer("TARS，开始聊天")
        session = VoiceSession(brain, recognizer, FailOnceSynthesizer(), MemoryAudioSink(),
                               wake_word="TARS|塔斯", conversation_window_s=30, clock=clock)
        first = await session.run_turn(self.source())
        self.assertEqual(first.speech_error, "tts_failed")
        self.assertEqual(session._conversation_until, 30)
        clock.advance(20)
        recognizer.transcript = "错误后继续"
        second = await session.run_turn(self.source())
        self.assertEqual(second.response.text, "第二次。")
        self.assertEqual(session._conversation_until, 50)

    async def test_expired_window_requires_wake_word_again(self):
        clock = FakeClock()
        brain = SequenceBrain(BrainResponse("", should_speak=False))
        recognizer = ScriptedRecognizer("TARS，开始聊天")
        session = VoiceSession(brain, recognizer, RecordingSynthesizer(), MemoryAudioSink(),
                               wake_word="TARS|塔斯", conversation_window_s=30, clock=clock)
        await session.run_turn(self.source())
        clock.advance(30)
        recognizer.transcript = "继续聊天"
        expired = await session.run_turn(self.source())
        self.assertFalse(expired.response.should_speak)
        self.assertEqual(expired.response.text, "未检测到唤醒词。")
        self.assertEqual(len(brain.calls), 1)

    async def test_fuzzy_wake_window_never_authorizes_followup_motion(self):
        clock = FakeClock()
        recognizer = ScriptedRecognizer("task，聊聊天")
        brain = TarsBrain(self.client, provider=ScriptedProvider("可以。"))
        session = VoiceSession(brain, recognizer, RecordingSynthesizer(), MemoryAudioSink(),
                               wake_word="TARS|塔斯", conversation_window_s=30, clock=clock)
        await session.run_turn(self.source())
        clock.advance(5)
        recognizer.transcript = "前进一步"
        followup = await session.run_turn(self.source())
        self.assertIn("需要精确说出", followup.response.text)
        self.assertIsNone(followup.response.command_id)
        self.assertEqual(self.device.execution_count, 0)

    async def test_case_insensitive_wake_alias_after_safe_greeting(self):
        session = VoiceSession(TarsBrain(self.client), ScriptedRecognizer("你好， Tars， 前进一步。"),
                               RecordingSynthesizer(), MemoryAudioSink(), wake_word="TARS|TAS|塔斯")
        await session.run_turn(self.source())
        self.assertEqual(self.device.execution_count, 1)

    async def test_wake_alias_in_arbitrary_middle_text_is_not_addressed(self):
        session = VoiceSession(TarsBrain(self.client), ScriptedRecognizer("我觉得 Tars 应该前进一步。"),
                               RecordingSynthesizer(), MemoryAudioSink(), wake_word="TARS|TAS|塔斯")
        turn = await session.run_turn(self.source())
        self.assertFalse(turn.response.should_speak)
        self.assertEqual(self.device.execution_count, 0)

    async def test_tas_alias_does_not_match_longer_word(self):
        session = VoiceSession(TarsBrain(self.client), ScriptedRecognizer("TASMAN前进一步"),
                               RecordingSynthesizer(), MemoryAudioSink(), wake_word="TARS|TAS|塔斯")
        await session.run_turn(self.source())
        self.assertEqual(self.device.execution_count, 0)

    async def test_wake_word_does_not_match_longer_word(self):
        session = VoiceSession(TarsBrain(self.client), ScriptedRecognizer("TARSENAL前进一步"),
                               RecordingSynthesizer(), MemoryAudioSink(), wake_word="TARS|塔斯")
        await session.run_turn(self.source())
        self.assertEqual(self.device.execution_count, 0)

    async def test_tts_retry_never_replays_motion(self):
        tts = FailOnceSynthesizer()
        sink = MemoryAudioSink()
        session = VoiceSession(TarsBrain(self.client), ScriptedRecognizer("前进一步"), tts, sink)
        turn = await session.run_turn(self.source())
        self.assertEqual(turn.speech_error, "tts_failed")
        self.assertEqual(self.device.execution_count, 1)
        retried = await session.retry_speech(turn)
        self.assertIsNone(retried.speech_error)
        self.assertEqual(self.device.execution_count, 1)
        self.assertEqual(tts.calls, 2)
        self.assertEqual(len(sink.played), 1)

    async def test_asr_error_is_sanitized_and_sends_no_command(self):
        session = VoiceSession(TarsBrain(self.client), RaisingRecognizer(),
                               RecordingSynthesizer(), MemoryAudioSink())
        with self.assertRaises(VoiceStageError) as raised:
            await session.run_turn(self.source())
        self.assertEqual(str(raised.exception), "asr:provider_error")
        self.assertNotIn("secret", str(raised.exception))
        self.assertEqual(self.device.execution_count, 0)

    async def test_concurrent_turns_are_serialized(self):
        recognizer = GateRecognizer()
        session = VoiceSession(TarsBrain(self.client), recognizer,
                               RecordingSynthesizer(), MemoryAudioSink())
        first = asyncio.create_task(session.run_turn(self.source()))
        await recognizer.entered.wait()
        second = asyncio.create_task(session.run_turn(self.source()))
        await asyncio.sleep(0.01)
        self.assertEqual(recognizer.calls, 1)
        recognizer.release.set()
        await asyncio.gather(first, second)
        self.assertEqual(recognizer.calls, 2)
        self.assertEqual(recognizer.max_active, 1)

    async def test_asr_circuit_opens_without_calling_brain(self):
        recognizer = RaisingRecognizer()
        session = VoiceSession(TarsBrain(self.client), recognizer, RecordingSynthesizer(),
                               MemoryAudioSink(), asr_breaker=CircuitBreaker(2, 60))
        codes = []
        for _ in range(3):
            with self.assertRaises(VoiceStageError) as raised:
                await session.run_turn(self.source())
            codes.append(raised.exception.code)
        self.assertEqual(codes, ["provider_error", "provider_error", "circuit_open"])
        self.assertEqual(recognizer.calls, 2)
        self.assertEqual(self.device.execution_count, 0)

    async def test_tts_circuit_never_replays_motion(self):
        tts = AlwaysFailSynthesizer()
        session = VoiceSession(TarsBrain(self.client), ScriptedRecognizer("前进一步"), tts,
                               MemoryAudioSink(), tts_breaker=CircuitBreaker(2, 60))
        turn = await session.run_turn(self.source())
        second = await session.retry_speech(turn)
        third = await session.retry_speech(second)
        self.assertEqual([turn.speech_error, second.speech_error, third.speech_error],
                         ["tts_failed", "tts_failed", "tts_circuit_open"])
        self.assertEqual(tts.calls, 2)
        self.assertEqual(self.device.execution_count, 1)
        self.assertEqual(turn.response.command_id, third.response.command_id)

    async def test_success_metrics_are_low_cardinality(self):
        metrics = MetricsObserver()
        session = VoiceSession(TarsBrain(self.client), ScriptedRecognizer("你好"),
                               RecordingSynthesizer(), MemoryAudioSink(), observer=metrics)
        await session.run_turn(self.source())
        summary = metrics.summary()
        for key in ("capture.ok.none", "asr.ok.none", "brain.ok.none", "tts.ok.none",
                    "playback.ok.none", "turn.ok.none"):
            self.assertEqual(summary["counts"][key], 1)

    async def test_safe_jsonl_never_records_content_or_provider_error(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "voice.jsonl"
            session = VoiceSession(TarsBrain(self.client),
                                   ScriptedRecognizer("TARS，聊聊 TRANSCRIPT_SENTINEL sk-abcdefghijklmnop"),
                                   AlwaysFailSynthesizer(), MemoryAudioSink(), wake_word="TARS",
                                   observer=SafeJsonlObserver(path))
            turn = await session.run_turn(self.source())
            self.assertEqual(turn.speech_error, "tts_failed")
            raw = path.read_text(encoding="utf-8")
            for forbidden in ("TRANSCRIPT_SENTINEL", "SECRET_EXCEPTION_SENTINEL", "sk-", "Bearer"):
                self.assertNotIn(forbidden, raw)
            allowed = {"stage", "outcome", "code", "duration_ms", "timestamp_ms"}
            for line in raw.splitlines():
                self.assertEqual(set(json.loads(line)), allowed)

    def test_event_rejects_arbitrary_secret_code(self):
        with self.assertRaises(ValueError):
            VoiceEvent.create("asr", "error", "sk-secret")


class VoiceDoctorTests(unittest.TestCase):
    def test_workspace_id_requires_api_host_prefix(self):
        self.assertFalse(workspace_id_is_valid("6155573"))
        self.assertTrue(workspace_id_is_valid("ws-3gioena9myvhcroo"))

    def test_doctor_reports_presence_only(self):
        from unittest.mock import patch
        with patch.dict("os.environ", {"DASHSCOPE_API_KEY": "sk-do-not-print",
                                       "DASHSCOPE_VOICE_ID": "voice-secret"}, clear=True):
            rendered = repr(run_checks())
        self.assertNotIn("sk-do-not-print", rendered)
        self.assertNotIn("voice-secret", rendered)

    def test_doctor_infers_supported_qwen_profile_without_printing_voice(self):
        from unittest.mock import patch
        voice = "qwen-tts-vc-bailian-voice-private"
        with patch.dict("os.environ", {
            "DASHSCOPE_API_KEY": "sk-secret",
            "DASHSCOPE_VOICE_ID": voice,
            "DASHSCOPE_WORKSPACE_ID": "ws-testworkspace",
        }, clear=True):
            checks = run_checks()
        profile = next(check for check in checks if check.name == "TTS_PROFILE")
        self.assertTrue(profile.ok)
        self.assertIn("qwen3-tts-vc-2026-01-22", profile.message)
        self.assertNotIn(voice, repr(checks))

    def test_doctor_rejects_mismatched_qwen_model(self):
        from unittest.mock import patch
        with patch.dict("os.environ", {
            "DASHSCOPE_API_KEY": "sk-secret",
            "DASHSCOPE_VOICE_ID": "qwen-tts-vc-private",
            "DASHSCOPE_WORKSPACE_ID": "ws-testworkspace",
            "DASHSCOPE_TTS_MODEL": "cosyvoice-v3.5-flash",
        }, clear=True):
            checks = run_checks()
        profile = next(check for check in checks if check.name == "TTS_PROFILE")
        self.assertFalse(profile.ok)

    def test_deepseek_live_probe_reports_success_without_content(self):
        with patch.dict("os.environ", {"DEEPSEEK_API_KEY": "sk-secret"}, clear=True), \
                patch("tars_companion.voice_doctor.DeepSeekProvider.complete",
                      new=AsyncMock(return_value="PRIVATE_RESPONSE")):
            checks = run_checks(deepseek=True, probe_deepseek=True)
        live = next(check for check in checks if check.name == "deepseek_live")
        self.assertTrue(live.ok)
        self.assertNotIn("PRIVATE_RESPONSE", repr(checks))

    def test_deepseek_live_probe_reports_only_http_status(self):
        error = urllib.error.HTTPError("https://api.deepseek.com", 401, "PRIVATE", None, None)
        with patch.dict("os.environ", {"DEEPSEEK_API_KEY": "sk-secret"}, clear=True), \
                patch("tars_companion.voice_doctor.DeepSeekProvider.complete",
                      new=AsyncMock(side_effect=error)):
            checks = run_checks(deepseek=True, probe_deepseek=True)
        live = next(check for check in checks if check.name == "deepseek_live")
        self.assertFalse(live.ok)
        self.assertEqual(live.message, "http_401")
        self.assertNotIn("PRIVATE", repr(checks))


if __name__ == "__main__":
    unittest.main()
