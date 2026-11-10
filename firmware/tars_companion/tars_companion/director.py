"""Local filming console. One owner for paired control and Opus audio; no USB."""
import argparse
import asyncio
import contextlib
import json
import os
from pathlib import Path
import secrets
import time

from .director_speech import SpeechCache
from .motionpack_voice import ACTIONS, GAITS, CATALOG
from .xiaozhi_control import ControlPool
from .xiaozhi_audio import XiaozhiServer, XiaozhiOutput
from .wifi_audio import load_profile

STATIC = Path(__file__).with_name('director_web')
EXPRESSIONS = json.loads(Path(__file__).with_name('expression_catalog.json').read_text(encoding='utf-8'))
EXPRESSION_NAMES = {e['name'] for e in EXPRESSIONS}
EXPRESSION_DURATIONS = {e['name']: e['duration_ms'] for e in EXPRESSIONS}


def integer(value, low, high, label):
    if type(value) is not int or not low <= value <= high: raise ValueError(label)
    return value


def validate_plan(data, speech):
    steps = data.get('sequence')
    if not isinstance(steps, list) or not 1 <= len(steps) <= 100: raise ValueError('脚本需要 1～100 个步骤')
    loops = integer(data.get('loops', 1), 0, 999, '循环次数为 1～999；0 表示持续循环')
    result = []
    for row in steps:
        if not isinstance(row, dict): raise ValueError('步骤格式错误')
        expression = row.get('expression', '')
        display_mode = row.get('display_mode', '')
        if display_mode not in {'','pixel','legacy'}: raise ValueError('显示模式无效')
        if display_mode == 'legacy' and expression: raise ValueError('原版 UI 步骤不应同时指定像素表情')
        if expression not in EXPRESSION_NAMES | {'', 'auto'}: raise ValueError('未知表情')
        duration = integer(row.get('duration_ms', 0), 0, 3600000, '表情保持时间超出范围')
        motion = row.get('motion', '')
        if motion not in set(ACTIONS) | GAITS | {''}: raise ValueError('未知动作或准备测试动作不可用于拍摄')
        count = integer(row.get('steps', 1), 1, 5 if motion == 'walk_forward' else 3 if motion in GAITS else 1, '动作次数超出范围')
        audio_id = row.get('audio_id', '')
        text = row.get('text', '')
        if not isinstance(text,str) or len(text)>300: raise ValueError('台词文字超过 300 字')
        if motion and audio_id: raise ValueError('身体动作和台词请放在两个步骤，按顺序执行')
        if audio_id: speech.pcm(audio_id)
        wait = integer(row.get('wait_ms', 0), 0, 60000, '等待时间为 0～60 秒')
        pure_display = bool(display_mode or expression) and not motion and not audio_id
        display_ms = 0
        if pure_display and data.get('timeline', True):
            default = duration or EXPRESSION_DURATIONS.get(expression, 0) or 2000
            display_ms = integer(row.get('display_ms', default), 200, 3600000, '展示时间为 0.2～3600 秒')
        if not (display_mode or expression or motion or audio_id or wait): raise ValueError('步骤不能为空')
        if motion and data.get('motion_ready') is not True: raise ValueError('请勾选动作准备确认')
        if ACTIONS.get(motion, {}).get('requires_support') and data.get('support_ready') is not True:
            raise ValueError('所选动作需要确认机构支撑条件')
        result.append(dict(display_mode=display_mode, expression=expression, duration_ms=duration, motion=motion, steps=count,
                           audio_id=audio_id, text=text, wait_ms=wait, display_ms=display_ms))
    return result, loops


