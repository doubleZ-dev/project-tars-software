"""Explicit one-call TTS diagnosis, no playback, ASR, brain, robot or retries."""
import argparse,asyncio,json
from pathlib import Path
from .speech import create_dashscope_synthesizer,speech_error_details

async def run():
    tts=create_dashscope_synthesizer()
    report={'adapter':type(tts).__name__,'model':tts.model,'cloud_calls':1,'playback':False,'motion_sent':False}
    try:
        pcm,fmt=await tts.synthesize('你好')
        report.update(ok=True,pcm_bytes=len(pcm),sample_rate=fmt.sample_rate)
    except Exception:
        report.update(ok=False,**speech_error_details(tts))
    folder=Path(__file__).resolve().parents[1]/'evidence/voice-diagnostics'
    folder.mkdir(parents=True,exist_ok=True)
    (folder/'tts-one-call.json').write_text(json.dumps(report,indent=2),encoding='utf-8')
    print(json.dumps(report))

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--live-once',action='store_true',required=True,help='explicit permission for one possibly billed request')
    p.parse_args();asyncio.run(run())
