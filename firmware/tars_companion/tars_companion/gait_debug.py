"""Local-only USB forward/left/right gait laboratory. No cloud imports."""
import argparse
import asyncio
import contextlib
import json
import secrets
import time
from datetime import datetime,timezone
from pathlib import Path
from .client import TarsClient
from .transport import UsbSerialTransport,memory_transport_pair
from .simulator import SimulatedTarsDevice
from .servo_calibration import LocalServer,WEB_LEASE

BASELINE=[[0,0,0,0],[0,45,45,0],[5,45,45,5],[5,-30,-30,5],[0,-30,-30,0],[0,0,0,0]]
GAITS={'walk_forward':BASELINE,
       'turn_left':[[0,0,0,0],[0,-30,-30,0],[40,-30,-30,-40],[40,20,20,-40],[0,20,20,0],[0,0,0,0]],
       'turn_right':[[0,0,0,0],[0,-30,-30,0],[-45,-30,-30,45],[-45,25,35,45],[0,25,35,0],[0,0,0,0]]}
EXTRA_LIMITS={'walk_forward':[[0,10],[-45,45],[-45,45],[0,10]],
              'turn_left':[[0,40],[-45,45],[-45,45],[-40,0]],
              'turn_right':[[-45,0],[-45,45],[-45,45],[0,45]]}
ACTION_LABELS={'walk_forward':'前进','turn_left':'左转','turn_right':'右转'}
def validate_drop_ramp(action,frames,enabled):
    if type(enabled) is not bool:raise ValueError('平滑下降开关必须为布尔值')
    if enabled and action!='walk_forward':raise ValueError('平滑下降仅适用于前进第4阶段')
    if enabled and len(frames)>=4 and any(frames[2][j]!=frames[3][j] for j in (0,3)):
        raise ValueError('平滑下降要求第3、4阶段的1号及4号角度分别相同，保证两侧不动')

STATIC=Path(__file__).with_name('gait_web')

def validate_turn_profile(action,frames,smooth,settle):
    if type(smooth) is not int or smooth not in (0,8,28,62):raise ValueError('转向轨迹只允许直接/A/B/C')
    if type(settle) is not int or settle not in (0,60):raise ValueError('落地保持只允许0或60ms')
    if action=='walk_forward' and (smooth or settle):raise ValueError('转向轨迹选项不用于前进')
    if settle and not smooth:raise ValueError('额外保持仅用于转向平滑第4阶段之后')
    if action!='walk_forward' and smooth:
        for f in range(2,min(5,len(frames))):
            axes=(0,3) if f==3 else (1,2)
            if any(frames[f][j]!=frames[f-1][j] for j in axes):
                raise ValueError(f'支撑顺序冲突：第{f+1}阶段须保持{axes[0]+1}/{axes[1]+1}号角度不变；需要独立修改可选直接轨迹')

def validate_plan(metadata,frames,selected):
    if not metadata or len(metadata.get('baseline',[]))!=6:
        raise ValueError('尚未读取固件步态和机械零点')
    if not isinstance(selected,list) or not selected or any(type(v) is not int for v in selected) or selected!=list(range(1,len(selected)+1)) or len(selected)>12:
        raise ValueError('只允许从第1阶段连续勾选到第N阶段，不允许跳阶段')
    if not isinstance(frames,list) or not 6<=len(frames)<=12 or len(selected)>len(frames):
        raise ValueError('必须提供6～12个阶段；不能选择不存在的阶段')
    for index,row in enumerate(frames):
        base=metadata['baseline'][index] if index<6 else [0]*4
        if not isinstance(row,list) or len(row)!=4:
            raise ValueError('每个阶段必须包含4路角度')
        for j,(value,limit) in enumerate(zip(row,base)):
            lower,upper=((-45,45) if metadata.get('free_angles') else
                         (min(0,limit),max(0,limit)) if index<6 else metadata.get('extra_limits',EXTRA_LIMITS['walk_forward'])[j])
            if type(value) is not int or not lower<=value<=upper:
                raise ValueError('角度超出当前固件允许的调试范围（新版每格−45°～+45°，整数）')
            physical=metadata['zeros'][j]+metadata['directions'][j]*value
            if not metadata['limits'][j][0]<=physical<=metadata['limits'][j][1]:
                raise ValueError('换算后的实际输出超出机械限位')
    return [row[:] for row in frames[:len(selected)]]


