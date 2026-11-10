"""Normal conversational mode using the director's single shared connections."""
import asyncio
import contextlib
import json
import os
import ssl
import time
from pathlib import Path


async def converse(director):
    from websockets.asyncio.client import connect
    from .voice_service import instructions
    from .allowance import Allowance
    from .voice_budget import VoiceBudget
    from .voice_memory import VoiceMemory
    from .wakeword import TarsWakeWord
    from .local_listening import LocalListeningMicrophone
    from .audio_levels import BodyMicrophone
    from .motionpack_voice import MotionpackActions, TOOL
    from .seeduplex import URL, configuration, receive, send, turn
    from .xiaozhi_audio import XiaozhiOutput
    source=link=actions=memory=allowance=None
    try:
        settings=json.loads(Path('tars-service.local.json').read_text(encoding='utf-8-sig'))
        persona=json.loads(Path('tars-seeduplex.json').read_text(encoding='utf-8-sig'))
        # Use the same local accounting and memory files as the established service.
        from .voice_service import parser
        args=parser().parse_args([])
        wakeword=TarsWakeWord(args.wake_model)
        memory=VoiceMemory(args.memory)
        allowance=Allowance(args.allowance,settings['free_tokens'])
        tls=await asyncio.to_thread(ssl.create_default_context)
        awake_until=0;last_action=None
        while director.mode=='voice':
            link=await director.audio.wait_connected(30)
            client=director.pool.snapshot()
            source=LocalListeningMicrophone(BodyMicrophone(link,gain=4),link,
                threshold=settings.get('vad_threshold',500),wakeword=wakeword,awake_until=awake_until,continuous_capture=True)
            await source.prepare()
            allowance.reserve()
            budget=VoiceBudget(settings['free_tokens'])
            actions=MotionpackActions(client,link,enabled=settings.get('motion_enabled',False),
                support_confirmed=settings.get('support_confirmed',False),addressed=True,independent_audio=True)
            request=configuration(instructions(persona,memory,last_action),[TOOL,{
                'type':'function','name':'memory_lookup','description':'Retrieve local historical context.',
                'parameters':{'type':'object','properties':{'query':{'type':'string'}},'additionalProperties':False}}])
            try:
                async with connect(URL,additional_headers={'X-Api-Key':os.environ['DOUBAO_API_KEY']},
                        open_timeout=10,close_timeout=2,max_size=2_000_000,compression=None,ssl=tls) as ws:
                    await ws.send(json.dumps(request,ensure_ascii=False))
                    if (await receive(ws))['type']!='session.created': raise RuntimeError('session_not_created')
                    try:
                        async with asyncio.timeout(100):
                            result=await turn(ws,source,XiaozhiOutput(link,20),link,actions=actions,memory=memory,budget=budget)
                    finally:
                        with contextlib.suppress(Exception): await send(ws,'session.close')
                allowance.settle(budget.used)
                results=result.get('actions',[])
                if results: last_action=results[-1]
                awake_until=0 if result.get('quit') else time.monotonic()+180
            except (TimeoutError,ConnectionError,RuntimeError) as exc:
                director.log('语音本轮结束：'+type(exc).__name__+'；不重放动作')
                awake_until=0
                if link.mode not in ('idle','capture') or link.closed.is_set():
                    await link.close()
            finally:
                actions.discard()
                with contextlib.suppress(Exception): await source.stop_capture()
            await asyncio.sleep(.3)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        director.error='日常语音未启动或已停止：'+type(exc).__name__
        director.log(director.error)
        director.mode='director'
    finally:
        if actions: actions.discard()
        if source:
            with contextlib.suppress(Exception): await source.stop_capture()
        await director.halt_hardware(link=link)
        if memory: memory.close()
        if allowance: allowance.close()
