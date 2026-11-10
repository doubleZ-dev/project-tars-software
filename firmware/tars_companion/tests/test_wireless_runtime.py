"""Loopback and pure PCM checks. No PCB, USB, cloud or credentials used."""
import asyncio
import math
import unittest
from unittest.mock import patch
from types import SimpleNamespace
from tars_companion.audio import ASR_PCM, PcmFormat, MemoryAudioSource
from tars_companion.audio_levels import BodyMicrophone, BodySpeaker, pack, samples, normalize, to_body_pcm
from tars_companion.client import TarsClient
from tars_companion.simulator import SimulatedTarsDevice
from tars_companion.transport import memory_transport_pair
from tars_companion import wifi_audio as wire
from tars_companion.robot_runtime import RuntimeClient, parser, providers, simulated_turn, interact
from tars_companion.robot_runtime import make_session
from tars_companion.audio import MemoryAudioSink, make_test_tone
from tars_companion.tools import ToolPolicyRejected
from tars_companion.voice import VoiceSession
from test_wifi_audio import FakeEsp, TOKEN


class KeepaliveCadenceTests(unittest.IsolatedAsyncioTestCase):
    async def test_delayed_ack_does_not_delay_next_keepalive(self):
        class DelayedTransport:
            requires_session_id=True
            def __init__(self): self.sent=[]
            async def write_line(self, wire):
                self.sent.append(asyncio.get_running_loop().time())
                await asyncio.sleep(.12)
            async def close(self): pass
        transport=DelayedTransport()
        client=TarsClient(transport,heartbeat_interval=.05)
        client.boot_id='1234567890abcdef'
        task=asyncio.create_task(client._heartbeat())
        try:
            await asyncio.sleep(.34)
            self.assertGreaterEqual(len(transport.sent),5)
            gaps=[b-a for a,b in zip(transport.sent,transport.sent[1:])]
            self.assertLess(max(gaps),.12)
        finally:
            task.cancel(); await asyncio.gather(task,return_exceptions=True)


class JsonEsp(FakeEsp):
    async def control_json(self, payload):
        await self.pc.write_line(payload)

    async def relay(self):
        while True:
            await self.ready.wait()
            line = await self.pc.read_line()
            await self.send(wire.CONTROL_RESULT, line)

    async def run(self):
        self.pc, peer = memory_transport_pair()
        self.device = SimulatedTarsDevice(peer, native_session_required=True)
        await self.device.start()
        task = asyncio.create_task(self.relay())
        try:
            await super().run()
        finally:
            task.cancel(); await asyncio.gather(task, return_exceptions=True)
            await self.device.close()


