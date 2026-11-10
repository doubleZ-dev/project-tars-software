"""Loopback TCP tests with a simulated ESP peer. No physical WLAN/cloud/USB."""
import asyncio
import contextlib
import json
import unittest
from unittest.mock import patch

from tars_companion import wifi_audio as wire
from tars_companion.audio import ASR_PCM, make_test_tone
from tars_companion.speech import create_offline_speech
from tars_companion.voice import VoiceSession, VoiceStageError
from tars_companion.brain import TarsBrain
from tars_companion.llm import ScriptedProvider
from tars_companion.client import TarsClient
from tars_companion.simulator import SimulatedTarsDevice
from tars_companion.transport import memory_transport_pair
from tars_companion.audio import UtteranceCollector

TOKEN = "a" * 32


class FakeEsp:
    def __init__(self, port, token=TOKEN, session=123, ack_delay=0):
        self.port, self.token, self.session = port, token, session
        self.tx = 0
        self.rx = 0
        self.mode = "idle"
        self.played = bytearray()
        self.states = []
        self.dialogue = []
        self.writer = None
        self.mic_task = None
        self.lock = asyncio.Lock()
        self.ready = asyncio.Event()
        self.ack_delay=ack_delay
        self.ack_tasks=set()
        self.received_at=[]

    async def delayed_ack(self, seq):
        await asyncio.sleep(self.ack_delay)
        await self.send(wire.ACK, wire.struct.pack('!IB',seq,0))

    async def send(self, kind, payload=b""):
        async with self.lock:
            self.writer.write(wire.encode(kind, self.session, self.tx, payload))
            self.tx += 1
            await self.writer.drain()

    async def microphone(self):
        pcm = make_test_tone(200, volume_pct=10) + bytes(6400)
        for i in range(0, len(pcm), 256):
            await self.send(wire.MIC_PCM, pcm[i:i+256])
            await asyncio.sleep(.001)

    async def run(self):
        self.reader, self.writer = await asyncio.open_connection("127.0.0.1", self.port)
        hello = bytes(range(38))
        try:
            await self.send(wire.HELLO, hello)
            kind, session, sequence, payload = await wire.read_frame(self.reader)
            if (kind, session, sequence, payload) != (
                    wire.AUTH, self.session, 0, wire.proof(self.token, b"PC", hello, self.session)):
                return
            self.rx = 1
            await self.send(wire.READY, wire.proof(self.token, b"ESP", hello, self.session))
            self.ready.set()
            while True:
                kind, session, seq, payload = await wire.read_frame(self.reader)
                if session != self.session or seq != self.rx:
                    raise AssertionError("sequence mismatch")
                self.rx += 1
                self.received_at.append((kind,asyncio.get_running_loop().time()))
                if kind == wire.CAP_START:
                    self.mode = "capture"
                elif kind == wire.CAP_STOP:
                    self.mode = "idle"
                    if self.mic_task:
                        self.mic_task.cancel()
                        await asyncio.gather(self.mic_task, return_exceptions=True)
                elif kind == wire.PLAY_START:
                    self.mode = "playback"; self.played.clear()
                elif kind == wire.PLAY_PCM:
                    self.played.extend(payload)
                elif kind == wire.PLAY_END:
                    self.mode = "idle"
                elif kind == wire.STATE:
                    self.states.append(wire.STATES[payload[0]])
                elif kind == wire.TEXT:
                    self.dialogue.append((payload[0], payload[1:].decode()))
                elif kind == wire.CONTROL_JSON:
                    await self.control_json(payload)
                elif kind != wire.PING:
                    raise AssertionError("unknown wire command")
                if self.ack_delay and kind in {wire.PING,wire.CONTROL_JSON}:
                    task=asyncio.create_task(self.delayed_ack(seq))
                    self.ack_tasks.add(task)
                    task.add_done_callback(self.ack_tasks.discard)
                else:
                    await self.send(wire.ACK, wire.struct.pack("!IB", seq, 0))
                if kind == wire.CAP_START:
                    self.mic_task = asyncio.create_task(self.microphone())
        except (asyncio.IncompleteReadError, ConnectionError, asyncio.CancelledError):
            pass
        finally:
            for task in self.ack_tasks: task.cancel()
            await asyncio.gather(*self.ack_tasks,return_exceptions=True)
            if self.mic_task:
                self.mic_task.cancel()
                await asyncio.gather(self.mic_task, return_exceptions=True)
            self.writer.close()
            with contextlib.suppress(Exception):
                await self.writer.wait_closed()

    async def control_json(self, payload):
        raise AssertionError("control requires a JSON simulator bridge")


class LanTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.server = wire.LanAudioServer(TOKEN)
        self.port = await self.server.start("127.0.0.1", 0)
        self.peer = FakeEsp(self.port)
        self.peer_task = asyncio.create_task(self.peer.run())
        self.link = await self.server.wait_connected(2)

    async def asyncTearDown(self):
        await self.server.close()
        self.peer_task.cancel()
        await asyncio.gather(self.peer_task, return_exceptions=True)

    async def test_authenticated_capture_playback_states_and_text(self):
        await self.link.interaction("LISTENING")
        await self.link.start_capture()
        pcm = await asyncio.wait_for(self.link.read_frame(), 1)
        self.assertEqual(len(pcm), ASR_PCM.frame_bytes)
        await self.link.stop_capture()
        tone = make_test_tone()
        await self.link.play(tone, ASR_PCM, asyncio.Event())
        await self.link.display_dialogue("tars", "无线音频框架测试")
        self.assertEqual(self.peer.played, tone)
        self.assertEqual(self.peer.states, ["LISTENING"])
        self.assertEqual(self.peer.dialogue, [(1, "无线音频框架测试")])
        self.assertEqual(self.link.mode, "idle")

    async def test_full_voice_turn_with_real_loopback_wire_and_mock_services(self):
        pc, robot = memory_transport_pair()
        simulator = SimulatedTarsDevice(robot)
        await simulator.start()
        client = TarsClient(pc)
        try:
            await client.start()
            asr, tts = create_offline_speech(mock=True)
            session = VoiceSession(TarsBrain(client, provider=ScriptedProvider("Offline reply.")),
                asr, tts, self.link, UtteranceCollector(silence_ms=200),
                dialogue_publisher=self.link.display_dialogue, interaction_publisher=self.link.interaction)
            turn = await session.run_turn(self.link)
            self.assertIsNone(turn.speech_error)
            self.assertEqual(self.peer.states, ["LISTENING", "THINKING", "SPEAKING", "IDLE"])
            self.assertEqual(len(self.peer.dialogue), 2)
            self.assertGreater(len(self.peer.played), 0)
            self.assertFalse(simulator.armed)
            self.assertEqual(simulator.execution_count, 0)
        finally:
            await client.close(); await simulator.close()

    async def test_half_duplex_and_format_guards(self):
        await self.link.start_capture()
        with self.assertRaisesRegex(RuntimeError, "half_duplex"):
            await self.link.play(make_test_tone(), ASR_PCM, asyncio.Event())
        await self.link.stop_capture()
        with self.assertRaises(ValueError):
            await self.link.play(b"x", ASR_PCM, asyncio.Event())
        with self.assertRaises(ValueError):
            await self.link.play(make_test_tone(), ASR_PCM, asyncio.Event(), volume_pct=100)

    async def test_disconnect_then_new_session_does_not_resume_audio(self):
        await self.link.start_capture()
        self.peer_task.cancel()
        await asyncio.gather(self.peer_task, return_exceptions=True)
        await asyncio.wait_for(self.link.closed.wait(), 1)
        with self.assertRaises(ConnectionError):
            await self.link.read_frame()
        await asyncio.sleep(.02)
        self.peer = FakeEsp(self.port, session=456)
        self.peer_task = asyncio.create_task(self.peer.run())
        new = await self.server.wait_connected(2)
        self.assertEqual(new.session, 456)
        self.assertEqual(new.mode, "idle")
        self.assertEqual(new.frames.qsize(), 0)
        await new.start_capture()
        await new.read_frame()
        await new.stop_capture()

    async def test_bad_sequence_closes_without_audio_replay(self):
        async with self.peer.lock:
            self.peer.writer.write(wire.encode(wire.MIC_PCM, self.peer.session, 999, b"\0\0"))
            await self.peer.writer.drain()
        await asyncio.wait_for(self.link.closed.wait(), 1)
        self.assertEqual(self.link.mode, "idle")

    async def test_microphone_queue_overflow_is_bounded(self):
        self.link.mode = "capture"
        for _ in range(40):
            if self.link.closed.is_set():
                break
            with contextlib.suppress(ConnectionError):
                await self.peer.send(wire.MIC_PCM, bytes(256))
        await asyncio.wait_for(self.link.closed.wait(), 1)
        self.assertLessEqual(self.link.frames.qsize(), 32)


