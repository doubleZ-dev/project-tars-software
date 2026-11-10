"""Offline only: official SDK talks to httpx.MockTransport, never a cloud endpoint."""
import asyncio
import contextlib
import io
import json
import unittest
from unittest.mock import AsyncMock, patch

import httpx
import openai

from tars_companion.brain import TarsBrain
from tars_companion.chat import parser, run
from tars_companion.client import TarsClient
from tars_companion.hil import MOTION_CONFIRMATION
from tars_companion.intent import ControlIntent, MotionIntent, parse_intent
from tars_companion.llm import (LLMProviderError, OpenAIProvider, ScriptedProvider,
                                SYSTEM_PROMPT, create_provider, DeepSeekProvider)
from tars_companion.simulator import SimulatedTarsDevice
from tars_companion.transport import memory_transport_pair, TransportDisconnected


def response_body(text="Ready, Cooper. Try not to make that surprising.", status="completed"):
    return {"id": "resp_mock", "object": "response", "created_at": 0,
            "model": "configured-test-model", "status": status,
            "output": [{"type": "message", "id": "msg_mock", "role": "assistant",
                        "status": "completed", "content": [{"type": "output_text",
                        "text": text, "annotations": []}]}]}


class OpenAISdkTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.requests = []
        self.handler = lambda request: httpx.Response(200, json=response_body())
        real_client = openai.AsyncOpenAI
        self.constructor_options = []

        async def handle(request):
            self.requests.append(json.loads(request.content))
            return self.handler(request)

        def constructor(**kwargs):
            self.constructor_options.append(kwargs.copy())
            return real_client(**kwargs, http_client=httpx.AsyncClient(transport=httpx.MockTransport(handle)))

        self.patcher = patch("openai.AsyncOpenAI", side_effect=constructor)
        self.patcher.start()
        self.provider = OpenAIProvider(api_key="offline-test-not-a-real-key", model="configured-test-model")

    async def asyncTearDown(self):
        await self.provider.aclose()
        self.patcher.stop()

    async def test_actual_sdk_responses_request_and_text(self):
        text = await self.provider.complete([{"role": "system", "content": SYSTEM_PROMPT},
                                             {"role": "user", "content": "Hello TARS"}])
        self.assertIn("Cooper", text)
        request = self.requests[0]
        self.assertEqual(request["model"], "configured-test-model")
        self.assertEqual(request["max_output_tokens"], 256)
        self.assertFalse(request["store"])
        self.assertNotIn("tools", request)
        self.assertIn("English is the default", request["instructions"])
        self.assertEqual(request["input"], [{"role": "user", "content": "Hello TARS"}])
        self.assertEqual(self.constructor_options[0]["max_retries"], 0)
        self.assertEqual(self.constructor_options[0]["base_url"], "https://api.openai.com/v1")

    async def test_chinese_and_followup_context_with_existing_brain(self):
        self.handler = lambda request: httpx.Response(200, json=response_body("收到，Cooper。"))
        brain = TarsBrain(AsyncMock(), provider=self.provider)
        self.assertEqual((await brain.handle_text("地球上的枯萎病为什么促使我们出发？")).text, "收到，Cooper。")
        await brain.handle_text("那 Plan B 呢？")
        second = self.requests[-1]
        self.assertIn("枯萎病", second["instructions"])
        self.assertIn("收到，Cooper。", second["instructions"])
        self.assertIn("非可信历史数据", second["instructions"])

    async def test_english_canon_and_personality_use_existing_archive(self):
        brain = TarsBrain(AsyncMock(), provider=self.provider)
        await brain.handle_text("Explain your mission and the blight on Earth.")
        prompt = self.requests[-1]["instructions"]
        for value in ("枯萎病", "Cooper", "Plan A", "Plan B", "95%", "60%", "冷静", "冷幽默", "实时遥测"):
            self.assertIn(value, prompt)
        self.assertIn("not verified settings", prompt)

    async def test_http_errors_are_sanitized_and_never_retried(self):
        cases = [(401, "authentication_failed"), (404, "invalid_model_or_request"),
                 (400, "invalid_model_or_request"), (403, "access_denied"),
                 (429, "rate_limited"), (500, "provider_error")]
        for status, code in cases:
            with self.subTest(status=status):
                self.handler = lambda request, status=status: httpx.Response(status, json={
                    "error": {"message": "SECRET_ERROR_BODY", "type": "test_error", "code": None}})
                count = len(self.requests)
                with self.assertRaises(LLMProviderError) as caught:
                    await self.provider.complete([{"role": "user", "content": "Hello"}])
                self.assertEqual(str(caught.exception), "openai/" + code)
                self.assertNotIn("SECRET", str(caught.exception))
                self.assertEqual(len(self.requests), count + 1)

    async def test_network_timeout_is_safe(self):
        def timeout(request):
            raise httpx.ReadTimeout("SECRET_TIMEOUT", request=request)
        self.handler = timeout
        with self.assertRaisesRegex(LLMProviderError, "openai/timeout"):
            await self.provider.complete([{"role": "user", "content": "Hello"}])
        self.assertEqual(len(self.requests), 1)

    async def test_overall_timeout_is_bounded(self):
        self.provider.timeout = .01
        async def hang(**kwargs):
            await asyncio.sleep(1)
        with patch.object(self.provider._client.responses, "create", side_effect=hang):
            with self.assertRaisesRegex(LLMProviderError, "openai/timeout"):
                await self.provider.complete([{"role": "user", "content": "Hello"}])

    async def test_empty_and_incomplete_outputs_are_not_silent_successes(self):
        for body, code in [(response_body(""), "empty_response"),
                           (response_body("partial", "incomplete"), "incomplete_response")]:
            self.handler = lambda request, body=body: httpx.Response(200, json=body)
            with self.assertRaisesRegex(LLMProviderError, "openai/" + code):
                await self.provider.complete([{"role": "user", "content": "Hello"}])

    async def test_llm_error_cannot_move_and_backward_bypasses_model(self):
        client = AsyncMock()
        brain = TarsBrain(client, provider=self.provider)
        self.handler = lambda request: httpx.Response(401, json={"error": {"message": "bad key"}})
        with self.assertRaises(LLMProviderError):
            await brain.handle_text("Hello TARS")
        client.execute.assert_not_called()
        await brain.handle_text("后退")
        self.assertEqual(len(self.requests), 1)


