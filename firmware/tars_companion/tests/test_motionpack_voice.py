import asyncio
import base64
import json
from pathlib import Path
import tempfile
import unittest
from datetime import datetime, timedelta

from tars_companion.motionpack_voice import MotionpackActions, intent_for, ACTIONS, GAITS
from tars_companion.local_listening import LocalListeningMicrophone
from tars_companion.allowance import Allowance
from tars_companion.voice_memory import VoiceMemory
from tars_companion.voice_service import instructions, verify_firmware
from tars_companion.seeduplex import turn, upload_audio, end_conversation
from test_voice_daily import result
from test_seeduplex import Mic, Sink, WS, Link


class Robot:
    def __init__(self, events): self.events = events; self.armed = False; self.fail = False
    async def state(self):
        return result(snapshot={'lifecycle':1,'motion':0,'health':0,'connected':True,
                                'armed':self.armed,'active_command_id':''})
    async def arm(self): self.events.append('arm'); self.armed=True; return result()
    async def disarm(self): self.events.append('disarm'); self.armed=False; return result()
    async def execute(self, name, **kwargs):
        self.events.append(('execute', name, kwargs))
        if self.fail: raise TimeoutError('uncertain')
        return result()


class Body(Link):
    mode = 'idle'
    async def close(self): self.mode = 'closed'


def request(name='walk_forward', identity='one'):
    return {'call_id':identity,'name':'robot_command','arguments':{'command':name}}


