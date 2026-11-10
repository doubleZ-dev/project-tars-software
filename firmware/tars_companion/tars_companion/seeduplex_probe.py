"""Capture only synthesized probe speech; never records the user's microphone."""
import asyncio
import base64
import contextlib
import json
import math
import os
from pathlib import Path
import wave

from .seeduplex import URL, configuration, receive, send
from .doubao import StreamingResampler
from .audio_levels import samples


def save_wav(path, pcm, rate):
    with wave.open(str(path), 'wb') as out:
        out.setnchannels(1); out.setsampwidth(2); out.setframerate(rate)
        out.writeframes(pcm)


def stats(pcm, rate):
    values = samples(pcm)
    return {'seconds': round(len(values)/rate, 3),
            'peak': max(map(abs, values), default=0),
            'rms': round(math.sqrt(sum(v*v for v in values)/max(1, len(values)))),
            'near_full_scale_pct': round(100*sum(abs(v)>=32000 for v in values)/max(1, len(values)), 3)}


async def main():
    from websockets.asyncio.client import connect
    key = os.environ.get('DOUBAO_API_KEY', '').strip()
    if not key: raise ValueError('DOUBAO_API_KEY_required')
    pcm = bytearray()
    async with connect(URL, additional_headers={'X-Api-Key': key}, open_timeout=10,
                       close_timeout=2, max_size=2_000_000, compression=None) as ws:
        try:
            await ws.send(json.dumps(configuration('You are TARS. Speak clearly in English.')))
            if (await receive(ws))['type'] != 'session.created':
                raise RuntimeError('session_not_created')
            await send(ws, 'input_audio_mute.commit')
            await send(ws, 'speech_text_buffer.commit',
                       text='TARS online. All systems are ready. Can you hear me clearly?')
            async with asyncio.timeout(40):
                while True:
                    event = await receive(ws)
                    if event['type'] == 'response.output_audio.delta':
                        pcm.extend(base64.b64decode(event['delta'], validate=True))
                        if len(pcm) > 960000: raise ValueError('probe_audio_too_long')
                    elif event['type'] in ('response.output_audio.done', 'response.done'):
                        break
        finally:
            with contextlib.suppress(Exception): await send(ws, 'session.close')
    if not pcm or len(pcm) % 2: raise ValueError('invalid_probe_pcm')
    directory = Path('evidence'); directory.mkdir(exist_ok=True)
    save_wav(directory/'tars-probe-cloud-24k.wav', bytes(pcm), 24000)
    converter = StreamingResampler()
    body = converter.feed(bytes(pcm), final=True)
    save_wav(directory/'tars-probe-body-16k.wav', body, 16000)
    result = {'source': stats(pcm, 24000), 'converted': stats(body, 16000),
              'gain': 1, 'cloud_loudness': 0, 'microphone_recorded': False}
    (directory/'tars-probe-stats.json').write_text(json.dumps(result, indent=2), encoding='utf-8')
    print(json.dumps(result), flush=True)


if __name__ == '__main__':
    try:
        with asyncio.Runner(loop_factory=asyncio.SelectorEventLoop if os.name == 'nt' else None) as runner:
            runner.run(main())
    except Exception as error:
        print('Probe failed: ' + type(error).__name__, flush=True)
        raise SystemExit(1) from None