class Director:
    def __init__(self, pool=None, audio=None, *, mock=False, speech=None):
        self.pool, self.audio, self.mock = pool, audio, mock
        self.speech = speech or SpeechCache()
        self.mode = 'director'
        self.job = None
        self.voice_task = None
        self.voice_factory = None
        self.voice_available = bool(os.environ.get('DOUBAO_API_KEY'))
        self.last_ping = 0
        self.owner = None
        self.owner_lock = asyncio.Lock()
        self.stage = '等待连接' if not mock else '模拟模式'
        self.error = ''
        self.progress = {'step': 0, 'total': 0, 'loop': 0}
        self.events = []
        self.display = {}
        self.device = {}
        self.active_client = None
        self.active_audio = None
        self.last_client = None
        self.request_ids = set()
        self.stopping = False
        self.stop_lock = asyncio.Lock()

    def log(self, text):
        self.events.append({'time': time.strftime('%H:%M:%S'), 'text': text})
        self.events = self.events[-60:]

    def snapshot(self):
        client = self.pool.current if self.pool else None
        link = self.audio.current if self.audio else None
        version = client.hello['payload']['firmware_version'] if client else None
        return dict(mock=self.mock, mode=self.mode, connected=bool(client) or self.mock,
            audio_connected=bool(link and not link.closed.is_set()) or self.mock,
            firmware=version, compatible=self.mock or bool(client and client.hello['payload'].get('director_api') == 1),
            busy=bool(self.job and not self.job.done()), stage=self.stage, error=self.error,
            progress=self.progress, events=self.events, display=self.display, device=self.device,
            voice_available=self.voice_available)

    async def command(self, client, name, payload=None):
        result = await client.command(name, payload or {}, retries=0, ack_timeout=2, terminal_timeout=40)
        if not result.succeeded:
            raise RuntimeError(result.message.get('error', {}).get('code', result.terminal_type))
        return result.message.get('payload', {}).get('result', {})

    async def expression(self, client, name, duration=0):
        if self.mock:
            self.display = {**self.display, 'name': name, 'manual': name != 'auto'}; return
        result = await self.command(client, 'display.release' if name == 'auto' else 'display.expression',
                                    {} if name == 'auto' else {'name': name, 'duration_ms': duration})
        async with asyncio.timeout(6):
            while True:
                status = await self.command(client, 'display.status')
                self.display = status
                if status['applied'] >= result['ticket']:
                    if name != 'auto' and status['name'] != name:
                        if status['name'] == 'boot': await asyncio.sleep(.1); continue
                        raise RuntimeError('表情被设备安全状态覆盖：'+status['name'])
                    return
                await asyncio.sleep(.06)

    async def display_mode(self, client, mode):
        if self.mock: self.display['mode']=mode;return
        await self.command(client,'display.mode',{'mode':mode})
        async with asyncio.timeout(3):
            while True:
                self.display=await self.command(client,'display.status')
                if self.display.get('mode')==mode: return
                await asyncio.sleep(.06)

    def require_owner(self, data):
        owner = data.get('owner')
        if not isinstance(owner, str) or len(owner) != 32: raise ValueError('页面会话失效，请刷新页面')
        now = time.monotonic()
        if self.owner != owner:
            if self.owner and now-self.last_ping < 8: raise ValueError('另一张导演台页面正在控制，请先关闭它')
            if self.job and not self.job.done(): raise ValueError('旧页面任务尚未停止，请稍候')
            self.owner = owner
        self.last_ping = now

    async def handle(self, action, data):
        if action == 'status': return self.snapshot()
        if action == 'catalog':
            return {'expressions': EXPRESSIONS, 'actions': [{'name': n, 'label': l, 'requires_support': False}
                for n,l in [('walk_forward','前进'),('turn_left','左转'),('turn_right','右转')]] + list(ACTIONS.values()),
                'speech': self.speech.options(), 'snapshot': self.snapshot()}
        # Stop is deliberately available even if the owning tab lost its lease.
        if action == 'stop':
            await self.stop(); return self.snapshot()
        async with self.owner_lock:
            self.require_owner(data)
            if action == 'ping': return self.snapshot()
            if action == 'synthesize':
                pass  # no hardware operation, do not hold ownership lock during synthesis
            elif action == 'run':
                if self.stopping: raise ValueError('正在停止，请稍候')
                if self.mode != 'director': raise ValueError('请先切换到拍摄模式')
                if self.job and not self.job.done(): raise ValueError('已有脚本执行中，请先停止')
                plan, loops = validate_plan(data, self.speech)
                if not self.snapshot()['compatible']: raise ValueError('请先安装 0.18.0-director 固件并等待无线连接')
                request = data.get('request_id')
                if not isinstance(request, str) or len(request) != 32: raise ValueError('请求编号无效')
                if request in self.request_ids: raise ValueError('这次点击已处理；需要重拍时请重新点击')
                if len(self.request_ids) > 10000: raise ValueError('请重启导演台服务以清理会话记录')
                self.request_ids.add(request)
                client = self.pool.current if self.pool else None
                link = self.audio.current if self.audio else None
                if any(s['audio_id'] for s in plan) and not self.snapshot()['audio_connected']:
                    raise ValueError('机器人音频连接尚未就绪')
                self.active_client, self.active_audio = client, link
                self.last_client = client
                self.error=''; self.stage='开始执行'
                self.job=asyncio.create_task(self.execute(plan,loops,client,link,data.get('support_ready') is True))
                return self.snapshot()
            elif action == 'mode':
                mode = data.get('mode')
                if mode not in {'director','voice'}: raise ValueError('模式无效')
                if mode == 'voice' and (not self.voice_factory or not self.voice_available):
                    raise ValueError('日常语音配置尚未就绪，请使用设置脚本补齐配置')
                await self.stop()
                self.mode = mode
                if mode == 'voice': self.voice_task=asyncio.create_task(self.voice_factory())
                self.stage='日常语音监听' if mode == 'voice' else '拍摄模式就绪'
                self.log(self.stage)
                return self.snapshot()
            elif action == 'clear':
                if self.job and not self.job.done(): raise ValueError('请先停止任务')
                if not self.mock:
                    if not self.pool.current: raise ValueError('设备未连接')
                    await self.command(self.pool.current,'safety.clear')
                self.error=''; return self.snapshot()
            else: raise ValueError('未知操作')
        return await self.speech.synthesize(data.get('text'),data.get('provider'))

    async def execute(self, plan, loops, client, link, support):
        boot = client.boot_id if client else None
        generation = client._generation if client else None
        try:
            cycle = 0
            while not loops or cycle < loops:
                cycle += 1
                for i, step in enumerate(plan,1):
                    if not self.mock and (self.pool.current is not client or client.boot_id != boot or client._generation != generation):
                        raise ConnectionError('控制连接变化，脚本已终止，未重放')
                    if step['audio_id'] and not self.mock and (self.audio.current is not link or link.closed.is_set()):
                        raise ConnectionError('音频连接变化，脚本已终止，未重放')
                    self.progress={'step':i,'total':len(plan),'loop':cycle}
                    self.stage=f'第 {cycle} 轮 · 步骤 {i}/{len(plan)}'
                    self.log(self.stage)
                    if step['display_mode']: await self.display_mode(client,step['display_mode'])
                    elif step['expression'] and step['expression']!='auto': await self.display_mode(client,'pixel')
                    # A timeline owns the display through its presentation and gap;
                    # do not let a firmware timeout restore idle halfway through it.
                    if step['expression']: await self.expression(client,step['expression'],0 if step['display_ms'] else step['duration_ms'])
                    if step['motion']:
                        if self.mock: await asyncio.sleep(.15)
                        else:
                            state=await self.command(client,'state.get')
                            if state.get('motion') != 0 or state.get('armed') or state.get('health') not in (0,1):
                                raise RuntimeError('机器人当前不适合开始动作，请检查状态')
                            if link and link.mode != 'idle': raise RuntimeError('音频尚未空闲')
                            try:
                                await self.command(client,'safety.arm')
                                await self.command(client,'motion.execute',dict(name=step['motion'],steps=step['steps'],
                                    speed_pct=100,support_confirm=support))
                            finally:
                                await self.command(client,'safety.disarm')
                    if step['audio_id']:
                        pcm=self.speech.pcm(step['audio_id'])
                        if self.mock: await asyncio.sleep(.15)
                        else:
                            await self.command(client,'safety.disarm')
                            # Literal script text may also appear in the original dialogue UI.
                            if step.get('text'): await link.display_dialogue('tars',step['text'])
                            await link.interaction('SPEAKING')
                            output=XiaozhiOutput(link,20);output.buffer.extend(pcm)
                            await output.finish()
                            await link.interaction('IDLE')
                    if step['display_ms']:
                        self.log('显示已应用，展示 %.1f 秒'% (step['display_ms']/1000))
                        await self.present(step['display_ms'])
                    await asyncio.sleep(max(.1,step['wait_ms']/1000))
            self.stage='执行完成，可以重拍';self.log(self.stage)
        except asyncio.CancelledError:
            self.stage='已停止';self.log('脚本已取消，未执行的步骤已清空');raise
        except Exception as exc:
            self.error=str(exc);self.stage='执行中止';self.log('执行中止：'+self.error)
            await self.halt_hardware(client,link)
        finally:
            self.active_client=self.active_audio=None

    async def present(self, milliseconds):
        await asyncio.sleep(milliseconds/1000)

    async def halt_hardware(self, client=None, link=None):
        if self.mock: return
        client=client or (self.pool.current if self.pool else None)
        link=link or (self.audio.current if self.audio else None)
        async def safe_commands():
            if client:
                for name in ('audio.stop','safety.disarm','display.release'):
                    with contextlib.suppress(Exception):
                        await asyncio.wait_for(self.command(client,name),3)
        async def close_audio():
            if link and link.mode != 'idle': await link.close()
        await asyncio.gather(safe_commands(),close_audio(),return_exceptions=True)

    async def stop(self):
        async with self.stop_lock:
            self.stopping=True
            try: await self._stop()
            finally: self.stopping=False

    async def _stop(self):
        self.mode='director'
        pending=[t for t in (self.job,self.voice_task) if t and not t.done()]
        # Abort output independently before waiting for the job coroutine's cleanup.
        halt=asyncio.create_task(self.halt_hardware(self.active_client,self.active_audio))
        for task in pending: task.cancel()
        await asyncio.gather(*pending,return_exceptions=True)
        await halt
        self.stage='已停止';self.job=self.voice_task=None

    async def monitor(self):
        while True:
            await asyncio.sleep(.8)
            if self.mode == 'director' and self.owner and time.monotonic()-self.last_ping>8:
                await self.stop();self.owner=None;self.log('页面心跳超时，已停止并释放表情')
            if self.mock: continue
            client=self.pool.current
            if client is not self.last_client:
                if self.job and not self.job.done(): await self.stop()
                self.last_client=client;self.device={};self.display={}
                self.log('无线控制已连接' if client else '无线控制断开；等待重连，不恢复旧脚本')
                if self.mode=='director' and not (self.job and not self.job.done()):
                    self.stage='拍摄模式就绪' if client else '等待无线重连'
            if client:
                try:
                    self.device=await self.command(client,'state.get')
                    if self.snapshot()['compatible']: self.display=await self.command(client,'display.status')
                except Exception:
                    self.device={}


