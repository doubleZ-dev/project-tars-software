"""Continuous motionpack voice service. No hardware or cloud access on import."""
import argparse
import asyncio
import contextlib
import json
import os
import time
import ssl
from pathlib import Path

from .voice_service import instructions, parser
from .allowance import Allowance
from .audio_levels import BodyMicrophone
from .local_listening import LocalListeningMicrophone
from .motionpack_voice import MotionpackActions, TOOL, CATALOG, ALIASES
from .robot_runtime import RuntimeClient, console
from .seeduplex import URL, configuration, receive, send, turn, BufferedRobotOutput
from .xiaozhi_audio import XiaozhiServer, XiaozhiOutput
from .xiaozhi_control import ControlPool
from websockets.exceptions import ConnectionClosed
from .voice_budget import VoiceBudget
from .voice_memory import VoiceMemory
from .wifi_audio import LanAudioServer, NetworkJsonTransport, load_profile


async def recover_cloud_timeout(link, actions, conversation_timeout):
    if link.closed.is_set() or link.mode not in ('idle', 'capture'):
        raise TimeoutError('device_not_ready_for_timeout_recovery')
    actions.discard()
    await link.interaction('IDLE')
    return time.monotonic() + conversation_timeout


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
    server = XiaozhiServer(profile['token'])
    control_pool = ControlPool(profile, args.allow_motion)
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
        await server.start(profile['host'])
        await control_pool.start()
        while True:
            print('Waiting for paired TARS; no other controller may own this connection.', flush=True)
            try:
                link = await server.wait_connected(30)
            except TimeoutError:
                continue
            client = control_pool.snapshot()
            try:
                print('WSS Opus voice ready; local wake and memory enabled.', flush=True)
                while not link.closed.is_set():
                    source = LocalListeningMicrophone(BodyMicrophone(link, gain=4), link,
                        threshold=args.vad_threshold, wakeword=wakeword, awake_until=awake_until, continuous_capture=True)
                    await source.prepare()
                    client = control_pool.snapshot()
                    allowance.reserve()  # committed before any cloud request
                    budget = VoiceBudget(args.free_tokens)
                    actions = MotionpackActions(client, link, enabled=motion_enabled,
                                                support_confirmed=args.support_confirmed, addressed=True, independent_audio=True)
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
                                    result = await turn(ws, source, XiaozhiOutput(link, 20), link,
                                                        actions=actions, memory=memory, budget=budget)
                            finally:
                                with contextlib.suppress(Exception): await send(ws, 'session.close')
                        allowance.settle(budget.used)
                        motion_enabled = actions.enabled
                        results = result.get('actions', [])
                        if results: last_action = results[-1]
                        awake_until = 0 if result.get('quit') else time.monotonic() + args.conversation_timeout
                        if result.get('quit'): print('Conversation ended; returning to local wake-word standby.', flush=True)
                    except TimeoutError:
                        # A cloud deadline is not evidence of robot transport loss.
                        # Keep the healthy LAN connection, discard this action and
                        # listen for a NEW utterance; never replay the timed-out one.
                        awake_until = await recover_cloud_timeout(link, actions, args.conversation_timeout)
                        print('Cloud request timed out; listening for a new utterance. No action replay.', flush=True)
                    except RuntimeError as error:
                        if str(error) not in {'audio_upload_fell_behind', 'session_or_transcription_failed'}:
                            raise
                        awake_until = await recover_cloud_timeout(link, actions, args.conversation_timeout)
                        print('Voice turn failed (' + str(error) + '); listening for a NEW utterance. No action replay.', flush=True)
                    finally:
                        actions.discard()
                        await source.stop_capture()
                    await asyncio.sleep(.3)
            except TimeoutError:
                awake_until = 0
                print('Voice/device operation timed out; reconnecting without replay.', flush=True)
            except (ConnectionError, OSError, ConnectionClosed):
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
            except ValueError as error:
                if str(error) != 'utterance_too_long_repeat_shorter': raise
                awake_until = 0
                print('Utterance exceeded 18 seconds. Returning to standby; use shorter sentences.', flush=True)
            finally:
                if source:
                    with contextlib.suppress(ConnectionError, TimeoutError): await source.stop_capture()
                if link: await link.close()

    try:
        operator = asyncio.create_task(controls())
        work = asyncio.create_task(conversation())
        done, _ = await asyncio.wait({operator, work}, return_when=asyncio.FIRST_COMPLETED)
        for task in done: task.result()
    finally:
        for task in (operator, work):
            if task: task.cancel()
        await asyncio.gather(*(t for t in (operator, work) if t), return_exceptions=True)
        await control_pool.close()
        await server.close()
        memory.close()
        allowance.close()


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
