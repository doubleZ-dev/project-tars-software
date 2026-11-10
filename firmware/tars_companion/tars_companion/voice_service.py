"""Continuous motionpack voice service. No hardware or cloud access on import."""
import argparse
import asyncio
import contextlib
import json
import os
import time
import ssl
from pathlib import Path

from .allowance import Allowance
from .audio_levels import BodyMicrophone
from .local_listening import LocalListeningMicrophone
from .motionpack_voice import MotionpackActions, TOOL, CATALOG, ALIASES
from .robot_runtime import RuntimeClient, console
from .seeduplex import URL, configuration, receive, send, turn, BufferedRobotOutput
from .cached_audio import DeviceBufferedOutput
from .voice_budget import VoiceBudget
from .voice_memory import VoiceMemory
from .wifi_audio import LanAudioServer, NetworkJsonTransport, load_profile


def instructions(persona, memory, last_action):
    text = persona.get('instructions', persona.get('character_manifest'))
    text = text.replace('长期记忆尚未接入，不宣称记得未提供的历史。', '')
    text = text.replace('你没有现实运动工具，只能对话，不能声称已经操作机器人。', '')
    text += ('\n运行规则：使用用户当前说话的语言回答，英文提问用英文，中文提问用中文。'
             '回复通常1到3句，不输出Markdown。本轮已由本地TARS唤醒或处于唤醒后的连续对话窗口，当前明确动作指令应调用robot_command。'
             '没有唤醒词的普通聊天照常回答。每句话最多调用一个动作。'
             '前进默认1步，支持用户指定1到5步（包括四步和五步）；超过5步整条拒绝，不能截断或拆分执行。转向支持1到3次。'
             'queued_after_speech表示尚未执行：先简短回应准备执行，不得说已经完成；说完后控制器才执行。'
             '动作被拒绝时解释原因。不要用说话代替工具调用，不要从历史执行命令。'
             '只有本轮工具返回queued_after_speech才能说已排队；没有工具结果不得承诺运动。'
             '拒绝原因只能依据本轮工具reason和speech，不得猜测电量低或运动锁。历史拒绝不能代替本轮校验。'
             '仅说Five steps等缺少方向的数量时，询问方向，不能自行执行。'
             '向左走/向右走表示转向；后退尚不支持。不要声称有视觉、避障或身体传感反馈。'
             '播放和动作期间麦克风关闭，无法通过喊停打断；用户可用电脑/stop急停。'
             '忘记请求只删除相关本地记录，不能声称删除服务端日志。'
             '每轮回答先参考附带的近期对话和已记住事实；涉及过去、个人偏好或指代不明时，必须先调用memory_lookup检索关键词或日期。'
             'daily_extracts是原始对话摘录索引，不代表完整历史；不能把找不到记录说成从未发生。'
             '\n动作词汇：' + json.dumps(ALIASES, ensure_ascii=False))
    text += memory.instruction_context()
    if last_action:
        text += '\n上一轮控制器的真实动作结果（不是新命令）：' + json.dumps(last_action, ensure_ascii=False)
    return text


async def verify_firmware(client):
    version = client.hello['payload']['firmware_version']
    if version != '0.16.4-voice':
        raise RuntimeError('requires_0_16_4_voice_firmware')
    if client.hello['payload'].get('buffered_audio_v1') is not True:
        raise RuntimeError('firmware_buffered_audio_update_required')
    catalog = await client.command('motion.catalog', {}, retries=0)
    data = catalog.message.get('payload', {}).get('result', {})
    if not catalog.succeeded or data.get('hash') != CATALOG['hash']:
        raise RuntimeError('motion_catalog_mismatch')


