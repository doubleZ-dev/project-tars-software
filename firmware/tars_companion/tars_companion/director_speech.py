"""Literal text synthesis and content-addressed 16 kHz mono PCM cache.

No dialogue model, retries, or implicit fallback between voices.
Cloud contract: https://docs.volcengine.com/docs/doubao-voice/unidirectional-streaming-text-to-speech-http
"""
import asyncio
import base64
import hashlib
import io
import json
import os
from pathlib import Path
import urllib.request
import urllib.error
import uuid
import wave

MAX_PCM = 16000 * 2 * 120


def wav_bytes(pcm):
    out = io.BytesIO()
    with wave.open(out, 'wb') as f:
        f.setnchannels(1); f.setsampwidth(2); f.setframerate(16000); f.writeframes(pcm)
    return out.getvalue()


def read_wav(data):
    with wave.open(io.BytesIO(data), 'rb') as f:
        if (f.getnchannels(), f.getsampwidth(), f.getframerate()) != (1, 2, 16000):
            raise ValueError('需要 16 kHz、16 位、单声道 WAV')
        pcm = f.readframes(f.getnframes())
    if not pcm or len(pcm) > MAX_PCM: raise ValueError('台词音频需在 120 秒以内')
    return pcm


class SpeechCache:
    def __init__(self, directory='data/director/audio'):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.lock = asyncio.Lock()

    def options(self):
        return {'cloud_key': bool(os.environ.get('DOUBAO_API_KEY')),
                'speaker': os.environ.get('TARS_TTS_SPEAKER', 'S_vXjewQyk2'),
                'resource': os.environ.get('TARS_TTS_RESOURCE', 'seed-icl-2.0'),
                'model': os.environ.get('TARS_TTS_MODEL', 'seed-tts-2.0-standard')}

    async def synthesize(self, text, provider):
        if not isinstance(text, str) or not 1 <= len(text.strip()) <= 300:
            raise ValueError('请输入 1～300 字的台词')
        if provider not in {'windows', 'tars'}: raise ValueError('请选择台词音色')
        text = text.strip()
        config = self.options() if provider == 'tars' else {'voice': 'Windows-installed-zh-CN'}
        key = hashlib.sha256(json.dumps([text, provider, config], sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        path = self.directory/(key+'.wav')
        async with self.lock:
            if not path.exists():
                pcm = await (self.windows(text) if provider == 'windows' else asyncio.to_thread(self.cloud, text))
                if not pcm or len(pcm) > MAX_PCM or len(pcm) % 2: raise ValueError('语音数据为空、格式错误或超过 120 秒')
                tmp = path.with_suffix('.tmp')
                tmp.write_bytes(wav_bytes(pcm)); tmp.replace(path)
            data = path.read_bytes()
            pcm = read_wav(data)
        return {'id': key, 'provider': provider, 'text': text, 'seconds': round(len(pcm)/32000, 2),
                'wav': base64.b64encode(data).decode('ascii')}

    def pcm(self, key):
        if not isinstance(key, str) or len(key) != 64 or any(c not in '0123456789abcdef' for c in key):
            raise ValueError('台词缓存编号无效')
        path = self.directory/(key+'.wav')
        if not path.exists(): raise ValueError('台词尚未生成，请先生成并试听')
        return read_wav(path.read_bytes())

    async def windows(self, text):
        script = r'''
$ErrorActionPreference='Stop'
Add-Type -AssemblyName System.Speech
$value=[Text.Encoding]::UTF8.GetString([Convert]::FromBase64String([Console]::In.ReadToEnd().Trim()))
$s=New-Object System.Speech.Synthesis.SpeechSynthesizer
$voices=@($s.GetInstalledVoices() | Where-Object { $_.Enabled -and $_.VoiceInfo.Culture.Name -eq 'zh-CN' })
if ($voices.Count -eq 0) { throw 'Chinese speech voice not installed' }
$s.SelectVoice($voices[0].VoiceInfo.Name)
$stream=New-Object IO.MemoryStream
$format=New-Object System.Speech.AudioFormat.SpeechAudioFormatInfo(16000,[System.Speech.AudioFormat.AudioBitsPerSample]::Sixteen,[System.Speech.AudioFormat.AudioChannel]::Mono)
$s.SetOutputToAudioStream($stream,$format)
$s.Speak($value)
[Console]::Out.Write([Convert]::ToBase64String($stream.ToArray()))
$s.Dispose();$stream.Dispose()
'''
        proc = await asyncio.create_subprocess_exec('powershell.exe', '-NoProfile', '-NonInteractive', '-Command', script,
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            creationflags=0x08000000 if os.name == 'nt' else 0)
        try:
            out, _ = await asyncio.wait_for(proc.communicate(base64.b64encode(text.encode('utf-8'))), 90)
        except BaseException:
            proc.kill(); await proc.wait(); raise
        if proc.returncode: raise ValueError('本机中文语音不可用，请在 Windows 设置中安装中文语音包')
        # SetOutputToAudioStream writes raw PCM (no RIFF header).
        return base64.b64decode(out.strip(), validate=True)

    def cloud(self, text):
        key = os.environ.get('DOUBAO_API_KEY')
        if not key: raise ValueError('未配置豆包 API Key；请运行设置脚本，密钥不要填在网页或聊天中')
        cfg = self.options()
        body = {'req_params': {'text': text, 'speaker': cfg['speaker'], 'model': cfg['model'],
            'audio_params': {'format': 'pcm', 'sample_rate': 16000}}}
        request = urllib.request.Request('https://openspeech.bytedance.com/api/v3/tts/unidirectional',
            data=json.dumps(body).encode(), headers={'Content-Type': 'application/json', 'X-Api-Key': key,
            'X-Api-Resource-Id': cfg['resource'], 'X-Api-Request-Id': str(uuid.uuid4())})
        data = bytearray()
        try:
            with urllib.request.urlopen(request, timeout=45) as response:
                while True:
                    line = response.readline(2_000_000)
                    if not line: break
                    if not line.strip(): continue
                    item = json.loads(line)
                    if item.get('code') not in (0, 20000000):
                        raise ValueError('Tars 合成被拒绝（代码 %s），请核对音色版本、资源权限和额度' % item.get('code'))
                    if item.get('data'): data.extend(base64.b64decode(item['data'], validate=True))
                    if len(data) > MAX_PCM: raise ValueError('台词超过 120 秒，请拆成多段')
        except urllib.error.HTTPError as e:
            raise ValueError('Tars 合成 HTTP %s：请核对 API Key、音色资源权限及额度' % e.code) from None
        except (urllib.error.URLError, TimeoutError):
            raise ValueError('Tars 合成连接失败或超时，请检查网络；未自动重试') from None
        return bytes(data)