class GaitBridge:
    def __init__(self,mock=False):
        self.mock=mock;self.client=None;self.device=None;self.owner=None
        self.boot_id=None;self.generation=None;self.last_ping=0.
        self.lock=asyncio.Lock();self.metadata=None;self.job=None
        self.connection='未连接';self.status='输出关闭；未连接';self.records=[]
        self.diagnostic=None;self.active_command=None;self.error='';self.armed=False
        self.stop_generation=0
        self.action='walk_forward';self.supports_turns=False

    def connected(self):
        return bool(self.client and self.client.boot_id==self.boot_id and self.client._generation==self.generation)

    def snapshot(self):
        return {'mock':self.mock,'connection':self.connection,'connected':self.connected(),
                'action':self.action,'supports_turns':self.supports_turns,
                'metadata':self.metadata,'status':self.status,'armed':self.armed,
                'busy':self.job is not None and not self.job.done(),'records':self.records[-20:],
                'diagnostic':self.diagnostic,'error':self.error,'active_command':self.active_command,
                'actual_angle':None}

    async def request(self,name,payload=None,timeout=2,command_id=None):
        if not self.connected(): raise ConnectionError('会话变化，请手动重新连接；不会自动恢复动作')
        result=await self.client.command(name,payload or {},terminal_timeout=timeout,command_id=command_id)
        if not result.succeeded:
            raise ValueError(result.message.get('error',{}).get('code','设备拒绝命令'))
        return result

    async def diagnostic_read(self):
        data={}
        for view in ('boot','previous','current'):
            result=await self.request('diagnostic.get',{'view':view})
            data[view]=result.message.get('payload',{}).get('result',{})
        self.diagnostic=data
        return data

    def record(self,value):
        entry={'utc':datetime.now(timezone.utc).isoformat(),'mock':self.mock,'boot_id':self.boot_id,**value}
        self.records.append(entry)
        folder=Path(__file__).resolve().parents[1]/'evidence/gait-lab'
        folder.mkdir(parents=True,exist_ok=True)
        name=datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
        (folder/(name+'.json')).write_text(json.dumps(entry,ensure_ascii=False,indent=2),encoding='utf-8')

    async def stop(self):
        self.stop_generation+=1
        self.armed=False
        if self.connected():
            await self.request('safety.disarm')
        self.status='已请求关闭输出；没有回零动作'

    async def drop(self):
        client,self.client=self.client,None
        self.armed=False;self.metadata=None;self.active_command=None;self.supports_turns=False
        if self.job and not self.job.done():
            self.job.cancel();await asyncio.gather(self.job,return_exceptions=True)
        self.job=None
        if client: await client.close()
        if self.device: await self.device.close();self.device=None
        self.connection='未连接；不会自动重放';self.status='连接中断，输出以固件租约为准'

    async def watchdog(self):
        while True:
            await asyncio.sleep(.05)
            async with self.lock:
                if self.owner and time.monotonic()-self.last_ping>WEB_LEASE:
                    self.owner=None
                    with contextlib.suppress(Exception): await self.stop()
                    self.status='网页失活，已请求关闭输出；需重新确认'
                if self.client and not self.connected():
                    self.record({'event':'connection_changed','execution_confirmed':False})
                    await self.drop()

    async def load_plan(self,action):
        if action not in GAITS:raise ValueError('未知步态，只允许前进/左转/右转')
        if action!='walk_forward' and not self.supports_turns:
            raise ValueError('当前固件仅支持前进阶段调试；左右转需升级0.15.5-turnlab')
        geometry=(await self.request('motion.test_plan',{'view':'calibration','action':action})).message['payload']['result']
        plan=(await self.request('motion.test_plan',{'view':'frames','action':action})).message['payload']['result']
        metadata={**geometry,'baseline':plan['frames'],'phase_ms':plan['hold_ms'],
                  'max_phases':plan['max_phases'],'action':action}
        validate_plan(metadata,metadata['baseline'],[1])
        self.metadata=metadata;self.action=action

    async def execute(self,frames,owner,generation,action,zero=False,drop_ramp=False,smooth=0,settle=0):
        client=self.client;command_id=TarsClient.new_command_id()
        self.active_command=command_id
        def check():
            if generation!=self.stop_generation or owner!=self.owner or time.monotonic()-self.last_ping>WEB_LEASE or client is not self.client:
                raise ValueError('停止/租约/会话已变化，取消发送')
        try:
            check()
            await self.diagnostic_read()
            check()
            await self.request('safety.arm') # ONLY from the explicit confirmed run/zero button
            self.armed=True
            check()
            payload={'confirm':True,'frames':frames,'action':action}
            if drop_ramp:payload['drop_ramp']=True
            if smooth:payload['smooth']=smooth
            if settle:payload['settle']=settle
            result=await self.request('motion.test_run',payload,timeout=5,command_id=command_id)
            self.armed=False # firmware disables PWM after selected final phase, no neutral move
            await self.diagnostic_read()
            self.status='回机械零点命令完成，输出已关闭（实际位置未知）' if zero else f'已完成{ACTION_LABELS[action]}阶段1–{len(frames)}，输出已关闭；没有补做后续阶段'
            self.record({'event':'zero' if zero else 'test','action':action,'frames':frames,'phase_ms':300,'drop_ramp':drop_ramp,'smooth':smooth,'settle':settle,
                         'command_id':result.command_id,'completed':True,'diagnostics':self.diagnostic})
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.error=str(exc) if isinstance(exc,(ValueError,ConnectionError,TimeoutError)) else type(exc).__name__
            self.status='测试失败或结果未知；不会自动重试/回零'
            self.record({'event':'failed','action':action,'command_id':command_id,'frames':frames,'completed':False,'error':self.error})
        finally:
            self.armed=False
            if client is self.client and self.connected():
                with contextlib.suppress(Exception): await self.request('safety.disarm')
            self.active_command=None

    async def handle(self,action,data):
        async with self.lock:
            if action=='claim':
                if self.owner and time.monotonic()-self.last_ping<=WEB_LEASE:
                    raise ValueError('已有网页持有控制权')
                if self.owner: await self.stop()
                self.owner=secrets.token_hex(24);self.last_ping=time.monotonic()
                return {'owner':self.owner,**self.snapshot()}
            if not self.owner or not secrets.compare_digest(str(data.get('owner','')),self.owner):
                raise ValueError('请先获取网页控制权')
            if time.monotonic()-self.last_ping>WEB_LEASE:
                self.owner=None
                with contextlib.suppress(Exception): await self.stop()
                raise ValueError('网页心跳超时，需重新确认')
            if action=='ping':
                self.last_ping=time.monotonic()
                if self.connected() and self.active_command:
                    # Read trace only. No target update, Arm, calibration or motion replay.
                    result=await self.request('diagnostic.get',{'view':'current'})
                    if self.diagnostic is None:self.diagnostic={}
                    self.diagnostic['current']=result.message.get('payload',{}).get('result',{})
            elif action=='connect':
                if self.client: raise ValueError('请先断开旧连接')
                try:
                    if self.mock:
                        pc,peer=memory_transport_pair();pc.requires_session_id=True
                        self.device=SimulatedTarsDevice(peer,native_session_required=True,gait_testing=True)
                        await self.device.start();self.client=TarsClient(pc)
                    else:
                        serial=data.get('serial')
                        if not isinstance(serial,str) or not 1<=len(serial)<=128: raise ValueError('请选择原生USB序列号')
                        self.client=TarsClient(UsbSerialTransport(serial_number=serial))
                    await self.client.start();self.boot_id=self.client.boot_id;self.generation=self.client._generation
                    await self.request('safety.disarm')
                    version=(self.client.hello or {}).get('payload',{}).get('firmware_version')
                    if version not in {'0.15.3-gaitlab','0.15.4-drop30','0.15.5-turnlab','0.15.6-dropramp','0.15.7-freeangles','0.15.8-turnsmooth','0.15.9-voicegait'}:
                        raise ValueError('当前固件不支持阶段调试；需先烧录配套0.15.3或0.15.4版本。本工具不会自动烧录。')
                    self.supports_turns=version in {'0.15.5-turnlab','0.15.6-dropramp','0.15.7-freeangles','0.15.8-turnsmooth','0.15.9-voicegait'}
                    await self.load_plan('walk_forward')
                    await self.diagnostic_read()
                    self.error=''
                    self.connection='MOCK虚拟设备，未打开硬件' if self.mock else 'USB已连接，元数据来自当前固件'
                    self.status='已连接，输出关闭；需手动确认才能测试'
                    self.record({'event':'connected','metadata':self.metadata,'diagnostics':self.diagnostic})
                except Exception:
                    await self.drop();raise
            elif action=='select_action':
                if self.job and not self.job.done():raise ValueError('执行中不能切换步态')
                await self.load_plan(data.get('action'))
                self.error='';self.status=f'已选择{ACTION_LABELS[self.action]}，输出未使能'
            elif action in ('run','zero'):
                if data.get('confirm') is not True: raise ValueError('请确认手持/悬空、无卡滞且可立即断电')
                if not self.connected(): raise ValueError('请先连接兼容固件；当前机身需升级后才能实测')
                if self.job and not self.job.done(): raise ValueError('已有测试正在执行')
                if action=='zero': frames=[[0,0,0,0]]
                else: frames=validate_plan(self.metadata,data.get('frames'),data.get('selected'))
                ramp=data.get('drop_ramp',False) if action=='run' else False
                validate_drop_ramp(self.action,frames,ramp)
                if ramp and not self.metadata.get('drop_ramp'):raise ValueError('当前固件不支持平滑下降，需烧录0.15.6-dropramp；不会静默改为跳角')
                smooth=data.get('smooth',0) if action=='run' else 0
                settle=data.get('settle',0) if action=='run' else 0
                validate_turn_profile(self.action,frames,smooth,settle)
                if (smooth or settle) and not self.metadata.get('turn_smooth'):raise ValueError('转向平滑需0.15.8-turnsmooth；不会静默降级')
                self.error='';self.status='已接收手动确认，先使能回机械零位，再执行一次'
                self.job=asyncio.create_task(self.execute(frames,self.owner,self.stop_generation,self.action,zero=action=='zero',drop_ramp=ramp,smooth=smooth,settle=settle))
            elif action in ('off','release','disconnect'):
                await self.stop()
                if action=='release':self.owner=None
                if action=='disconnect':await self.drop()
            elif action=='estop':
                self.stop_generation+=1
                await self.request('safety.estop');self.armed=False;self.status='急停，输出关闭；不会自动解除'
            elif action=='clear':
                await self.request('safety.clear');self.status='已解除急停，未使能'
            elif action=='diag':await self.diagnostic_read()
            elif action=='export':return {'schema':'tars-gait-lab-1','metadata':self.metadata,'records':self.records}
            else:raise ValueError('未知操作')
            return self.snapshot()