class WirelessControl(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.server = wire.LanAudioServer(TOKEN)
        self.port = await self.server.start("127.0.0.1", 0)
        self.peer = JsonEsp(self.port)
        self.task = asyncio.create_task(self.peer.run())
        self.link = await self.server.wait_connected(2)
        self.client = TarsClient(wire.NetworkJsonTransport(self.link))
        await self.client.start()

    async def asyncTearDown(self):
        await self.client.close(); await self.server.close()
        self.task.cancel(); await asyncio.gather(self.task, return_exceptions=True)

    async def test_handshake_disarmed_display_and_explicit_motion(self):
        self.assertIsNotNone(self.client.boot_id)
        denied = await self.client.execute("walk_forward")
        self.assertFalse(denied.accepted)
        self.assertTrue((await self.client.arm()).succeeded)
        self.assertTrue((await self.client.execute("turn_left")).succeeded)
        self.assertTrue((await self.client.display_dialogue("user", "中文屏幕消息")).succeeded)
        self.assertEqual(self.peer.device.execution_count, 1)

    async def test_exact_retry_replayed_not_executed_twice(self):
        await self.client.arm()
        payload = {"name": "walk_forward", "steps": 1, "speed_pct": 50}
        first = await self.client.command("motion.execute", payload, command_id="retry_test")
        second = await self.client.command("motion.execute", payload, command_id="retry_test")
        self.assertTrue(first.succeeded and second.succeeded)
        self.assertEqual(self.peer.device.execution_count, 1)

    async def test_disconnect_new_session_disarms_no_replay(self):
        await self.client.arm()
        await self.client.close()
        with self.assertRaises(ConnectionError):
            await self.client.execute("turn_right")
        self.task.cancel(); await asyncio.gather(self.task, return_exceptions=True)
        self.peer = JsonEsp(self.port, session=456)
        self.task = asyncio.create_task(self.peer.run())
        self.link = await self.server.wait_connected(2)
        self.client = TarsClient(wire.NetworkJsonTransport(self.link))
        await self.client.start()
        self.assertFalse(self.peer.device.armed)
        self.assertEqual(self.peer.device.execution_count, 0)

    async def test_runtime_guard_cannot_arm_or_execute(self):
        guarded = RuntimeClient(wire.NetworkJsonTransport(self.link))
        with self.assertRaises(ToolPolicyRejected):
            await guarded.arm()
        with self.assertRaises(ToolPolicyRejected):
            await guarded.execute("walk_forward")

    async def test_control_while_microphone_streaming(self):
        await self.link.start_capture()
        self.assertTrue((await self.client.state()).succeeded)
        self.assertEqual(len(await self.link.read_frame()), ASR_PCM.frame_bytes)
        self.assertTrue((await self.client.estop()).succeeded)
        await self.link.stop_capture()


class Levels(unittest.IsolatedAsyncioTestCase):
    async def test_actual_reported_transcript_executes_one_simulated_cycle(self):
        pc,peer=memory_transport_pair()
        device=SimulatedTarsDevice(peer)
        client=TarsClient(pc)
        try:
            await device.start(); await client.start(); await client.arm()  # simulator only
            args=parser().parse_args(["--mock","--mock-text","TARS向前走一步。"])
            asr,tts,provider=providers(args)
            session=make_session(client,asr,tts,provider,MemoryAudioSink())
            pcm=make_test_tone(300,volume_pct=10)+bytes(32000)
            frames=[pcm[i:i+ASR_PCM.frame_bytes] for i in range(0,len(pcm),ASR_PCM.frame_bytes)]
            turn=await session.run_turn(MemoryAudioSource(frames))
            self.assertIsNotNone(turn.response.command_id)
            self.assertEqual(device.execution_count,1)
        finally:
            await client.close(); await device.close()

    def test_unspaced_bilingual_wake_is_precise_not_substring(self):
        for text in ("TARS向前走一步。", "塔斯向前走一步。", "tars向前走一步。"):
            match=VoiceSession._addressed_text(text,"TARS|塔斯"," \t，,：:。.!！?？")
            self.assertTrue(match.precise)
            self.assertEqual(match.text,"向前走一步")
        self.assertIsNone(VoiceSession._addressed_text("TARSENAL向前走一步","TARS|塔斯"," ，,。"))
        fuzzy=VoiceSession._addressed_text("task向前走一步","TARS|塔斯"," ，,。")
        self.assertFalse(fuzzy.precise)

    def test_resample_qwen_24k_length_and_antialias(self):
        fmt = PcmFormat(sample_rate=24000)
        def tone(hz):
            return pack(10000 * math.sin(2 * math.pi * hz * i / 24000) for i in range(2400))
        low = samples(to_body_pcm(tone(1000), fmt))[40:-40]
        high = samples(to_body_pcm(tone(10000), fmt))[40:-40]
        self.assertEqual(len(to_body_pcm(tone(1000), fmt)), 3200)
        self.assertLess(sum(x*x for x in high), sum(x*x for x in low) * .02)

    def test_normalization_limits_dc_and_silence(self):
        self.assertEqual(normalize(bytes(100)), bytes(100))
        data = samples(normalize(pack([32000, -32000] * 100)))
        self.assertLessEqual(max(abs(x) for x in data), 26213)
        self.assertEqual(samples(normalize(pack([12000] * 100))), [0] * 100)
        self.assertEqual(samples(pack([-99999, 99999])), [-32768, 32767])

    async def test_microphone_discards_startup_and_filters_dc(self):
        class Link(MemoryAudioSource):
            async def start_capture(self): pass
            async def stop_capture(self): pass
        link = Link([pack([30000]*1600), pack([1000]*1600), pack([1000]*1600)])
        mic = BodyMicrophone(link, warmup_ms=100, gain=4)
        await mic.start_capture()
        await mic.read_frame()
        second = samples(await mic.read_frame())
        self.assertLess(max(abs(x) for x in second), 2)

    async def test_speaker_resamples_segments_and_cancel(self):
        class Sink:
            def __init__(self): self.calls=[]
            async def play(self, pcm, fmt, cancel, volume_pct):
                self.calls.append((len(pcm), fmt, volume_pct))
        link=Sink(); sink=BodySpeaker(link)
        await sink.play(bytes(24000*2*12), PcmFormat(sample_rate=24000), asyncio.Event())
        self.assertEqual([c[0] for c in link.calls], [256000,128000])
        cancel=asyncio.Event(); cancel.set()
        await sink.play(bytes(100), ASR_PCM, cancel)
        self.assertEqual(len(link.calls),2)

    async def test_mock_never_constructs_cloud_and_completes(self):
        args=parser().parse_args(["--mock","--once"])
        with patch("tars_companion.robot_runtime.DashScopeFunAsr", side_effect=AssertionError), \
             patch("tars_companion.robot_runtime.create_provider", side_effect=AssertionError):
            asr,tts,provider=providers(args)
            turn=await simulated_turn(asr,tts,provider)
            self.assertIsNone(turn.speech_error)
        args.allow_motion=True
        with self.assertRaises(ValueError): providers(args)


class OperatorSafety(unittest.IsolatedAsyncioTestCase):
    async def test_stop_sent_before_waiting_for_voice_cleanup(self):
        events=[]; started=asyncio.Event()
        class Link: closed=asyncio.Event()
        class Session:
            def barge_in(self): pass
            async def run_turn(self, source):
                started.set()
                try: await asyncio.Event().wait()
                finally: events.append("cleanup")
        class Client:
            async def estop(self):
                events.append("stop")
                return SimpleNamespace(message={"stopped":True})
        commands=iter(["", "/stop", "/quit"])
        async def fake_console(prompt):
            text=next(commands)
            if text=="/stop": await started.wait()
            return text
        with patch("tars_companion.robot_runtime.console",fake_console):
            await asyncio.wait_for(interact(SimpleNamespace(allow_motion=False,once=False),
                                           Client(),Link(),Session(),None),1)
        self.assertEqual(events,["stop","cleanup"])

    async def test_idle_disconnect_does_not_wait_for_console(self):
        link=SimpleNamespace(closed=asyncio.Event())
        async def blocked_console(prompt): await asyncio.Event().wait()
        with patch("tars_companion.robot_runtime.console",blocked_console):
            task=asyncio.create_task(interact(SimpleNamespace(allow_motion=False,once=False),None,link,None,None))
            await asyncio.sleep(.01); link.closed.set()
            with self.assertRaises(ConnectionError):
                await asyncio.wait_for(task,.5)


if __name__ == "__main__": unittest.main()
