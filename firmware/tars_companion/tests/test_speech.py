import base64
import asyncio
import os
import sys
import types
import unittest
from unittest.mock import patch

from tars_companion.audio import TTS_PCM
from tars_companion.speech import (
    COSYVOICE_MODEL,
    QWEN_VC_MODEL,
    DashScopeCosyVoice,
    DashScopeFunAsr,
    DashScopeQwenVoice,
    create_dashscope_synthesizer,
)


def chunk(data=None, *, status_code=200):
    audio = types.SimpleNamespace(data=data, url=None)
    output = types.SimpleNamespace(audio=audio, finish_reason=None)
    return types.SimpleNamespace(status_code=status_code, output=output)


class QwenVoiceTests(unittest.TestCase):
    def test_buffered_asr_does_not_replay_audio_in_real_time(self):
        self.assertFalse(DashScopeFunAsr("sk-secret").pace_frames)

    def fake_sdk(self, returned, calls):
        class MultiModalConversation:
            @staticmethod
            def call(**kwargs):
                calls.append(kwargs)
                return returned

        return types.SimpleNamespace(api_key=None, base_http_api_url=None,
                                     MultiModalConversation=MultiModalConversation)

    def test_qwen_stream_decodes_pcm_chunks_in_order(self):
        calls = []
        first = b"\x01\x00\x02\x00"
        second = b"\x03\x00"
        sdk = self.fake_sdk([
            chunk(base64.b64encode(first).decode()),
            chunk(base64.b64encode(second).decode()),
            chunk(),
        ], calls)
        adapter = DashScopeQwenVoice("qwen-tts-vc-test", "sk-secret",
                                     workspace_id="ws-testworkspace")
        with patch.dict(sys.modules, {"dashscope": sdk}):
            pcm, pcm_format = adapter._synthesize_sync("你好")
        self.assertEqual(pcm, first + second)
        self.assertEqual(pcm_format, TTS_PCM)
        self.assertEqual(calls, [{
            "model": QWEN_VC_MODEL,
            "api_key": "sk-secret",
            "text": "你好",
            "voice": "qwen-tts-vc-test",
            "language_type": "Chinese",
            "stream": True,
        }])
        self.assertEqual(
            sdk.base_http_api_url,
            "https://ws-testworkspace.cn-beijing.maas.aliyuncs.com/api/v1")

    def test_async_qwen_stream_forwards_provider_chunks(self):
        calls = []
        first = b"\x01\x00\x02\x00"
        second = b"\x03\x00"
        sdk = self.fake_sdk([
            chunk(base64.b64encode(first).decode()),
            chunk(base64.b64encode(second).decode()),
            chunk(),
        ], calls)
        adapter = DashScopeQwenVoice("qwen-tts-vc-test", "sk-secret")

        async def collect():
            with patch.dict(sys.modules, {"dashscope": sdk}):
                return [data async for data in adapter.stream("你好")]

        self.assertEqual(asyncio.run(collect()), [first, second])

    def test_qwen_empty_audio_has_fixed_safe_error(self):
        sdk = self.fake_sdk([chunk()], [])
        adapter = DashScopeQwenVoice("qwen-tts-vc-test", "sk-do-not-print")
        with patch.dict(sys.modules, {"dashscope": sdk}):
            with self.assertRaisesRegex(RuntimeError, "returned no audio") as raised:
                adapter._synthesize_sync("test")
        self.assertNotIn("sk-do-not-print", str(raised.exception))

    def test_qwen_provider_and_base64_errors_are_sanitized(self):
        for returned in ([chunk(status_code=401)], [chunk("not-base64")]):
            sdk = self.fake_sdk(returned, [])
            adapter = DashScopeQwenVoice("qwen-tts-vc-test", "sk-do-not-print")
            with patch.dict(sys.modules, {"dashscope": sdk}):
                with self.assertRaisesRegex(RuntimeError, "Qwen TTS request failed") as raised:
                    adapter._synthesize_sync("test")
            self.assertNotIn("sk-do-not-print", str(raised.exception))

    def test_qwen_voice_prefix_routes_to_exact_snapshot(self):
        with patch.dict(os.environ, {
            "DASHSCOPE_API_KEY": "sk-secret",
            "DASHSCOPE_VOICE_ID": "qwen-tts-vc-bailian-voice-test",
        }, clear=True):
            adapter = create_dashscope_synthesizer()
        self.assertIsInstance(adapter, DashScopeQwenVoice)
        self.assertEqual(adapter.model, QWEN_VC_MODEL)

    def test_cosyvoice_remains_compatible(self):
        adapter = create_dashscope_synthesizer(
            "cosy-clone", "sk-secret", model=COSYVOICE_MODEL)
        self.assertIsInstance(adapter, DashScopeCosyVoice)

    def test_mismatched_qwen_voice_model_is_rejected_before_network(self):
        with self.assertRaisesRegex(ValueError, "Qwen voice requires"):
            create_dashscope_synthesizer(
                "qwen-tts-vc-test", "sk-secret", model=COSYVOICE_MODEL)


if __name__ == "__main__":
    unittest.main()
