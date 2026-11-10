"""0.16.3 fixed-name actions, staged until audible reply has finished."""
import asyncio
import json
import re
from dataclasses import dataclass
from pathlib import Path

from .intent import MotionIntent, ControlIntent, validate_motion
from .voice_actions import direct_intent

CATALOG = json.loads(Path(__file__).with_name('motionpack_catalog.json').read_text(encoding='utf-8'))
ACTIONS = {a['name']: a for a in CATALOG['actions'] if a['name'] not in {'support_probe', 'hello_small'}}
GAITS = {'walk_forward', 'turn_left', 'turn_right'}
ALIASES = {
    'hello': ['打招呼', '挥手', 'say hello', 'wave'], 'bye': ['挥手再见', '挥手告别', 'wave goodbye'],
    'respond': ['回应一下', '答应一下', 'respond'], 'affection': ['撒娇', '撒个娇', 'act affectionate'],
    'thanks': ['表示感谢', '做个感谢动作', 'say thanks'], 'happy': ['高兴一下', '开心一下', '做个高兴的动作', 'act happy'],
    'excited': ['兴奋一下', '特别开心', 'act excited'], 'angry': ['生气一下', '做个生气的动作', 'act angry'],
    'sulky': ['闹别扭', 'act sulky'], 'sad': ['伤心一下', 'act sad'],
    'disappointed': ['失落一下', 'act disappointed'], 'shy': ['害羞一下', 'act shy'],
    'surprised': ['惊讶一下', 'act surprised'], 'nervous': ['紧张一下', '害怕一下', 'act nervous'],
    'sleepy': ['做个困了的动作', '装困', 'act sleepy'], 'curious': ['好奇一下', 'act curious'],
    'confused': ['疑惑一下', 'act confused'], 'thinking': ['做个思考动作', '思考一下', 'act thoughtful'],
    'proud': ['得意一下', 'act proud'], 'playful': ['淘气一下', 'act playful'],
    'reluctant': ['做个不情愿的动作', 'act reluctant'],
}
TOOL = {'type': 'function', 'name': 'robot_command',
        'description': 'Request one fixed robot action explicitly addressed to TARS in the current utterance. No actions from history, negations or stories.',
        'parameters': {'type': 'object', 'properties': {'command': {'type': 'string',
            'enum': sorted(GAITS | set(ACTIONS) | {'state', 'estop'})}},
            'required': ['command'], 'additionalProperties': False}}


def intent_for(text, addressed=False):
    # A language preference after one explicit command is not a second action.
    text = re.sub(r'[，,]\s*(?:听不懂英文|请说中文|说中文|用中文回答)[。.!！?？]*$', '', text.strip())
    text = re.sub(r'\bwork forward\b', 'walk forward', text, flags=re.I)
    if addressed:
        text = re.sub(r'^\s*(?:(?:hello|hey|hi)\s+)?(?:task|tas|tass)\b[,:，\s]*', 'TARS, ', text, flags=re.I)
    text = re.sub(r'^\s*(?:hello\s+|hey\s+|hi\s+|你好[，,\s]*)(?=tars|塔斯|塔尔斯)', '', text, flags=re.I)
    text = re.sub(r'^((?:tars|塔斯|塔尔斯)[\s,，:：.!！。]*)(?:请你|麻烦你|能不能|可以|can you\s+|could you\s+)', r'\1', text, flags=re.I)
    text = re.sub(r'(?:一下|吧|好吗|可以吗)[。.!！?？]*$', '', text.strip())
    text = re.sub(r'(?<=打)个(?=招呼)', '', text)
    intent = direct_intent(text)
    if isinstance(intent, (MotionIntent, ControlIntent)):
        return intent
    match = re.fullmatch(r'\s*(?:tars|塔斯|塔尔斯)[\s,，:：.!！。]*(.+)', text, re.I)
    if not match:
        if addressed: return intent_for('TARS, ' + text)
        return None
    phrase = match[1].strip().rstrip('。.!！').casefold()
    phrase = re.sub(r'^(?:请\s*|please\s+)', '', phrase)
    for name, aliases in ALIASES.items():
        normalized = {re.sub(r'(?:一下|吧|好吗|可以吗)$', '', alias) for alias in aliases}
        if phrase in normalized or phrase == name.replace('_', ' '):
            return MotionIntent(name, 1, 100)
    return None