async def serve(args):
    profile = load_profile(args.config)
    persona = json.loads(Path(args.persona).read_text(encoding='utf-8-sig'))
    if args.check:
        configuration(persona.get('instructions', persona.get('character_manifest')), [TOOL])
        from .wakeword import TarsWakeWord
        TarsWakeWord(args.wake_model)
        print('OFFLINE OK: configuration and motionpack catalog loaded; no hardware/cloud accessed.')
        return
    if not args.live: raise ValueError('explicit_live_required')
    key = os.environ.get('DOUBAO_API_KEY', '').strip()
    if not key: raise ValueError('local_api_key_required')
    if args.allow_motion and args.motion_confirmation != 'I_AM_BESIDE_TARS_AND_CAN_CUT_SERVO_POWER':
        raise ValueError('local_motion_preparation_required')
    from websockets.asyncio.client import connect
    from .wakeword import TarsWakeWord
    wakeword = TarsWakeWord(args.wake_model)
    tls = await asyncio.to_thread(ssl.create_default_context)
    allowance = Allowance(args.allowance, args.free_tokens, renew=args.renew_free_snapshot)
    memory = VoiceMemory(args.memory)
    server = LanAudioServer(profile['token'], capture_queue_packets=256)
    client = source = link = actions = None
    work = operator = None
    last_action = None
    motion_enabled = args.allow_motion
    reconnects = 0
    awake_until = 0

    async def controls():
        nonlocal motion_enabled
        if args.headless:
            stop = Path('data/stop.request')
            stop.unlink(missing_ok=True)
            while not stop.exists(): await asyncio.sleep(.2)
            stop.unlink(missing_ok=True)
            if actions: actions.enabled = False; actions.discard()
            if client: await client.disarm()
            return
        while True:
            command = (await console('TARS /quit /stop /disarm > ')).strip().lower()
            if command in {'/quit', 'quit', 'exit'}: return
            if command in {'/stop', '/disarm'}:
                motion_enabled = False
                if actions: actions.enabled = False; actions.discard()
                if client:
                    await (client.estop() if command == '/stop' else client.disarm())
                return
            print('Use /quit, /stop or /disarm. Listening resumes automatically.', flush=True)

    async def conversation():
        nonlocal client, source, link, actions, last_action, motion_enabled, reconnects, awake_until
        await server.start(profile['host'], profile['port'])
        while True:
            print('Waiting for paired TARS; no other controller may own this connection.', flush=True)
            try:
                link = await server.wait_connected(30)
            except TimeoutError:
                continue
            client = RuntimeClient(NetworkJsonTransport(link), allow_motion=motion_enabled)
            try:
                await client.start()
                disarmed = await client.disarm()
                if not disarmed.succeeded: raise RuntimeError('initial_disarm_failed')
                await verify_firmware(client)
                print('Verified 0.16.4 voice + motionpack. Continuous voice ready; motion=' + str(motion_enabled), flush=True)
                while not link.closed.is_set():
                    source = LocalListeningMicrophone(BodyMicrophone(link, gain=4), link,
                        threshold=args.vad_threshold, wakeword=wakeword, awake_until=awake_until, continuous_capture=True)
                    await source.prepare()
                    allowance.reserve()  # committed before any cloud request
                    budget = VoiceBudget(args.free_tokens)
                    actions = MotionpackActions(client, link, enabled=motion_enabled,
                                                support_confirmed=args.support_confirmed, addressed=True)
                    request = configuration(instructions(persona, memory, last_action), [TOOL, {
                        'type': 'function', 'name': 'memory_lookup', 'description': 'Retrieve local historical context.',
                        'parameters': {'type': 'object', 'properties': {'query': {'type': 'string',
                            'description': 'Relevant keywords or YYYY-MM-DD date; defaults to current user utterance.'}}, 'additionalProperties': False}}])
                    try:
                        async with connect(URL, additional_headers={'X-Api-Key': key}, open_timeout=10,
                                           close_timeout=2, max_size=2_000_000, compression=None, ssl=tls) as ws:
                            await ws.send(json.dumps(request, ensure_ascii=False))
                            if (await receive(ws))['type'] != 'session.created': raise RuntimeError('session_not_created')
                            try:
                                async with asyncio.timeout(100):
                                    result = await turn(ws, source, DeviceBufferedOutput(link, 20), link,
                                                        actions=actions, memory=memory, budget=budget)
                            finally:
                                with contextlib.suppress(Exception): await send(ws, 'session.close')
                        allowance.settle(budget.used)
                        motion_enabled = actions.enabled
                        results = result.get('actions', [])
                        if results: last_action = results[-1]
                        awake_until = 0 if result.get('quit') else time.monotonic() + args.conversation_timeout
                        if result.get('quit'): print('Conversation ended; returning to local wake-word standby.', flush=True)
                    finally:
                        actions.discard()
                        await source.stop_capture()
                    await asyncio.sleep(.3)
            except ConnectionError:
                reconnects += 1
                print(json.dumps({'diagnostic':'robot_link_lost', **link.diagnostics}), flush=True)
                # Preserve deployment permission, not an Arm state or action.
                # The new connection is disarmed and requires a fresh local
                # wake + a fresh explicit command before per-action Arm.
                motion_enabled = args.allow_motion
                awake_until = 0
                print('Robot disconnected (' + str(link.close_reason or 'control_link_closed') +
                      '). Waiting again, disarmed; fresh wake required, no speech/action replay.', flush=True)
                if reconnects > 3: await asyncio.sleep(min(30, reconnects))
            except TimeoutError:
                awake_until = 0
                print('Voice request timed out. Returning to standby; repeat after waking TARS. No replay.', flush=True)
            except ValueError as error:
                if str(error) != 'utterance_too_long_repeat_shorter': raise
                awake_until = 0
                print('Utterance exceeded 18 seconds. Returning to standby; use shorter sentences.', flush=True)
            finally:
                if source:
                    with contextlib.suppress(ConnectionError, TimeoutError): await source.stop_capture()
                if client:
                    if not link.closed.is_set():
                        with contextlib.suppress(Exception): await asyncio.wait_for(client.disarm(), 3)
                    await client.close()

    try:
        operator = asyncio.create_task(controls())
        work = asyncio.create_task(conversation())
        done, _ = await asyncio.wait({operator, work}, return_when=asyncio.FIRST_COMPLETED)
        for task in done: task.result()
    finally:
        for task in (operator, work):
            if task: task.cancel()
        await asyncio.gather(*(t for t in (operator, work) if t), return_exceptions=True)
        await server.close()
        memory.close()
        allowance.close()