class DirectorHTTP:
    def __init__(self, director, port):
        self.director,self.port=director,port
        self.secret=secrets.token_hex(32)

    async def start(self):
        self.server=await asyncio.start_server(self.serve,'127.0.0.1',self.port,limit=16384)
        self.port=self.server.sockets[0].getsockname()[1]
        self.origin=f'http://127.0.0.1:{self.port}'

    async def serve(self,reader,writer):
        status=200;mime='application/json; charset=utf-8';content=b''
        try:
            head=await asyncio.wait_for(reader.readuntil(b'\r\n\r\n'),3)
            lines=head.decode('ascii').split('\r\n');method,path,_=lines[0].split(' ')
            headers={}
            for line in lines[1:]:
                if not line: continue
                key,value=line.split(':',1);key=key.lower()
                if key in headers: raise ValueError('重复请求头')
                headers[key]=value.strip()
            if headers.get('host') != f'127.0.0.1:{self.port}' or headers.get('sec-fetch-site','same-origin') not in {'same-origin','none'}:
                raise PermissionError('仅允许本机同源访问')
            if 'transfer-encoding' in headers: raise ValueError('不支持分块请求')
            assets={'/':('index.html','text/html'),'/app.js':('app.js','text/javascript'),'/style.css':('style.css','text/css')}
            if method=='GET' and path in assets:
                file,mime=assets[path];mime+='; charset=utf-8';content=(STATIC/file).read_bytes()
            elif method=='GET' and path=='/favicon.ico': status=204
            elif method=='POST' and path.startswith('/api/'):
                if headers.get('origin')!=self.origin or not secrets.compare_digest(headers.get('x-tars-token',''),self.secret):
                    raise PermissionError('页面会话失效，请用启动器重新打开导演台')
                if headers.get('content-type')!='application/json': raise ValueError('需要 JSON')
                length=integer(int(headers.get('content-length','0')),1,65536,'请求过大')
                data=json.loads(await asyncio.wait_for(reader.readexactly(length),3))
                if not isinstance(data,dict): raise ValueError('需要 JSON 对象')
                result=await self.director.handle(path[5:],data)
                content=json.dumps(result,ensure_ascii=False).encode()
            else: status=404;content=b'{"error":"not_found"}'
        except PermissionError as exc: status=403;content=json.dumps({'error':str(exc)},ensure_ascii=False).encode()
        except Exception as exc: status=400;content=json.dumps({'error':str(exc)},ensure_ascii=False).encode()
        try:
            writer.write((f'HTTP/1.1 {status} Response\r\nContent-Type: {mime}\r\nContent-Length: {len(content)}\r\n'
                "Connection: close\r\nCache-Control: no-store\r\nX-Content-Type-Options: nosniff\r\n"
                "Content-Security-Policy: default-src 'self'; media-src 'self' blob:; frame-ancestors 'none'; base-uri 'none'; form-action 'none'\r\n"
                'Referrer-Policy: no-referrer\r\n\r\n').encode()+content)
            await writer.drain()
        except ConnectionError: pass
        finally:
            writer.close()
            with contextlib.suppress(ConnectionError): await writer.wait_closed()