class MotionTests(unittest.IsolatedAsyncioTestCase):
    async def test_source_wait_is_not_upload_backlog(self):
        class SlowSource:
            prepared=True
            async def read_frame(self):
                await asyncio.sleep(.12)
                return bytes(3200)
        class FastSocket:
            async def send(self,data): pass
        # No network at all. Previously the source's cumulative drift raised
        # audio_upload_fell_behind before this ordinary request deadline.
        with self.assertRaises(TimeoutError):
            await upload_audio(FastSocket(),SlowSource(),duration=4)

    async def test_logged_five_step_expressions(self):
        for text in ('Work forward 5 steps.', '向前走 5 步，听不懂英文？', '向前走五步，请说中文。'):
            events=[]
            actions=MotionpackActions(Robot(events),Body(),enabled=True,addressed=True)
            self.assertEqual((await actions.dispatch(request(),text))['status'],'queued_after_speech',text)
            await actions.after_playback()
            self.assertEqual(events[1][2]['steps'],5)
        for text in ('Five steps.', '向前走五步，向右转', '不要work forward 5 steps'):
            actions=MotionpackActions(Robot([]),Body(),enabled=True,addressed=True)
            self.assertEqual((await actions.dispatch(request(),text))['status'],'rejected',text)
            self.assertIsNone(await actions.after_playback())

    async def test_cloud_timeout_preserves_healthy_link_but_discards_action(self):
        from tars_companion.xiaozhi_service import recover_cloud_timeout
        import time
        events=[]; link=Body(); link.closed=asyncio.Event()
        actions=MotionpackActions(Robot(events),link,enabled=True,addressed=True)
        await actions.dispatch(request(),'向前走五步')
        until=await recover_cloud_timeout(link,actions,180)
        self.assertGreater(until,time.monotonic()+179)
        self.assertFalse(link.closed.is_set())
        self.assertIsNone(await actions.after_playback())
        self.assertEqual(events,[])
        link.closed.set()
        with self.assertRaises(TimeoutError):
            await recover_cloud_timeout(link,actions,180)

    async def test_requested_forward_steps_and_hard_limit(self):
        for count, word in enumerate(('一', '二', '三', '四', '五'), 1):
            for text in (f'向前走{word}步', f'向前走{count}步',
                         'walk forward ' + ('one','two','three','four','five')[count-1] + ' steps'):
                events=[]
                actions=MotionpackActions(Robot(events),Body(),enabled=True,addressed=True)
                queued=await actions.dispatch(request(),text)
                self.assertEqual(queued['status'],'queued_after_speech',text)
                self.assertEqual(events,[])
                answer=await actions.after_playback()
                self.assertTrue(answer['executed'])
                self.assertEqual(events[1][2]['steps'],count)
                self.assertIsNone(await actions.after_playback())
        for text in ('向前走六步','向前走20步','向前走二十一歩','向前走十一',
                     '向前走0步','move forward six steps','move forward 99 steps',
                     '不要向前走五步','他说向前走五步','向前走四步然后再走四步'):
            events=[]
            actions=MotionpackActions(Robot(events),Body(),enabled=True,addressed=True)
            self.assertEqual((await actions.dispatch(request(),text))['status'],'rejected',text)
            self.assertIsNone(await actions.after_playback())
            self.assertEqual(events,[])

    async def test_independent_audio_survives_control_loss_without_action_replay(self):
        class Lost(Robot):
            async def execute(self, *args, **kwargs): raise ConnectionError('lost')
            async def disarm(self): raise ConnectionError('lost')
        events=[]; link=Body()
        actions=MotionpackActions(Lost(events),link,enabled=True,independent_audio=True)
        await actions.dispatch(request(),'塔斯，向前走')
        result=await actions.after_playback()
        self.assertFalse(result['executed'])
        self.assertFalse(actions.enabled)
        self.assertEqual(link.mode,'idle')
        self.assertIsNone(await actions.after_playback())

    async def test_active_conversation_accepts_fresh_command_without_repeating_wake(self):
        self.assertEqual(intent_for('前进一下', addressed=True).action, 'walk_forward')
        self.assertEqual(intent_for('Hello TARS, turn left', addressed=True).action, 'turn_left')
        self.assertEqual(intent_for('TASK，向前走', addressed=True).action, 'walk_forward')
        self.assertIsNone(intent_for('前进一下'))
        self.assertFalse(getattr(intent_for('不要前进', addressed=True), 'action', None) in GAITS)

    async def test_disarmed_degraded_health_is_expected_before_arm(self):
        class DisarmedRobot(Robot):
            async def state(self):
                state=await super().state(); state.message['payload']['result']['health']=1; return state
        events=[]; actions=MotionpackActions(DisarmedRobot(events),Body(),enabled=True)
        await actions.dispatch(request(),'塔斯，前进')
        answer=await actions.after_playback()
        self.assertTrue(answer['executed'])

    async def test_speech_finishes_then_arm_execute_disarm(self):
        events=[]; robot=Robot(events); link=Body(); mic=Mic()
        class Speaker(Sink):
            async def finish(self): events.append('speech_done'); await super().finish()
        actions=MotionpackActions(robot,link,enabled=True)
        ws=WS([
            {'type':'conversation.item.input_audio_transcription.completed','text':'塔斯，向前走'},
            {'type':'response.function_call_arguments.done','items':[request()]},
            {'type':'response.output_audio.delta','delta':base64.b64encode(b'\0\x10'*480).decode()},
            {'type':'response.output_audio.done'},
        ])
        answer=await turn(ws,mic,Speaker(mic),link,actions=actions)
        self.assertEqual(events[0:2],['speech_done','arm'])
        self.assertEqual(events[2][0:2],('execute','walk_forward'))
        self.assertEqual(events[3],'disarm')
        self.assertTrue(answer['actions'][-1]['executed'])
        self.assertFalse(robot.armed)

    async def test_support_gate_and_no_debug_entries(self):
        self.assertEqual(len(ACTIONS),21)
        robot=Robot([]); actions=MotionpackActions(robot,Body(),enabled=True)
        self.assertEqual((await actions.dispatch(request('hello'),'塔斯，打招呼'))['status'],'rejected')
        self.assertIsNone(actions.pending)
        for text in ['塔斯，不要向前走','塔斯，说一句向前走','向前走','塔斯，support probe','塔斯，hello small']:
            intent=intent_for(text)
            self.assertFalse(intent and intent.action in GAITS|set(ACTIONS))

    async def test_fixed_emotion_parameters_and_duplicate(self):
        events=[]; actions=MotionpackActions(Robot(events),Body(),enabled=True,support_confirmed=True)
        first=await actions.dispatch(request('happy'),'塔斯，开心一下')
        self.assertEqual(first['status'],'queued_after_speech')
        self.assertEqual(events,[])
        await actions.after_playback()
        again=await actions.dispatch(request('happy'),'塔斯，开心一下')
        self.assertTrue(again['executed'])
        self.assertIsNone(await actions.after_playback())
        payload=events[1][2]
        self.assertEqual((payload['steps'],payload['speed_pct'],payload['support_confirm']),(1,100,True))

    async def test_playback_error_discards_staged_action(self):
        events=[]; mic=Mic(); actions=MotionpackActions(Robot(events),Body(),enabled=True)
        class BadSink(Sink):
            async def finish(self): raise ConnectionError('playback_failed')
        ws=WS([
            {'type':'conversation.item.input_audio_transcription.completed','text':'塔斯，前进'},
            {'type':'response.function_call_arguments.done','items':[request()]},
            {'type':'response.output_audio.delta','delta':base64.b64encode(b'\0\x10'*480).decode()},
            {'type':'response.output_audio.done'}])
        with self.assertRaises(ConnectionError): await turn(ws,mic,BadSink(mic),Body(),actions=actions)
        self.assertEqual(events,[])
        self.assertIsNone(actions.pending)

    async def test_uncertain_command_disarms_disables_and_never_replays(self):
        events=[]; robot=Robot(events); robot.fail=True
        actions=MotionpackActions(robot,Body(),enabled=True)
        await actions.dispatch(request(),'TARS, move forward')
        answer=await actions.after_playback()
        self.assertFalse(answer['executed']); self.assertFalse(actions.enabled)
        self.assertEqual(events[-1],'disarm')
        await actions.dispatch(request(),'TARS, move forward')
        self.assertIsNone(await actions.after_playback())

    async def test_busy_audio_cannot_arm(self):
        events=[]; link=Body(); link.mode='playback'
        actions=MotionpackActions(Robot(events),link,enabled=True)
        await actions.dispatch(request(),'塔斯，前进')
        await actions.after_playback()
        self.assertEqual(events,['disarm'])


