import asyncio
import unittest
from unittest.mock import AsyncMock, patch
from tars_companion.audio import ASR_PCM, make_test_tone, pcm16_rms, UtteranceCollector
from tars_companion.body_audio import MockBodyAudioChannel, BodyAudioSource, BodyAudioSink
from tars_companion.speech import (create_offline_speech, MockASR, MockTTS, SpeechOptions,
                                   UnconfiguredASR, UnconfiguredTTS)
from tars_companion.voice import VoiceSession, VoiceStageError
from tars_companion.brain import TarsBrain
from tars_companion.client import TarsClient
from tars_companion.llm import ScriptedProvider
from tars_companion.simulator import SimulatedTarsDevice
from tars_companion.transport import memory_transport_pair


class ProviderTests(unittest.IsolatedAsyncioTestCase):
    async def test_mock_tts_is_real_pcm_tone_not_fake_speech(self):
        result = await MockTTS().render("hello", SpeechOptions("en", "test"))
        self.assertEqual(result.status, "ok")
        self.assertEqual(len(result.pcm), 9600)
        self.assertGreater(pcm16_rms(result.pcm), 0)
        self.assertEqual(result.pcm_format.sample_rate, 16000)
        text = await MockASR("fixed transcript").recognize(result.pcm, ASR_PCM)
        self.assertEqual(text.text, "fixed transcript")

    async def test_unconfigured_is_explicit_no_cloud_fallback(self):
        self.assertEqual((await UnconfiguredASR().recognize(b"", ASR_PCM)).status, "unconfigured")
        self.assertEqual((await UnconfiguredTTS().render("x", SpeechOptions())).status, "unconfigured")
        with patch.dict("os.environ", {}, clear=True):
            asr, tts = create_offline_speech()
            with self.assertRaisesRegex(RuntimeError, "asr_unavailable"):
                await asr.transcribe(b"\0\0")
            with self.assertRaisesRegex(RuntimeError, "tts_unavailable"):
                await tts.synthesize("hello")
        with patch.dict("os.environ", {"TARS_ASR_BACKEND": "cloud-not-selected"}):
            with self.assertRaises(ValueError):
                create_offline_speech()

    async def test_overflow_and_half_duplex_are_bounded(self):
        channel = MockBodyAudioChannel(capacity_frames=1)
        channel.feed_capture(bytes(ASR_PCM.frame_bytes))
        with self.assertRaises(BufferError):
            channel.feed_capture(bytes(ASR_PCM.frame_bytes))
        epoch = channel.begin("playback")
        with self.assertRaises(RuntimeError):
            channel.feed_capture(bytes(ASR_PCM.frame_bytes))
        channel.end(epoch)


class StateTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        pc, robot = memory_transport_pair()
        self.device = SimulatedTarsDevice(robot)
        await self.device.start()
        self.client = TarsClient(pc)
        await self.client.start()
        self.channel = MockBodyAudioChannel()
        self.asr, self.tts = create_offline_speech(mock=True)
        self.session = VoiceSession(TarsBrain(self.client, provider=ScriptedProvider("Mock reply.")),
            self.asr, self.tts, BodyAudioSink(self.channel), UtteranceCollector(silence_ms=200),
            interaction_publisher=self.client.interaction, dialogue_publisher=self.client.display_dialogue,
            capture_timeout=.2)

    async def asyncTearDown(self):
        await self.client.close(); await self.device.close()

    def feed(self):
        voiced = make_test_tone(100, volume_pct=10)
        for frame in (voiced, voiced, bytes(ASR_PCM.frame_bytes), bytes(ASR_PCM.frame_bytes)):
            self.channel.feed_capture(frame)

    async def test_full_mock_states_dialogue_no_arm(self):
        self.feed()
        result = await self.session.run_turn(BodyAudioSource(self.channel))
        self.assertIsNone(result.speech_error)
        self.assertEqual(self.device.interaction_history, ["LISTENING", "THINKING", "SPEAKING", "IDLE"])
        self.assertEqual(self.session.interaction_state, "IDLE")
        self.assertEqual(self.channel.mode, "idle")
        self.assertEqual(len(self.device.dialogue), 2)
        self.assertFalse(self.device.armed)

    async def test_capture_disconnect_then_explicit_reconnect_recovers(self):
        task = asyncio.create_task(self.session.run_turn(BodyAudioSource(self.channel)))
        await asyncio.sleep(.02)
        self.channel.disconnect()
        with self.assertRaises(VoiceStageError):
            await task
        self.assertEqual(self.session.interaction_state, "IDLE")
        self.channel.reconnect(); self.feed()
        self.assertIsNone((await self.session.run_turn(BodyAudioSource(self.channel))).speech_error)

    async def test_capture_timeout_and_task_cancellation_reset_idle(self):
        with self.assertRaisesRegex(VoiceStageError, "capture:input_failed"):
            await self.session.run_turn(BodyAudioSource(self.channel))
        self.assertEqual(self.channel.mode, "idle")
        task = asyncio.create_task(self.session.run_turn(BodyAudioSource(self.channel)))
        await asyncio.sleep(.02)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(self.session.interaction_state, "IDLE")
        self.assertEqual(self.channel.mode, "idle")

    async def test_tts_failure_restores_idle_and_does_not_replay_brain(self):
        self.feed()
        with patch.object(self.tts, "synthesize", side_effect=RuntimeError("SECRET_PROVIDER_BODY")):
            turn = await self.session.run_turn(BodyAudioSource(self.channel))
        self.assertEqual(turn.speech_error, "tts_failed")
        self.assertEqual(self.session.interaction_state, "IDLE")
        self.assertEqual(self.device.execution_count, 0)
        self.assertNotIn("SECRET", turn.speech_error)

    async def test_brain_timeout_is_bounded_and_resets_idle(self):
        self.feed()
        self.session.brain_timeout = .01
        async def stuck(*args, **kwargs):
            await asyncio.sleep(1)
        with patch.object(self.session.brain, "handle_text", side_effect=stuck):
            with self.assertRaisesRegex(VoiceStageError, "brain:processing_error"):
                await self.session.run_turn(BodyAudioSource(self.channel))
        self.assertEqual(self.session.interaction_state, "IDLE")

    async def test_invalid_interaction_is_rejected_locally(self):
        with self.assertRaises(ValueError):
            await self.client.interaction("MOVING")


if __name__ == "__main__":
    unittest.main()
