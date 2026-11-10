"""One non-motion daily-mode integration probe using saved synthetic input."""
import asyncio
import contextlib
import json
import os
import sys
from pathlib import Path
import wave
from websockets.asyncio.client import connect
from .seeduplex import URL, configuration, receive, send, turn, BufferedRobotOutput
from .seeduplex_loopback_probe import SavedInput
from .wifi_audio import LanAudioServer, load_profile, NetworkJsonTransport
from .robot_runtime import RuntimeClient
from .voice_memory import VoiceMemory
from .voice_actions import VoiceActions, TOOL
from .voice_budget import VoiceBudget


async def main():
    profile = load_profile('wifi-audio.local.json')
    server = LanAudioServer(profile['token']); client = output = None
    memory = VoiceMemory('evidence/daily-probe.sqlite3')
    budget = VoiceBudget(100000)  # small, isolated acceptance probe
    no_robot = '--no-robot' in sys.argv
    try:
        with wave.open('evidence/tars-probe-body-16k.wav', 'rb') as wav:
            pcm = wav.readframes(wav.getnframes())
        if no_robot:
            class QuietLink:
                async def interaction(self, state): pass
                async def display_dialogue(self, role, text): pass
            link = QuietLink()
        else:
            await server.start(profile['host'], profile['port'])
            link = await server.wait_connected(20)
            client = RuntimeClient(NetworkJsonTransport(link), allow_motion=False)
            await client.start(); await client.disarm()
        instructions = ('You are TARS. Answer in English, one short sentence. '
                        'Only robot_command tool results can confirm actual actions. '
                        + memory.instruction_context())
        async with connect(URL, additional_headers={'X-Api-Key': os.environ['DOUBAO_API_KEY']},
                           open_timeout=10, close_timeout=2, max_size=2_000_000,
                           compression=None) as ws:
            await ws.send(json.dumps(configuration(instructions, [] if no_robot else [TOOL])))
            if (await receive(ws))['type'] != 'session.created': raise RuntimeError('no_session')
            try:
                class ShapeTrace:
                    async def send(self, text): return await ws.send(text)
                    async def recv(self):
                        raw = await ws.recv()
                        if isinstance(raw, str):
                            event = json.loads(raw)
                            if event.get('type') in ('conversation.item.input_audio_transcription.completed', 'response.output_text.done'):
                                def shape(value):
                                    if isinstance(value, dict): return {k: shape(v) for k,v in value.items()}
                                    if isinstance(value, list): return [shape(v) for v in value[:2]]
                                    if isinstance(value, str): return {'type': 'string', 'length': len(value)}
                                    return type(value).__name__
                                print('EVENT_SHAPE '+json.dumps(shape(event)), flush=True)
                        return raw
                if no_robot:
                    class QuietSink:
                        bytes_sent = 0
                        async def write(self, pcm): self.bytes_sent += len(pcm)
                        async def finish(self): pass
                        async def abort(self): pass
                    output = QuietSink()
                else:
                    output = BufferedRobotOutput(link, 20)
                async with asyncio.timeout(60):
                    await turn(ShapeTrace(), SavedInput(pcm), output, link,
                               actions=VoiceActions(client) if client else None, memory=memory, budget=budget)
                count = memory.db.execute('SELECT count(*) FROM voice_turns').fetchone()[0]
                if count == 0: raise RuntimeError('text_memory_not_written')
                print(json.dumps({'daily_cloud_probe': 'passed', 'memory_turns': count,
                                  'reported_tokens': budget.used, 'motion_enabled': False,
                                  'robot_opened': not no_robot}), flush=True)
            finally:
                with contextlib.suppress(Exception): await send(ws, 'session.close')
    finally:
        if output: await output.abort()
        if client: await client.close()
        await server.close(); memory.close()


if __name__ == '__main__':
    try:
        with asyncio.Runner(loop_factory=asyncio.SelectorEventLoop if os.name == 'nt' else None) as runner:
            runner.run(main())
    except Exception as error:
        code = str(error)
        safe = code if code.replace('_', '').isalnum() and len(code) < 100 else 'details_redacted'
        print('Daily probe stopped: ' + type(error).__name__ + '/' + safe, flush=True)
        raise SystemExit(1) from None
