"""Offline only: no credentials, cloud, sound device or physical robot."""
import asyncio
import gzip
import json
import struct
import unittest
from pathlib import Path
from tars_companion.audio import make_test_tone, PcmFormat
from tars_companion.audio_levels import to_body_pcm
from tars_companion.doubao import encode, decode, session_config, StreamingResampler
from tars_companion.doubao_runtime import turn, Output, preflight, DesktopMic
from tars_companion.robot_runtime import parser
from tars_companion import wifi_audio as wire

CONFIG = Path(__file__).resolve().parents[1] / "doubao.example.json"


def server_frame(event, payload, sid="s", compressed=False):
    audio = isinstance(payload, bytes)
    body = payload if audio else json.dumps(payload).encode()
    if compressed:
        body = gzip.compress(body)
    identity = sid.encode()
    return bytes((0x11, 0xb4 if audio else 0x94, (0 if audio else 0x10) | compressed, 0)) + struct.pack(
        "!II", event, len(identity)) + identity + struct.pack("!I", len(body)) + body


class ProtocolTests(unittest.TestCase):
    def test_official_connection_example(self):
        self.assertEqual(list(encode(1)), [17,20,16,0,0,0,0,1,0,0,0,2,123,125])

    def test_decode_audio_json_gzip_and_malformed(self):
        for payload in (b"\0\1" * 480, {"text": "中文 English"}):
            for compressed in (False, True):
                raw = server_frame(352, payload, compressed=compressed)
                self.assertEqual(decode(raw), (352, "s", payload))
                for broken in (raw[:-1], raw + b"x", b"bad"):
                    with self.assertRaises(ValueError):
                        decode(broken)

    def test_speaker_model_and_role(self):
        config = json.loads(CONFIG.read_text(encoding="utf-8"))
        request = session_config(config)
        self.assertEqual(request["tts"]["speaker"], "S_vXjewQyk2")
        self.assertEqual(request["tts"]["audio_config"]["format"], "pcm_s16le")
        self.assertIn("character_manifest", request["dialog"])
        for key, value in (("model", "seed-icl-2.0"), ("speaker", "preset")):
            with self.assertRaises(ValueError):
                session_config({**config, key: value})

    def test_resampler_is_independent_of_packet_boundaries(self):
        fmt = PcmFormat(sample_rate=24000)
        pcm = make_test_tone(300, pcm_format=fmt)
        expected = to_body_pcm(pcm, fmt)
        for size in (1, 37, 960, 4096):
            converter = StreamingResampler()
            actual = b"".join(converter.feed(pcm[i:i+size]) for i in range(0, len(pcm), size))
            actual += converter.feed(b"", final=True)
            self.assertEqual(actual, expected)
        with self.assertRaises(ValueError):
            StreamingResampler().feed(b"x", final=True)

    def test_preflight_defaults_cost_compatibility_and_motion(self):
        self.assertEqual(parser().parse_args(["--mock"]).voice_mode, "legacy")
        base = ["--voice-mode", "doubao", "--doubao-config", str(CONFIG)]
        preflight(parser().parse_args(base + ["--mock"]))
        for flags in (["--live"], ["--live", "--approve-doubao-cost"],
                      ["--mock", "--allow-motion"],
                      ["--live", "--approve-doubao-cost", "--speaker-compatible", "--robot"]):
            with self.assertRaises(ValueError):
                preflight(parser().parse_args(base + flags))
        self.assertEqual(DesktopMic().mic.format.frame_bytes, 640)


class Mic:
    def __init__(self):
        self.active = False

    async def start_capture(self):
        self.active = True

    async def read_frame(self):
        await asyncio.sleep(.02)
        return b"\x00\x10" * 320

    async def stop_capture(self):
        self.active = False


class Sink:
    def __init__(self, mic):
        self.mic = mic
        self.data = bytearray()
        self.written = asyncio.Event()
        self.aborted = False

    async def write(self, pcm):
        assert not self.mic.active, "echo capture during playback"
        self.data.extend(pcm)
        self.written.set()

    async def finish(self):
        pass

    async def abort(self):
        self.aborted = True