class WireTests(unittest.IsolatedAsyncioTestCase):
    async def test_delayed_ping_ack_keeps_fixed_cadence_then_loss_closes(self):
        server=wire.LanAudioServer(TOKEN)
        port=await server.start('127.0.0.1',0)
        peer=FakeEsp(port,ack_delay=.9)
        task=asyncio.create_task(peer.run())
        try:
            link=await server.wait_connected(2)
            await asyncio.sleep(3.2)
            self.assertFalse(link.closed.is_set())
            stamps=[t for k,t in peer.received_at if k==wire.PING]
            self.assertGreaterEqual(len(stamps),5)
            self.assertLess(max(b-a for a,b in zip(stamps,stamps[1:])),.8)
            peer.ack_delay=10
            await asyncio.wait_for(link.closed.wait(),4)
            self.assertIn('TimeoutError',link.close_reason)
            self.assertEqual(link.mode,'idle')
        finally:
            await server.close(); task.cancel(); await asyncio.gather(task,return_exceptions=True)

    async def test_wrong_pairing_secret_is_rejected(self):
        server = wire.LanAudioServer(TOKEN)
        port = await server.start("127.0.0.1", 0)
        peer = FakeEsp(port, token="b" * 32)
        task = asyncio.create_task(peer.run())
        try:
            with self.assertRaises(TimeoutError):
                await server.wait_connected(.15)
            self.assertIsNone(server.current)
            await asyncio.wait_for(task, 1)
        finally:
            await server.close(); task.cancel(); await asyncio.gather(task, return_exceptions=True)

    async def test_fragmentation_and_oversize_header_rejected(self):
        reader = asyncio.StreamReader()
        packet = wire.encode(wire.PING, 99, 0)
        for byte in packet:
            reader.feed_data(bytes([byte]))
        self.assertEqual(await wire.read_frame(reader), (wire.PING, 99, 0, b""))
        reader = asyncio.StreamReader()
        reader.feed_data(wire.HEADER.pack(b"TWA1", wire.MIC_PCM, 0, 1025, 99, 0))
        with self.assertRaises(ConnectionError):
            await wire.read_frame(reader)

    async def test_profile_validation_does_not_expose_secret(self):
        profile = {"ssid": "test", "password": "test-password", "host": "192.168.1.2", "port": 8765, "token": TOKEN}
        with patch("pathlib.Path.read_text", return_value=json.dumps(profile)):
            self.assertEqual(wire.load_profile("unused"), profile)
        profile["token"] = "SECRET_INVALID_TOKEN"
        with patch("pathlib.Path.read_text", return_value=json.dumps(profile)):
            with self.assertRaises(ValueError) as error:
                wire.load_profile("unused")
            self.assertNotIn("SECRET_INVALID_TOKEN", str(error.exception))


if __name__ == "__main__":
    unittest.main()
