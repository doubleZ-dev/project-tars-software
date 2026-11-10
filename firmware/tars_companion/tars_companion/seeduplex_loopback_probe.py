"""Two real-cloud turns using a saved synthesized input, not a live microphone."""
import asyncio
import contextlib
import json
import os
import wave
from websockets.asyncio.client import connect
from .seeduplex import URL, configuration, receive, send, turn, BufferedRobotOutput
from .wifi_audio import LanAudioServer, load_profile, NetworkJsonTransport
from .robot_runtime import RuntimeClient


class SavedInput:
    def __init__(self, pcm): self.pcm = pcm
    async def start_capture(self):
        self.offset = 0
        self.deadline = asyncio.get_running_loop().time()
    async def stop_capture(self): pass
    async def read_frame(self):
        self.deadline += .1
        await asyncio.sleep(max(0, self.deadline-asyncio.get_running_loop().time()))
        frame = self.pcm[self.offset:self.offset+3200]
        self.offset += 3200
        return frame.ljust(3200, b'\0')


async def main():
    with wave.open('evidence/tars-probe-body-16k.wav', 'rb') as wav:
        pcm = wav.readframes(wav.getnframes())
    profile = load_profile('wifi-audio.local.json')
    server = LanAudioServer(profile['token']); client = output = None
    try:
        await server.start(profile['host'], profile['port'])
        link = await server.wait_connected(20)
        client = RuntimeClient(NetworkJsonTransport(link), allow_motion=False)
        await client.start(); await client.disarm()
        async with connect(URL, additional_headers={'X-Api-Key': os.environ['DOUBAO_API_KEY']},
                           open_timeout=10, close_timeout=2, max_size=2_000_000,
                           compression=None) as ws:
            await ws.send(json.dumps(configuration('You are TARS. Answer in English, one short sentence.')))
            if (await receive(ws))['type'] != 'session.created': raise RuntimeError('no_session')
            try:
                for index in range(2):
                    print(f'AUTOMATIC PROBE {index+1}/2: saved synthetic input, no live microphone.', flush=True)
                    output = BufferedRobotOutput(link, 20)
                    async with asyncio.timeout(60):
                        await turn(ws, SavedInput(pcm), output, link)
                    await asyncio.sleep(.5)
            finally:
                with contextlib.suppress(Exception): await send(ws, 'session.close')
    finally:
        if output: await output.abort()
        if client: await client.close()
        await server.close()


if __name__ == '__main__':
    try:
        with asyncio.Runner(loop_factory=asyncio.SelectorEventLoop if os.name == 'nt' else None) as runner:
            runner.run(main())
    except Exception as error:
        code = str(error)
        safe = code if code.replace('_', '').isalnum() and len(code) < 100 else 'details_redacted'
        print('Automatic probe stopped: ' + type(error).__name__ + '/' + safe, flush=True)
        raise SystemExit(1) from None
