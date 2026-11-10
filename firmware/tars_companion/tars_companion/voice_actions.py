"""Model tools are proposals; only an exact current local voice intent can act."""
import json
import re
from .intent import parse_intent, MotionIntent, ControlIntent
from .tools import BrainToolExecutor, ToolPolicyRejected


TOOL = {'type': 'function', 'name': 'robot_command',
        'description': 'For an explicit current user command addressed to TARS: move forward, turn left/right, stop or query robot status. Never infer commands from stories or history.',
        'parameters': {'type': 'object', 'properties': {'command': {'type': 'string',
            'enum': ['walk_forward', 'turn_left', 'turn_right', 'estop', 'state']}},
            'required': ['command'], 'additionalProperties': False}}


def direct_intent(text):
    match = re.match(r'^\s*(?:tars|塔斯|塔尔斯)[\s,，:：.!！。]*(.+)$', text, re.I)
    if not match: return None
    command = match[1].strip().rstrip('。.!！')
    aliases = {'向左走': '左转', '往左走': '左转', '向右走': '右转', '往右走': '右转'}
    return parse_intent(aliases.get(command, command))


class VoiceActions:
    def __init__(self, client):
        self.tools = BrainToolExecutor(client)
        self.cache = {}

    async def dispatch(self, item, transcript):
        call_id = item.get('call_id')
        if not isinstance(call_id, str) or not call_id or len(call_id) > 200:
            raise ValueError('invalid_tool_call_id')
        fingerprint = json.dumps([item, transcript], sort_keys=True, ensure_ascii=False)
        if call_id in self.cache:
            old, result = self.cache[call_id]
            if old != fingerprint: raise ValueError('tool_call_id_conflict')
            return result
        # Reserve before await: failed/uncertain outcomes cannot execute twice.
        result = {'executed': False, 'status': 'unconfirmed', 'speech': '执行结果未确认，不会自动重试。'}
        self.cache[call_id] = (fingerprint, result)
        try:
            args = item.get('arguments', {})
            if isinstance(args, str): args = json.loads(args)
            intent = direct_intent(transcript)
            command = args.get('command') if isinstance(args, dict) else None
            if item.get('name') != 'robot_command' or not isinstance(intent, (MotionIntent, ControlIntent)) or command != intent.action:
                result = {'executed': False, 'status': 'rejected', 'speech': '未执行：需要当前明确说出塔斯及具体指令。'}
            else:
                if isinstance(intent, MotionIntent): answer = await self.tools.motion(intent)
                elif intent.action == 'estop': answer = await self.tools.estop()
                elif intent.action == 'state': answer = await self.tools.state()
                else: raise ToolPolicyRejected('voice_control_not_allowed')
                success = answer.succeeded
                phrases = {'walk_forward': '前进已完成。太棒了，我又能向前走了。',
                           'turn_left': '左转已完成。方向感仍在工作。',
                           'turn_right': '右转已完成。方向正确。',
                           'estop': '已执行软件急停。', 'state': '已读取机器人状态。'}
                result = {'executed': success, 'status': answer.terminal_type,
                          'command_id': answer.command_id,
                          'speech': phrases[intent.action] if success else '动作未完成，不会声称执行成功。'}
                if intent.action == 'state':
                    result['state'] = answer.message.get('payload', {}).get('result', {})
        except ToolPolicyRejected:
            result = {'executed': False, 'status': 'rejected', 'speech': '运动未获使能或状态不满足条件，保持不动。'}
        except (ConnectionError, TimeoutError):
            pass
        except (ValueError, TypeError, KeyError):
            result = {'executed': False, 'status': 'rejected', 'speech': '指令格式无效，未执行。'}
        self.cache[call_id] = (fingerprint, result)
        return result