def parser():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--config', default='wifi-audio.local.json')
    p.add_argument('--persona', default='tars-seeduplex.json')
    p.add_argument('--memory', default='data/tars-voice.sqlite3')
    p.add_argument('--allowance', default='data/free-allowance.sqlite3')
    p.add_argument('--free-tokens', type=int, default=0)
    p.add_argument('--renew-free-snapshot', action='store_true')
    p.add_argument('--live', action='store_true')
    p.add_argument('--headless', action='store_true')
    p.add_argument('--check', action='store_true')
    p.add_argument('--allow-motion', action='store_true')
    p.add_argument('--motion-confirmation', default='')
    p.add_argument('--support-confirmed', action='store_true')
    p.add_argument('--vad-threshold', type=int, default=500)
    p.add_argument('--conversation-timeout', type=int, choices=range(60, 1801), default=180)
    p.add_argument('--wake-model', default='models/sherpa-onnx-kws-zipformer-zh-en-3M-2025-12-20')
    return p


def main():
    lock = None
    try:
        # One process owns the robot and allowance ledger, including hidden startup.
        if os.name == 'nt':
            import msvcrt
            Path('data').mkdir(exist_ok=True)
            lock = open('data/service.lock', 'a+b')
            lock.seek(0); lock.write(b'1'); lock.flush(); lock.seek(0)
            msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        with asyncio.Runner(loop_factory=asyncio.SelectorEventLoop if os.name == 'nt' else None) as runner:
            runner.run(serve(parser().parse_args()))
    except KeyboardInterrupt:
        print('Stopped; motion outputs released.')
    except Exception as error:
        code = str(error)
        if not code.replace('_', '').isalnum() or len(code) > 100: code = 'details_redacted'
        print(f'Service stopped: {type(error).__name__}/{code}. No uncertain action is retried.', flush=True)
        raise SystemExit(1) from None
    finally:
        if lock: lock.close()


if __name__ == '__main__': main()