async def run(args):
    bridge=GaitBridge(args.mock);server=LocalServer(bridge,args.port,static=STATIC)
    await server.start()
    print(f'本机步态阶段调试：{server.origin}/#{server.secret}',flush=True)
    print('MOCK：无硬件、无云API' if args.mock else 'USB工具；不自动连接或使能。请先关闭语音程序和串口采集。',flush=True)
    if args.open:
        import webbrowser
        webbrowser.open(f'{server.origin}/#{server.secret}')
    task=asyncio.create_task(bridge.watchdog())
    try:
        async with server.server: await server.server.serve_forever()
    finally:
        task.cancel();await asyncio.gather(task,return_exceptions=True)
        with contextlib.suppress(Exception):await bridge.stop()
        await bridge.drop()

def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--mock',action='store_true');p.add_argument('--port',type=int,default=8767)
    p.add_argument('--open',action='store_true',help='打开本机浏览器，不自动连接或使能')
    args=p.parse_args()
    if not 1024<=args.port<=65535:p.error('端口必须为1024..65535')
    try:
        with asyncio.Runner(loop_factory=asyncio.SelectorEventLoop if __import__('sys').platform=='win32' else None) as runner:
            runner.run(run(args))
    except KeyboardInterrupt:pass

if __name__=='__main__':main()