class ListeningTests(unittest.IsolatedAsyncioTestCase):
    async def test_disconnected_capture_cleanup_does_not_kill_reconnect(self):
        class Source:
            stops=0
            async def start_capture(self): pass
            async def read_frame(self): raise ConnectionError('capture_interrupted')
            async def stop_capture(self):
                self.stops+=1
                raise ConnectionError('link_already_closed')
        source=Source(); gate=LocalListeningMicrophone(source,Body())
        with self.assertRaisesRegex(ConnectionError,'capture_interrupted'):
            await gate.prepare()
        await gate.stop_capture()
        await gate.stop_capture()
        self.assertFalse(gate.active)
        self.assertEqual(source.stops,1)

    async def test_prebuffer_larger_than_two_seconds_uses_backpressure(self):
        class Prebuffered:
            prepared=True
            async def read_frame(self): return bytes(3200)
        class Cloud:
            count=0
            async def send(self, data): self.count+=1
        cloud=Cloud(); task=asyncio.create_task(upload_audio(cloud,Prebuffered()))
        await asyncio.sleep(.15)
        self.assertFalse(task.done(), 'pre-roll must not overflow the live uploader queue')
        self.assertGreater(cloud.count,1)
        self.assertLess(cloud.count,20)
        task.cancel(); await asyncio.gather(task,return_exceptions=True)

    async def test_local_silence_cycles_capture_and_preserves_speech_start(self):
        class Source:
            starts=stops=0; count=0
            async def start_capture(self): self.starts+=1
            async def stop_capture(self): self.stops+=1
            async def read_frame(self):
                await asyncio.sleep(0)
                self.count+=1
                return b'\0\x10'*1600 if 85<=self.count<=89 else bytes(3200)
        source=Source(); gate=LocalListeningMicrophone(source,Body())
        await gate.prepare()
        self.assertEqual(source.starts,2)
        frames=[]
        for i in range(10): frames.append(await gate.read_frame())
        self.assertTrue(any(f!=bytes(3200) for f in frames))
        await gate.stop_capture()
        self.assertEqual(source.starts,source.stops)

    async def test_cancel_silent_wait_releases_recording(self):
        class Source:
            stopped=False
            async def start_capture(self): pass
            async def stop_capture(self): self.stopped=True
            async def read_frame(self): await asyncio.sleep(10)
        source=Source(); gate=LocalListeningMicrophone(source,Body())
        task=asyncio.create_task(gate.prepare()); await asyncio.sleep(.01)
        task.cancel(); await asyncio.gather(task,return_exceptions=True)
        await gate.stop_capture()
        self.assertTrue(source.stopped)


class PersistenceTests(unittest.TestCase):
    def test_voice_end_returns_to_standby_without_matching_quotes(self):
        for text in ['塔斯，结束对话。','Goodbye, TARS.','go to sleep','晚安']:
            self.assertTrue(end_conversation(text),text)
        for text in ['不要结束对话','Explain how to stop listening','say goodbye to my friend']:
            self.assertFalse(end_conversation(text),text)

    def test_recognized_request_survives_failed_playback_and_restart(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'memory.db'; memory=VoiceMemory(path)
            row=memory.record('TARS, remember that my base is Morning Star','',played=False)
            memory.close(); memory=VoiceMemory(path)
            self.assertIn('Morning Star',memory.context())
            self.assertIn('not confirmed spoken',memory.context())
            memory.complete(row,'I remember your base.')
            self.assertEqual(memory.db.execute('SELECT count(*) FROM voice_turns').fetchone()[0],1)
            self.assertIn('I remember your base',memory.context())
            memory.close()

    def test_restart_cannot_reset_allowance_and_crash_reserve_is_kept(self):
        with tempfile.TemporaryDirectory() as folder:
            path=str(Path(folder)/'budget.db')
            ledger=Allowance(path,100000); ledger.reserve(); ledger.close()
            ledger=Allowance(path,100000); ledger.reserve(); ledger.settle(1000)
            ledger.reserve()
            with self.assertRaises(RuntimeError): ledger.reserve()
            ledger.close()

    def test_yesterday_memory_restart_prompt_and_forget_index(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'memory.db'; memory=VoiceMemory(path)
            memory.record('记住：我的基地叫晨星','记住了')
            day=(datetime.now().astimezone()-timedelta(days=1)).isoformat()
            memory.db.execute('UPDATE voice_turns SET date=?',(day,)); memory.db.commit()
            memory.close(); memory=VoiceMemory(path)
            self.assertIn('晨星',memory.context('昨天聊了什么'))
            self.assertIn('晨星',instructions({'instructions':'TARS'},memory,None))
            memory.forget('晨星')
            self.assertNotIn('晨星',memory.context())
            self.assertFalse(memory.db.execute('SELECT 1 FROM voice_days').fetchall())
            memory.close()


if __name__=='__main__': unittest.main()