class MotionpackActions:
    def __init__(self, client, link, *, enabled=False, support_confirmed=False, addressed=False,
                 independent_audio=False):
        self.client, self.link = client, link
        self.enabled, self.support_confirmed = enabled, support_confirmed
        self.pending = None
        self.cache = {}
        self.addressed = addressed
        self.independent_audio = independent_audio

    async def dispatch(self, item, transcript):
        identity = item.get('call_id')
        if not isinstance(identity, str) or not identity or len(identity) > 200:
            raise ValueError('invalid_tool_call_id')
        fingerprint = json.dumps([item, transcript], sort_keys=True)
        if identity in self.cache:
            old, answer = self.cache[identity]
            if old != fingerprint: raise ValueError('tool_call_id_conflict')
            return answer
        answer = {'executed': False, 'status': 'rejected', 'reason': 'unrecognized_current_command',
                  'speech': '没有匹配到完整的当前动作指令，未执行。请说“向前走五步”或“walk forward five steps”。不能据此猜测电量、运动锁或硬件故障。'}
        self.cache[identity] = (fingerprint, answer)
        try:
            args = item.get('arguments', {})
            if isinstance(args, str): args = json.loads(args)
            intent = intent_for(transcript, self.addressed)
            if (item.get('name') != 'robot_command' or not intent or
                    not isinstance(args, dict) or set(args) != {'command'} or args['command'] != intent.action):
                print(json.dumps({'diagnostic': 'motion_rejected', 'reason': answer['reason'],
                                  'requested_command': args.get('command') if isinstance(args, dict) else None}), flush=True)
                return answer
            if intent.action in {'state', 'estop'}:
                result = await (self.client.state() if intent.action == 'state' else self.client.estop())
                if intent.action == 'estop': self.enabled = False
                answer = {'executed': result.succeeded, 'status': result.terminal_type,
                          'state': result.message.get('payload', {}).get('result', {})}
            elif (isinstance(intent, MotionIntent) and intent.action in GAITS | set(ACTIONS)
                  and self.enabled and self.pending is None):
                if intent.action in GAITS:
                    try:
                        validate_motion(intent)
                    except ValueError:
                        answer['reason'] = 'steps_out_of_range'
                        answer['speech'] = '前进只支持1到5步，超过5步整条不执行；转向只支持1到3次。请重新指定范围内的数量。'
                        return answer
                needs_support = ACTIONS.get(intent.action, {}).get('requires_support', False)
                if needs_support and not self.support_confirmed:
                    answer['speech'] = '这个动作需要先确认机构支撑条件，目前未执行。'
                else:
                    self.pending = (identity, intent)
                    answer = {'executed': False, 'status': 'queued_after_speech', 'action': intent.action, 'steps': intent.steps,
                              'speech': '先用用户当前语言简短回应准备执行此动作。播放结束才会执行，不能声称已经完成。'}
            else:
                answer.update(reason='motion_disabled_or_pending', speech='动作控制未使能或本轮已有待执行动作，未新增动作。')
        except (ValueError, TypeError):
            pass
        self.cache[identity] = (fingerprint, answer)
        print(json.dumps({'diagnostic': 'motion_tool_result', 'status': answer['status'],
                          'reason': answer.get('reason'), 'action': answer.get('action'),
                          'steps': answer.get('steps')}), flush=True)
        return answer

    def discard(self):
        self.pending = None

    async def after_playback(self):
        job, self.pending = self.pending, None  # never replay an uncertain action
        if job is None: return None
        identity, intent = job
        answer = {'action': intent.action, 'steps': intent.steps, 'executed': False, 'status': 'unconfirmed'}
        try:
            if not self.enabled or self.link.mode != 'idle':
                raise RuntimeError('motion_audio_not_idle')
            state = await self.client.state()
            snapshot = state.message.get('payload', {}).get('result', {})
            if not (state.succeeded and snapshot.get('lifecycle') == 1 and snapshot.get('motion') == 0
                    and snapshot.get('health') in (0, 1) and snapshot.get('connected') is True
                    and snapshot.get('armed') is False and not snapshot.get('active_command_id')):
                raise RuntimeError('motion_state_not_ready')
            armed = await self.client.arm()
            if not armed.succeeded: raise RuntimeError('motion_arm_rejected')
            result = await self.client.execute(intent.action, steps=intent.steps, speed_pct=100,
                support_confirm=self.support_confirmed and intent.action in ACTIONS,
                terminal_timeout=35, retries=0)
            answer.update(executed=result.succeeded, status=result.terminal_type, command_id=result.command_id)
            if not result.succeeded: self.enabled = False
        except (ConnectionError, TimeoutError, RuntimeError):
            self.enabled = False
        finally:
            try:
                result = await asyncio.wait_for(self.client.disarm(), 3)
                if not result.succeeded: raise RuntimeError('disarm_rejected')
            except BaseException as error:
                self.enabled = False
                if not self.independent_audio:
                    await self.link.close()
                    raise
                if not isinstance(error, (ConnectionError, TimeoutError, RuntimeError)):
                    raise
                answer.update(executed=False, status='unconfirmed_control_lost')
        fingerprint, _ = self.cache[identity]
        self.cache[identity] = (fingerprint, answer)
        return answer