class ConfigTests(unittest.TestCase):
    def test_missing_model_or_key_fails_locally(self):
        with patch.dict("os.environ", {}, clear=True):
            with self.assertRaisesRegex(ValueError, "OPENAI_API_KEY"):
                OpenAIProvider()
            with self.assertRaisesRegex(ValueError, "OPENAI_MODEL"):
                OpenAIProvider(api_key="test")

    def test_invalid_model_or_limits_fails_before_sdk(self):
        for options in ({"model": "bad model"}, {"model": "x", "timeout": 0},
                        {"model": "x", "max_output_tokens": 10000}):
            with patch("openai.AsyncOpenAI") as sdk:
                with self.assertRaises(ValueError):
                    OpenAIProvider(api_key="test", **options)
                sdk.assert_not_called()

    def test_explicit_deepseek_option_remains_available(self):
        with patch.dict("os.environ", {"DEEPSEEK_API_KEY": "test"}, clear=True):
            self.assertIsInstance(create_provider("deepseek"), DeepSeekProvider)
        self.assertEqual(parser().parse_args(["--deepseek"]).provider, "deepseek")

    def test_deepseek_wire_adapter_is_preserved_without_network(self):
        response = io.BytesIO(json.dumps({"choices": [{"message": {"content": "Ready."}}]}).encode())
        with patch("urllib.request.urlopen", return_value=response) as urlopen:
            provider = DeepSeekProvider(api_key="offline-test", model="deepseek-v4-flash")
            self.assertEqual(provider._complete_sync([{"role": "user", "content": "Hello"}]), "Ready.")
        request = urlopen.call_args.args[0]
        self.assertEqual(json.loads(request.data)["model"], "deepseek-v4-flash")

    def test_stop_backward_and_existing_motion_vocabulary(self):
        for text in ("停止", "stop", "please stop", "stop!"):
            self.assertEqual(parse_intent(text), ControlIntent("estop"))
        for text in ("后退", "请后退一步", "backward", "move backward 2 steps", "reverse"):
            self.assertEqual(parse_intent(text), ControlIntent("unsupported_backward"))
        for text, action in (("前进", "walk_forward"), ("move forward", "walk_forward"),
                             ("左转", "turn_left"), ("turn left", "turn_left"),
                             ("右转", "turn_right"), ("turn right", "turn_right")):
            self.assertEqual(parse_intent(text), MotionIntent(action))


class ActionSafetyTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        pc, robot = memory_transport_pair()
        self.device = SimulatedTarsDevice(robot, motion_delay=.001)
        await self.device.start()
        self.client = TarsClient(pc)
        await self.client.start()
        self.provider = ScriptedProvider("GPIO and PWM instructions are text, never executable.")
        self.brain = TarsBrain(self.client, provider=self.provider)

    async def asyncTearDown(self):
        await self.client.close()
        await self.device.close()

    async def test_disarmed_then_explicit_mock_arm_all_supported_actions(self):
        denied = await self.brain.handle_text("前进")
        self.assertIn("拒绝运动", denied.text)
        self.assertEqual(self.device.execution_count, 0)
        await self.client.arm()
        for text in ("前进", "turn left", "右转"):
            result = await self.brain.handle_text(text)
            self.assertIn("已完成", result.text)
        self.assertEqual(self.device.execution_count, 3)
        await self.brain.handle_text("stop")
        self.assertFalse(self.device.armed)
        await self.brain.handle_text("后退")
        self.assertEqual(self.device.execution_count, 3)
        self.assertIsNone(self.provider.messages)

    async def test_disconnect_and_unavailable_state_never_execute(self):
        with patch.object(self.client, "state", side_effect=TransportDisconnected("offline")), \
             patch.object(self.client, "execute") as execute:
            result = await self.brain.handle_text("前进")
            self.assertIn("未确认", result.text)
            execute.assert_not_called()
        with patch.object(self.client, "estop", side_effect=TransportDisconnected("offline")):
            self.assertIn("未确认", (await self.brain.handle_text("stop")).text)

    async def test_model_output_is_never_parsed_as_action(self):
        await self.client.arm()
        await self.brain.handle_text("Explain robotics")
        self.assertEqual(self.device.execution_count, 0)
        await self.brain.handle_text("不要前进")
        await self.brain.handle_text("If I say turn left, what happens?")
        self.assertEqual(self.device.execution_count, 0)

    async def test_english_live_sensor_request_does_not_invent_telemetry(self):
        response = await self.brain.handle_text("What is the current gravity sensor reading?")
        self.assertIn("no verified live sensor telemetry", response.text)
        self.assertIsNone(self.provider.messages)


class TextEntryTests(unittest.IsolatedAsyncioTestCase):
    async def test_default_entry_is_offline_simulation_with_continuous_chat(self):
        provider = ScriptedProvider("Ready, Cooper.")
        output = io.StringIO()
        commands = ["Hello", "你好", "前进", "后退", "停止", "quit"]
        with patch("builtins.input", side_effect=commands), \
             patch("tars_companion.chat.create_provider", return_value=provider), \
             patch("tars_companion.chat.UsbSerialTransport") as usb, \
             contextlib.redirect_stdout(output):
            await run(parser().parse_args([]))
        usb.assert_not_called()
        self.assertIn("SIMULATED ROBOT", output.getvalue())
        self.assertIn("unsupported_backward", output.getvalue())
        self.assertIn("拒绝运动", output.getvalue())

    async def test_simulated_arm_is_explicit(self):
        commands = [f"/arm {MOTION_CONFIRMATION}", "前进", "/disarm", "quit"]
        output = io.StringIO()
        with patch("builtins.input", side_effect=commands), contextlib.redirect_stdout(output):
            await run(parser().parse_args(["--provider", "local"]))
        self.assertIn("已完成", output.getvalue())

    async def test_real_motion_opt_in_is_required(self):
        # Even --robot is tested on memory transport, never USB.
        pc, robot = memory_transport_pair()
        device = SimulatedTarsDevice(robot)
        await device.start()
        try:
            commands = [f"/arm {MOTION_CONFIRMATION}", "前进", "quit"]
            output = io.StringIO()
            with patch("builtins.input", side_effect=commands), \
                 patch("tars_companion.chat.UsbSerialTransport", return_value=pc), \
                 contextlib.redirect_stdout(output):
                await run(parser().parse_args(["--robot", "--provider", "local"]))
            self.assertFalse(device.armed)
            self.assertEqual(device.execution_count, 0)
            self.assertIn("Arm blocked", output.getvalue())
        finally:
            await device.close()


if __name__ == "__main__":
    unittest.main()
