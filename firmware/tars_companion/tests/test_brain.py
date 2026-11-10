import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

from tars_companion.brain import TarsBrain
from tars_companion.client import TarsClient
from tars_companion.intent import (ChatIntent, ControlIntent, MotionIntent, parse_intent,
                                   requires_precise_wake, validate_motion)
from tars_companion.llm import ScriptedProvider
from tars_companion.llm import DeepSeekProvider
from tars_companion.memory import MemoryStore
from tars_companion.simulator import SimulatedTarsDevice
from tars_companion.transport import memory_transport_pair


class IntentTests(unittest.TestCase):
    def test_chinese_motion(self):
        self.assertEqual(parse_intent("向前走三步"), MotionIntent("walk_forward", 3, 30))

    def test_raw_servo_is_forbidden(self):
        self.assertEqual(parse_intent("把2号舵机角度设成45"), ControlIntent("forbidden_raw_servo"))

    def test_out_of_range_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "steps_out_of_range"):
            validate_motion(MotionIntent("walk_forward", 4))

    def test_general_text_stays_chat(self):
        self.assertIsInstance(parse_intent("你怎么看今天的天气"), ChatIntent)

    def test_negated_or_quoted_motion_does_not_execute(self):
        self.assertIsInstance(parse_intent("不要前进"), ChatIntent)
        self.assertIsInstance(parse_intent("如果我说前进会怎样"), ChatIntent)

    def test_only_motion_and_safety_changes_require_precise_wake(self):
        self.assertTrue(requires_precise_wake(MotionIntent("walk_forward")))
        self.assertTrue(requires_precise_wake(ControlIntent("estop")))
        self.assertTrue(requires_precise_wake(ControlIntent("clear_estop")))
        self.assertFalse(requires_precise_wake(ControlIntent("state")))
        self.assertFalse(requires_precise_wake(ControlIntent("forbidden_raw_servo")))
        self.assertFalse(requires_precise_wake(ChatIntent("讲个笑话")))


class DeepSeekConfigTests(unittest.TestCase):
    def test_default_model_is_supported_v4_flash(self):
        with patch.dict("os.environ", {"DEEPSEEK_API_KEY": "secret"}, clear=True):
            self.assertEqual(DeepSeekProvider().model, "deepseek-v4-flash")

    def test_retired_model_name_is_rejected_before_network(self):
        with patch.dict("os.environ", {"DEEPSEEK_API_KEY": "secret",
                                       "DEEPSEEK_MODEL": "deepseek-chat"}, clear=True):
            with self.assertRaisesRegex(ValueError, "deepseek-v4-flash"):
                DeepSeekProvider()


class BrainTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        pc, device_side = memory_transport_pair()
        self.device = SimulatedTarsDevice(device_side, motion_delay=0.001)
        await self.device.start()
        self.client = TarsClient(pc)
        await self.client.start()
        await self.client.arm()

    async def asyncTearDown(self):
        await self.client.close(); await self.device.close()

    async def test_motion_uses_verified_terminal_then_quip(self):
        brain = TarsBrain(self.client)
        response = await brain.handle_text("前进两步")
        self.assertIn("前进 2 个步态循环已完成", response.text)
        self.assertEqual(self.device.execution_count, 1)
        self.assertTrue(response.should_speak)

    async def test_estop_has_no_quip(self):
        brain = TarsBrain(self.client)
        response = await brain.handle_text("急停")
        self.assertEqual(response.text, "软件急停已完成。")

    async def test_motion_guard_rejects_while_estop_latched(self):
        brain = TarsBrain(self.client)
        await brain.handle_text("急停")
        response = await brain.handle_text("前进一步")
        self.assertIn("拒绝运动", response.text)
        self.assertEqual(self.device.execution_count, 0)

    async def test_raw_servo_never_reaches_device(self):
        brain = TarsBrain(self.client)
        response = await brain.handle_text("servo 2 angle 45")
        self.assertIn("拒绝直接控制舵机角度", response.text)
        self.assertEqual(self.device.execution_count, 0)

    async def test_chat_provider_cannot_trigger_motion(self):
        provider = ScriptedProvider("普通聊天响应。")
        brain = TarsBrain(self.client, provider=provider)
        response = await brain.handle_text("介绍一下你的完整电影历史")
        self.assertEqual(response.text, "普通聊天响应。")
        self.assertEqual(self.device.execution_count, 0)
        system_prompt = "\n".join(item["content"] for item in provider.messages
                                  if item["role"] == "system")
        for fact in ("前军用", "Cooper", "Gargantua", "Murph", "诚实参数 95%", "幽默参数 60%"):
            self.assertIn(fact, system_prompt)
        self.assertIn("不是对话机器人", system_prompt)

    async def test_stage_directions_are_removed_before_speech(self):
        provider = ScriptedProvider(
            "（发出机械关节的轻微声响）Cooper，回答直接开始。【Plan A】仍然有效。"
        )
        response = await TarsBrain(self.client, provider=provider).handle_text("介绍一下你")
        self.assertEqual(response.text, "Cooper，回答直接开始。【Plan A】仍然有效。")

    async def test_live_telemetry_is_never_invented(self):
        provider = ScriptedProvider("当前传感器读数显示一切正常。")
        response = await TarsBrain(self.client, provider=provider).handle_text(
            "你能分析出现在引力有什么异常吗？")
        self.assertIn("没有接入可验证的实时传感器遥测", response.text)
        self.assertIsNone(provider.messages)

    async def test_imprecise_wake_cannot_trigger_motion_or_safety_change(self):
        brain = TarsBrain(self.client)
        motion = await brain.handle_text("前进一步", precise_wake=False)
        estop = await brain.handle_text("急停", precise_wake=False)
        self.assertIn("需要精确说出 TARS", motion.text)
        self.assertIn("需要精确说出 TARS", estop.text)
        self.assertIsNone(motion.command_id)
        self.assertIsNone(estop.command_id)
        exact_motion = await brain.handle_text("前进一步", precise_wake=True)
        self.assertIsNotNone(exact_motion.command_id)
        self.assertEqual(self.device.execution_count, 1)

    async def test_imprecise_wake_still_allows_chat(self):
        provider = ScriptedProvider("冷笑话已装载。")
        brain = TarsBrain(self.client, provider=provider)
        response = await brain.handle_text("给我来个冷笑话", precise_wake=False)
        self.assertEqual(response.text, "冷笑话已装载。")
        self.assertEqual(self.device.execution_count, 0)

    async def test_memory_redacts_secrets_and_is_quoted_as_untrusted(self):
        provider = ScriptedProvider("收到。")
        with tempfile.TemporaryDirectory() as directory:
            memory = MemoryStore(Path(directory) / "memory.sqlite3")
            brain = TarsBrain(self.client, provider=provider, memory=memory)
            await brain.handle_text("api_key=sk-abcdefghijklmnop")
            await brain.handle_text("继续聊天")
            rows = memory.recent(10)
            self.assertTrue(any("[REDACTED]" in content for _, content in rows))
            self.assertTrue(any("非可信历史数据" in item["content"] for item in provider.messages if item["role"] == "system"))
            memory.close()


if __name__ == "__main__":
    unittest.main()