async def run(args):
    pool=audio=None
    if not args.mock:
        profile=load_profile(args.config)
        pool=ControlPool(profile,True)
        audio=XiaozhiServer(profile['token'])
    director=Director(pool,audio,mock=args.mock)
    if not args.mock:
        from .director_voice import converse
        director.voice_factory=lambda:converse(director)
    http=DirectorHTTP(director,args.port)
    await http.start()
    if pool: await pool.start()
    if audio: await audio.start(profile['host'])
    url=f'{http.origin}/#{http.secret}'
    Path('data/director').mkdir(parents=True,exist_ok=True)
    Path('data/director/'+('mock-url.local.txt' if args.mock else 'open-url.local.txt')).write_text(url,encoding='utf-8')
    print('Director ready at '+http.origin+' (use Start-Tars-Director to open authenticated page)',flush=True)
    watcher=asyncio.create_task(director.monitor())
    stop=Path('data/director/mock-stop.request' if args.mock else 'data/stop.request');stop.unlink(missing_ok=True)
    try:
        while not stop.exists(): await asyncio.sleep(.5)
        stop.unlink(missing_ok=True)
    finally:
        watcher.cancel();await asyncio.gather(watcher,return_exceptions=True)
        await director.stop()
        http.server.close();await http.server.wait_closed()
        if pool: await pool.close()
        if audio: await audio.close()


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--mock',action='store_true')
    parser.add_argument('--port',type=int,default=8870)
    parser.add_argument('--config',default='wifi-audio.local.json')
    args=parser.parse_args()
    lock=None
    try:
        if not args.mock and os.name=='nt':
            import msvcrt
            Path('data').mkdir(exist_ok=True)
            lock=open('data/service.lock','a+b');lock.seek(0);lock.write(b'1');lock.flush();lock.seek(0)
            msvcrt.locking(lock.fileno(),msvcrt.LK_NBLCK,1)
        asyncio.run(run(args))
    except KeyboardInterrupt: pass
    finally:
        if lock: lock.close()


if __name__=='__main__': main()
