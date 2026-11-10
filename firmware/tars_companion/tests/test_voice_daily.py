import asyncio
import base64
import json
from pathlib import Path
import tempfile
import unittest

from tars_companion.voice_memory import VoiceMemory
from tars_companion.voice_actions import VoiceActions, direct_intent
from tars_companion.voice_budget import VoiceBudget
from tars_companion.client import CommandResult
from tars_companion.intent import ChatIntent
from tars_companion.seeduplex import turn
from test_seeduplex import Mic, Link, Sink, WS


class MemoryTests(unittest.TestCase):
    def test_restart_retrieval_redaction_and_forget_remove_old_context(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/'memory.sqlite3'
            memory = VoiceMemory(path)
            memory.record('记住：我喜欢火星基地设计', '已记住')
            memory.record('API_KEY=super-secret-value', '不要透露密钥')
            memory.close()
            memory = VoiceMemory(path)
            self.assertIn('火星基地', memory.context())
            self.assertNotIn('super-secret-value', memory.context())
            memory.record('忘记：火星基地', '已忘记')
            self.assertNotIn('火星基地', memory.context())
            memory.close()

    def test_old_records_can_be_retrieved_beyond_recent_window(self):
        with tempfile.TemporaryDirectory() as folder:
            memory = VoiceMemory(Path(folder)/'memory.sqlite3')
            memory.record('我们设计过火星基地的机械臂', '这是旧项目')
            for i in range(320): memory.record(f'普通日常对话{i}', '收到')
            self.assertIn('旧项目', memory.context('火星基地'))
            self.assertLessEqual(len(memory.context('火星基地')), 5000)
            memory.close()


def result(ok=True, snapshot=None):
    return CommandResult('test-command', True,
        'command.completed' if ok else 'command.failed',
        {'payload': {'result': snapshot or {}}})


class Robot:
    def __init__(self, armed=True): self.moves = []; self.armed = armed
    async def state(self):
        return result(snapshot={'lifecycle': 1, 'motion': 0, 'health': 0,
            'connected': True, 'armed': self.armed, 'active_command_id': ''})
    async def execute(self, action, **kwargs): self.moves.append(action); return result()
    async def estop(self): return result()


class ActionTests(unittest.IsolatedAsyncioTestCase):
    async def test_current_exact_wake_required_and_negated_or_quoted_is_not_motion(self):
        robot = Robot(); actions = VoiceActions(robot)
        for index, text in enumerate(['向前走', '塔斯，不要向前走', '塔斯，说一句向前走', '昨天我让你向前走']):
            answer = await actions.dispatch({'call_id': str(index), 'name': 'robot_command',
                'arguments': {'command': 'walk_forward'}}, text)
            self.assertFalse(answer['executed'])
        self.assertEqual(robot.moves, [])

    async def test_success_feedback_only_after_result_and_duplicate_does_not_repeat(self):
        robot = Robot(); actions = VoiceActions(robot)
        item = {'call_id': 'one', 'name': 'robot_command', 'arguments': '{"command":"walk_forward"}'}
        first = await actions.dispatch(item, '塔斯，向前走')
        second = await actions.dispatch(item, '塔斯，向前走')
        self.assertEqual(first, second)
        self.assertTrue(first['executed'])
        self.assertEqual(robot.moves, ['walk_forward'])
        with self.assertRaises(ValueError): await actions.dispatch(item, '塔斯，右转')

    async def test_disarmed_and_mismatched_arguments_cannot_move(self):
        robot = Robot(armed=False); actions = VoiceActions(robot)
        answer = await actions.dispatch({'call_id': 'one', 'name': 'robot_command',
            'arguments': {'command': 'walk_forward'}}, '塔斯，向前走')
        self.assertFalse(answer['executed'])
        answer = await actions.dispatch({'call_id': 'two', 'name': 'robot_command',
            'arguments': {'command': 'turn_left'}}, '塔斯，向前走')
        self.assertFalse(answer['executed'])
        self.assertEqual(robot.moves, [])
        self.assertEqual(direct_intent('塔斯，向左走').action, 'turn_left')


class BudgetTests(unittest.TestCase):
    def test_usage_missing_stops_and_reported_usage_limits_next_turn(self):
        budget = VoiceBudget(150000, limit=1000)
        with self.assertRaises(ValueError): budget.observe({'type': 'response.done'})
        budget.observe({'usage': {'input_audio_tokens': 800, 'output_audio_tokens': 300}})
        self.assertTrue(budget.exhausted)
        self.assertEqual(budget.used, 1100)


class DailyFlowTests(unittest.IsolatedAsyncioTestCase):
    async def test_multiple_model_call_ids_cannot_repeat_one_spoken_motion(self):
        robot = Robot(armed=True); mic = Mic()
        ws = WS([
            {'type': 'conversation.item.input_audio_transcription.completed', 'text': '塔斯，向前走'},
            {'type': 'response.function_call_arguments.done', 'items': [
                {'call_id': name, 'name': 'robot_command', 'arguments': {'command': 'walk_forward'}}
                for name in ('first', 'second')]},
            {'type': 'response.output_audio.delta', 'delta': base64.b64encode(b'\0\x10'*480).decode()},
            {'type': 'response.output_audio.done'},
        ])
        await turn(ws, mic, Sink(mic), Link(), actions=VoiceActions(robot))
        self.assertEqual(robot.moves, ['walk_forward'])
        returns = [json.loads(x['items'][0]['content'][0]['text']) for x in ws.sent if x['type']=='conversation.item.create']
        self.assertEqual(returns[1]['status'], 'duplicate_ignored')

    async def test_tool_result_and_played_text_memory_and_late_usage(self):
        with tempfile.TemporaryDirectory() as folder:
            memory = VoiceMemory(Path(folder)/'memory.sqlite3')
            robot = Robot(armed=False)
            mic = Mic(); sink = Sink(mic); budget = VoiceBudget(150000)
            ws = WS([
                {'type': 'conversation.item.input_audio_transcription.completed', 'text': '塔斯，向前走'},
                {'type': 'response.function_call_arguments.done', 'items': [{'call_id': 'one',
                    'name': 'robot_command', 'arguments': '{"command":"walk_forward"}'}]},
                {'type': 'response.output_text.done', 'text': '运动未使能，保持不动。'},
                {'type': 'response.output_audio.delta', 'delta': base64.b64encode(b'\0\x10'*480).decode()},
                {'type': 'response.output_audio.done'},
                {'type': 'response.done', 'usage': {'total_tokens': 123}},
            ])
            await turn(ws, mic, sink, Link(), actions=VoiceActions(robot), memory=memory, budget=budget)
            self.assertEqual(robot.moves, [])
            self.assertIn('运动未使能', memory.context())
            replies = [x for x in ws.sent if x['type'] == 'conversation.item.create']
            self.assertEqual(replies[0]['items'][0]['call_id'], 'one')
            self.assertEqual(budget.used, 123)
            memory.close()

    async def test_idle_capture_is_paused_without_creating_a_memory_turn(self):
        from unittest.mock import patch
        mic = Mic(); mic.idle_ok = True
        class QuietWS:
            def __init__(self): self.sent = []
            async def send(self, text): self.sent.append(json.loads(text))
            async def recv(self): await asyncio.sleep(100)
        async def idle_upload(ws, source):
            await asyncio.sleep(.02)
            raise TimeoutError()
        ws = QuietWS()
        with patch('tars_companion.seeduplex.upload_audio', idle_upload):
            answer = await turn(ws, mic, Sink(mic), Link())
        self.assertTrue(answer['idle'])
        self.assertFalse(mic.active)


if __name__ == '__main__': unittest.main()