class Ws:
    def __init__(self):
        self.frames = asyncio.Queue()
        self.sent = []

    async def send(self, frame):
        self.sent.append(frame)

    async def recv(self):
        frame = await self.frames.get()
        if isinstance(frame, Exception):
            raise frame
        return frame

    def push(self, event, payload):
        self.frames.put_nowait(server_frame(event, payload))


class RuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def test_two_turns_stream_before_completion_and_stop_capture(self):
        mic = Mic()
        sink, ws = Sink(mic), Ws()
        for _ in range(2):
            sink.written.clear()
            task = asyncio.create_task(turn(ws, "s", mic, sink))
            await asyncio.sleep(.05)
            ws.push(459, {})
            ws.push(352, bytes(960))
            await asyncio.wait_for(sink.written.wait(), 1)
            self.assertFalse(task.done())  # audio starts before TTSEnded
            ws.push(359, {})
            await asyncio.wait_for(task, 2)
            self.assertFalse(mic.active)
        self.assertEqual(len(sink.data), 1280)

    async def test_disconnect_cancel_and_overflow_fail_closed(self):
        for failure in ("disconnect", "timeout", "cancel", "overflow", "wrong_session"):
            mic = Mic()
            sink, ws = Sink(mic), Ws()
            task = asyncio.create_task(turn(ws, "s", mic, sink))
            await asyncio.sleep(.03)
            if failure == "disconnect":
                ws.frames.put_nowait(ConnectionError("offline_test"))
            elif failure == "timeout":
                ws.frames.put_nowait(TimeoutError())
            elif failure == "cancel":
                task.cancel()
            elif failure == "overflow":
                ws.push(352, bytes(960 * 501))
            else:
                ws.frames.put_nowait(server_frame(352, bytes(960), sid="wrong"))
            with self.assertRaises((ConnectionError, TimeoutError, asyncio.CancelledError, asyncio.QueueFull)):
                await asyncio.wait_for(task, 2)
            self.assertTrue(sink.aborted)
            self.assertFalse(mic.active)

    async def test_cloud_audio_without_asr_end_still_stops_mic(self):
        mic = Mic()
        sink, ws = Sink(mic), Ws()
        task = asyncio.create_task(turn(ws, "s", mic, sink))
        await asyncio.sleep(.03)
        ws.push(352, bytes(960))
        await asyncio.wait_for(sink.written.wait(), 1)
        ws.push(359, {})
        await task

    async def test_robot_stream_rollover_and_abort(self):
        class Link:
            mode = "idle"
            def __init__(self):
                self.commands = []
                self.closed = False
            async def command(self, kind, payload=b""):
                self.commands.append((kind, payload))
            async def close(self):
                self.closed = True
        link = Link()
        output = Output(link, 10)
        await output.write(bytes(320000))
        await output.finish()
        self.assertEqual(sum(k == wire.PLAY_START for k, _ in link.commands), 2)
        self.assertTrue(all(len(p) <= 512 for k, p in link.commands if k == wire.PLAY_PCM))
        self.assertEqual(link.mode, "idle")
        await output.abort()
        self.assertTrue(link.closed)

    async def test_stream_through_authenticated_loopback(self):
        from test_wifi_audio import FakeEsp, TOKEN
        server = wire.LanAudioServer(TOKEN)
        port = await server.start("127.0.0.1", 0)
        peer = FakeEsp(port)
        peer_task = asyncio.create_task(peer.run())
        try:
            link = await server.wait_connected(2)
            output = Output(link, 10)
            await output.write(bytes(640))
            self.assertEqual(len(peer.played), 640)
            self.assertEqual(peer.mode, "playback")
            self.assertIsNotNone(output.first_ack)
            await output.write(bytes(640))
            await output.finish()
            self.assertEqual(len(peer.played), 1280)
            self.assertEqual(peer.mode, "idle")
        finally:
            await server.close()
            peer_task.cancel()
            await asyncio.gather(peer_task, return_exceptions=True)


if __name__ == "__main__":
    unittest.main()
