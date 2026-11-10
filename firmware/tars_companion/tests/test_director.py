import asyncio
import base64
import json
from pathlib import Path
import tempfile
import types
import unittest
from unittest.mock import patch

from tars_companion.director import Director, DirectorHTTP, validate_plan
from tars_companion.director_speech import SpeechCache, wav_bytes


class FakeSpeech:
    def pcm(self,key):
        if key!='cached': raise ValueError('cache_missing')
        return b'\0\0'*160


class FakeClient:
    def __init__(self):
        self.hello={'payload':{'firmware_version':'0.18.0-director','director_api':1}}
        self.boot_id='boot';self._generation=1;self.commands=[];self.ticket=0;self.expression='idle'
        self.hold=False;self.started=asyncio.Event()
        self.mode='pixel'

    async def command(self,name,payload,**kwargs):
        assert kwargs['retries']==0
        self.commands.append((name,payload))
        result={}
        if name=='state.get': result={'motion':0,'armed':False,'health':0}
        if name=='display.expression':
            self.ticket+=1;self.expression=payload['name'];result={'ticket':self.ticket}
        if name=='display.release': self.ticket+=1;self.expression='idle';result={'ticket':self.ticket}
        if name=='display.mode': self.mode=payload['mode']
        if name=='display.status': result={'applied':self.ticket,'name':self.expression,'manual':True,'mode':self.mode}
        if name=='motion.execute':
            self.started.set()
            if self.hold: await asyncio.Event().wait()
        return types.SimpleNamespace(succeeded=True,message={'payload':{'result':result}})


class ValidationTests(unittest.TestCase):
    def plan(self,step,**kw): return validate_plan({'sequence':[step],**kw},FakeSpeech())

    def test_no_motion_without_checkbox(self):
        with self.assertRaises(ValueError): self.plan({'motion':'walk_forward'})

    def test_support_required(self):
        with self.assertRaises(ValueError): self.plan({'motion':'hello'},motion_ready=True)

    def test_support_confirmed(self):
        rows,_=self.plan({'motion':'hello'},motion_ready=True,support_ready=True)
        self.assertEqual(rows[0]['steps'],1)

    def test_gait_limits(self):
        for name,steps in [('walk_forward',6),('turn_left',4),('hello',2),('walk_forward',True)]:
            with self.subTest(name=name),self.assertRaises(ValueError):
                self.plan({'motion':name,'steps':steps},motion_ready=True,support_ready=True)

    def test_audio_motion_exclusive(self):
        with self.assertRaises(ValueError): self.plan({'motion':'walk_forward','audio_id':'cached'},motion_ready=True)

    def test_cache_must_exist(self):
        with self.assertRaises(ValueError): self.plan({'audio_id':'missing'})

    def test_invalid_steps(self):
        for row in [{'expression':'made_up'},{'motion':'support_probe'},{'wait_ms':-1},{'duration_ms':3600001,'expression':'happy'},{}]:
            with self.subTest(row=row),self.assertRaises(ValueError): self.plan(row)

    def test_reject_invalid_tail_before_any_motion(self):
        with self.assertRaises(ValueError): validate_plan({'sequence':[{'motion':'walk_forward'},{'audio_id':'missing'}],
            'motion_ready':True},FakeSpeech())

    def test_loop_zero_is_valid(self):
        self.assertEqual(self.plan({'expression':'happy'},loops=0)[1],0)

    def test_legacy_ui_step(self):
        self.assertEqual(self.plan({'display_mode':'legacy'})[0][0]['display_mode'],'legacy')
        with self.assertRaises(ValueError): self.plan({'display_mode':'legacy','expression':'happy'})


class ExecutionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.client=FakeClient()
        self.pool=types.SimpleNamespace(current=self.client)
        self.d=Director(self.pool,types.SimpleNamespace(current=None),speech=FakeSpeech())
        self.owner='a'*32

    async def asyncTearDown(self):
        await self.d.stop()

    async def run_plan(self,sequence,**kwargs):
        data=dict(owner=self.owner,request_id='b'*32,sequence=sequence,motion_ready=True,support_ready=True,timeline=False)
        data.update(kwargs)
        return await self.d.handle('run',data)

    async def test_display_finishes_before_next_motion(self):
        reached=asyncio.Event();release=asyncio.Event();durations=[]
        async def present(ms):
            durations.append(ms);reached.set();await release.wait()
        self.d.present=present
        await self.run_plan([{'display_mode':'legacy'},{'expression':'boot','wait_ms':0},{'motion':'walk_forward'}],timeline=True)
        await reached.wait()
        self.assertFalse(self.client.started.is_set())
        release.set();await self.d.job
        self.assertEqual(durations,[2000,4000])
        self.assertTrue(self.client.started.is_set())

    async def test_wait_for_terminal_then_next_motion(self):
        await self.run_plan([{'expression':'happy','motion':'walk_forward','steps':3},{'motion':'hello'}])
        await self.d.job
        operations=[n for n,_ in self.client.commands]
        first=operations.index('motion.execute')
        self.assertEqual(operations[first+1],'safety.disarm')
        self.assertEqual([p['name'] for n,p in self.client.commands if n=='motion.execute'],['walk_forward','hello'])

    async def test_cancel_does_not_execute_tail(self):
        self.client.hold=True
        await self.run_plan([{'motion':'walk_forward'},{'motion':'hello'}])
        await self.client.started.wait()
        await self.d.stop()
        self.assertEqual(sum(n=='motion.execute' for n,_ in self.client.commands),1)
        self.assertIn('audio.stop',[n for n,_ in self.client.commands])
        self.assertIn('safety.disarm',[n for n,_ in self.client.commands])

    async def test_reconnect_does_not_resume(self):
        await self.run_plan([{'wait_ms':200},{'motion':'walk_forward'}])
        await asyncio.sleep(.02)
        self.pool.current=FakeClient()
        await self.d.job
        self.assertFalse(any(n=='motion.execute' for n,_ in self.client.commands))
        self.assertTrue(self.d.error)

    async def test_generation_change_does_not_resume(self):
        await self.run_plan([{'wait_ms':200},{'motion':'walk_forward'}])
        await asyncio.sleep(.02);self.client._generation+=1
        await self.d.job
        self.assertFalse(any(n=='motion.execute' for n,_ in self.client.commands))

    async def test_duplicate_click_not_replayed(self):
        await self.run_plan([{'expression':'happy'}]);await self.d.job
        with self.assertRaises(ValueError): await self.run_plan([{'expression':'happy'}])

    async def test_second_tab_cannot_take_lease(self):
        await self.d.handle('ping',{'owner':self.owner})
        with self.assertRaises(ValueError): await self.d.handle('ping',{'owner':'c'*32})

    async def test_old_firmware_blocked(self):
        self.client.hello['payload']['firmware_version']='0.17.0-xiaozhi'
        self.client.hello['payload'].pop('director_api')
        with self.assertRaises(ValueError): await self.run_plan([{'expression':'happy'}])

    async def test_legacy_then_pixel_in_one_script(self):
        await self.run_plan([{'display_mode':'legacy'},{'expression':'happy'}]);await self.d.job
        self.assertEqual([p['mode'] for n,p in self.client.commands if n=='display.mode'],['legacy','pixel'])

    async def test_rejected_motion_aborts_tail(self):
        original=self.client.command
        async def rejected(name,payload,**kwargs):
            if name=='motion.execute': return types.SimpleNamespace(succeeded=False,terminal_type='command.rejected',message={'error':{'code':'audio_busy'}})
            return await original(name,payload,**kwargs)
        self.client.command=rejected
        await self.run_plan([{'motion':'walk_forward'},{'expression':'happy'}]);await self.d.job
        self.assertEqual(self.d.error,'audio_busy')
        self.assertFalse(any(n=='display.expression' for n,_ in self.client.commands))

    async def test_voice_mode_blocks_run(self):
        self.d.mode='voice'
        with self.assertRaises(ValueError): await self.run_plan([{'expression':'happy'}])

    async def test_expression_suppression_fails(self):
        original=self.client.command
        async def suppressed(name,payload,**kw):
            result=await original(name,payload,**kw)
            if name=='display.status': result.message['payload']['result']['name']='error'
            return result
        self.client.command=suppressed
        await self.run_plan([{'expression':'happy'},{'motion':'walk_forward'}]);await self.d.job
        self.assertTrue(self.d.error)
        self.assertFalse(any(n=='motion.execute' for n,_ in self.client.commands))

    async def test_page_lease_expiry_stops_job(self):
        self.client.hold=True
        await self.run_plan([{'motion':'walk_forward'}]);await self.client.started.wait()
        self.d.last_ping=0
        watcher=asyncio.create_task(self.d.monitor())
        await asyncio.sleep(1)
        watcher.cancel();await asyncio.gather(watcher,return_exceptions=True)
        self.assertIsNone(self.d.job)


class CacheTests(unittest.IsolatedAsyncioTestCase):
    async def test_same_line_cached_and_voices_separate(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache=SpeechCache(tmp);calls=[]
            async def synth(text): calls.append(text);return b'\0\0'*16000
            cache.windows=synth
            a=await cache.synthesize('hello','windows');b=await cache.synthesize('hello','windows')
            self.assertEqual(a['id'],b['id']);self.assertEqual(calls,['hello'])
            self.assertEqual(len(cache.pcm(a['id'])),32000)

    async def test_no_path_traversal(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ValueError): SpeechCache(tmp).pcm('../../secret')

    async def test_cloud_error_no_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache=SpeechCache(tmp)
            def fail(text): raise ValueError('no_permission')
            cache.cloud=fail
            with self.assertRaisesRegex(ValueError,'no_permission'): await cache.synthesize('hello','tars')
            self.assertEqual(list(Path(tmp).iterdir()),[])


class HTTPTests(unittest.IsolatedAsyncioTestCase):
    async def test_origin_and_token_required(self):
        d=Director(mock=True,speech=FakeSpeech());http=DirectorHTTP(d,0);await http.start()
        try:
            reader,writer=await asyncio.open_connection('127.0.0.1',http.port)
            writer.write((f'POST /api/stop HTTP/1.1\r\nHost: 127.0.0.1:{http.port}\r\nOrigin: https://evil.test\r\nContent-Type: application/json\r\nContent-Length: 2\r\n\r\n{{}}').encode());await writer.drain()
            result=await reader.read();writer.close();await writer.wait_closed()
            self.assertIn(b'403',result.split(b'\r\n')[0])
        finally: http.server.close();await http.server.wait_closed()


if __name__=='__main__': unittest.main()
