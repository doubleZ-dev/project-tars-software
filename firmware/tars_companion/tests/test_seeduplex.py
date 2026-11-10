import asyncio
import base64
import json
import unittest
from tars_companion.seeduplex import turn, receive, SegmentedOutput, upload_audio, BufferedRobotOutput


class Mic:
    active = False
    async def start_capture(self): self.active = True
    async def stop_capture(self): self.active = False
    async def read_frame(self):
        await asyncio.sleep(.02)
        return b"\0\0" * 320


class Link:
    async def interaction(self, value): pass


class Sink:
    bytes_sent = 0
    finished = False
    def __init__(self, mic): self.mic = mic
    async def write(self, pcm):
        assert not self.mic.active
        self.bytes_sent += len(pcm)
    async def finish(self): self.finished = True


class WS:
    def __init__(self, events): self.events = iter(events); self.sent = []
    async def send(self, text): self.sent.append(json.loads(text))
    async def recv(self):
        await asyncio.sleep(.03)
        return json.dumps(next(self.events))


class SeeduplexTests(unittest.IsolatedAsyncioTestCase):
    async def test_one_robot_reconnect_resumes_history_without_replaying_completed_turn(self):
        from unittest.mock import patch
        from types import SimpleNamespace
        from pathlib import Path
        from tars_companion.seeduplex import run
        class Robot:
            mode = 'idle'
            def __init__(self): self.closed = asyncio.Event()
            async def close(self): self.closed.set()
        robots = [Robot(), Robot()]
        class Server:
            def __init__(self, token): self.index = 0
            async def start(self, host, port): pass
            async def wait_connected(self, timeout):
                link = robots[self.index]; self.index += 1
                return link
            async def close(self): pass
        clients = []
        class Client:
            def __init__(self, transport, allow_motion):
                assert not allow_motion
                self.link = transport.link; self.disarms = 0
                self.hello = {'payload': {'firmware_version': 'test'}}
                clients.append(self)
            async def start(self): pass
            async def disarm(self): self.disarms += 1
            async def close(self): await self.link.close()
        contexts = []
        class Cloud:
            def __init__(self): self.sent = []; contexts.append(self)
            async def __aenter__(self): return self
            async def __aexit__(self, *args): pass
            async def send(self, text): self.sent.append(json.loads(text))
            async def recv(self): return json.dumps({'type': 'session.created', 'session': {'id': 'history-test'}})
        calls = []
        async def fake_turn(ws, source, output, link):
            calls.append(link)
            if len(calls) == 2:
                link.closed.set()
                raise ConnectionError('capture_interrupted')
        args = SimpleNamespace(turns=2, seconds=30, config='unused',
            persona=str(Path(__file__).resolve().parents[1]/'tars-seeduplex.json'),
            check=False, live=True, free_tokens=100000, volume=20)
        with patch('tars_companion.seeduplex.load_profile', return_value={'token': 'unused', 'host': 'unused', 'port': 1}), \
             patch('tars_companion.seeduplex.LanAudioServer', Server), \
             patch('tars_companion.seeduplex.RuntimeClient', Client), \
             patch('tars_companion.seeduplex.turn', fake_turn), \
             patch('websockets.asyncio.client.connect', lambda *a, **k: Cloud()), \
             patch.dict('os.environ', {'DOUBAO_API_KEY': 'dummy-not-a-real-key'}):
            await run(args)
        self.assertEqual(calls, [robots[0], robots[0], robots[1]])
        self.assertEqual(len(contexts), 2)
        self.assertEqual(contexts[1].sent[0]['session']['id'], 'history-test')
        self.assertEqual([c.disarms for c in clients], [1, 1])

    async def test_audio_done_without_usage_plays_and_late_usage_does_not_end_next_turn(self):
        events = [
            {"type": "conversation.item.input_audio_transcription.completed"},
            {"type": "response.output_audio.delta", "delta": base64.b64encode(b"\0\x10" * 480).decode()},
            {"type": "response.output_audio.done"},
        ]
        ws = WS(events + [{"type": "response.done"}] + events)
        mic = Mic()
        for _ in range(2):
            sink = Sink(mic)
            await asyncio.wait_for(turn(ws, mic, sink, Link()), 2)
            self.assertTrue(sink.finished)
            self.assertEqual(sink.bytes_sent, 640)
            self.assertFalse(mic.active)

    async def test_two_turns_reuse_session_and_restart_capture(self):
        events = [
            {"type": "conversation.item.input_audio_transcription.completed"},
            {"type": "response.output_audio.delta", "delta": base64.b64encode(b"\0\x10" * 480).decode()},
            {"type": "response.done"},
        ]
        ws = WS(events + events)
        mic = Mic()
        for _ in range(2):
            sink = Sink(mic)
            await turn(ws, mic, sink, Link())
            self.assertFalse(mic.active)
            self.assertTrue(sink.finished)
            self.assertEqual(sink.bytes_sent, 640)
        kinds = [e['type'] for e in ws.sent]
        self.assertEqual(kinds.count('input_audio_unmute.commit'), 2)
        self.assertEqual(kinds.count('input_audio_mute.commit'), 2)

    async def test_buffered_reply_preserves_pcm_and_bounds_pipeline(self):
        from tars_companion.wifi_audio import PLAY_START, PLAY_PCM, PLAY_END
        class Robot:
            mode = 'idle'
            active = peak = 0
            data = b''
            ended = False
            async def command(self, kind, payload=b''):
                if kind == PLAY_PCM:
                    self.active += 1
                    self.peak = max(self.peak, self.active)
                    self.data += payload
                    await asyncio.sleep(.001)
                    self.active -= 1
                elif kind == PLAY_END:
                    assert self.active == 0
                    self.ended = True
            async def close(self): pass
        robot = Robot(); out = BufferedRobotOutput(robot, 20)
        pcm = b'\0\x70\0\x90' * 5000
        await out.write(pcm[:100]); await out.write(pcm[100:])
        self.assertEqual(robot.data, b'')
        await out.finish()
        self.assertEqual(robot.data, pcm)
        self.assertEqual(out.bytes_sent, len(pcm))
        self.assertEqual(robot.peak, 4)
        self.assertTrue(robot.ended)
        self.assertEqual(robot.mode, 'idle')

    async def test_audio_burst_larger_than_queue_applies_backpressure(self):
        from unittest.mock import patch
        class Converter:
            def feed(self, data, final=False): return data
        class SlowSink(Sink):
            async def write(self, pcm):
                await asyncio.sleep(.001)
                await super().write(pcm)
        mic = Mic(); sink = SlowSink(mic)
        pcm = b"\0\x10" * (480 * 510)
        ws = WS([
            {"type": "response.output_audio.delta", "delta": base64.b64encode(pcm).decode()},
            {"type": "response.done"},
        ])
        with patch('tars_companion.seeduplex.StreamingResampler', Converter):
            await turn(ws, mic, sink, Link())
        self.assertEqual(sink.bytes_sent, len(pcm))
        self.assertTrue(sink.finished)

    async def test_hardware_batches_are_drained_while_network_is_paced(self):
        class BatchedMic:
            def __init__(self): self.count = 0; self.reading = False
            async def read_frame(self):
                self.reading = True
                try:
                    await asyncio.sleep(.1)
                    self.count += 1
                    return b"\0\x10" * 1600
                finally:
                    self.reading = False
        class SlowWS:
            def __init__(self): self.count = 0
            async def send(self, data):
                await asyncio.sleep(.025)
                self.count += 1
        mic, ws = BatchedMic(), SlowWS()
        with self.assertRaises(TimeoutError):
            await upload_audio(ws, mic, duration=.65)
        # Capture must not block behind upload sleeps: ~6 x100ms batches.
        self.assertGreaterEqual(mic.count, 5)
        self.assertGreater(ws.count, 10)
        self.assertFalse(mic.reading)

    async def test_real_event_sequence_mutes_capture_and_resamples_playback(self):
        mic = Mic(); sink = Sink(mic)
        ws = WS([
            {"type": "conversation.item.input_audio_transcription.completed"},
            {"type": "response.output_audio.delta", "delta": base64.b64encode(b"\0\x10" * 24000).decode()},
            {"type": "response.done"},
        ])
        await turn(ws, mic, sink, Link())
        self.assertFalse(mic.active)
        self.assertTrue(sink.finished)
        self.assertEqual(sink.bytes_sent, 32000)
        self.assertIn("input_audio_mute.commit", [e["type"] for e in ws.sent])

    async def test_remote_error_does_not_leak_message(self):
        ws = WS([{"type": "error", "error": {"code": "InvalidSpeaker", "message": "SECRET"}}])
        with self.assertRaisesRegex(RuntimeError, "cloud_error_InvalidSpeaker") as cm:
            await receive(ws)
        self.assertNotIn("SECRET", str(cm.exception))

    async def test_failure_releases_microphone(self):
        mic = Mic()
        ws = WS([{"type": "response.output_audio.done"}])
        with self.assertRaisesRegex(RuntimeError, "response_has_no_audio"):
            await turn(ws, mic, Sink(mic), Link())
        self.assertFalse(mic.active)

    async def test_segmented_output_bounded_transactions(self):
        class Playback:
            def __init__(self): self.chunks = []; self.finishes = 0
            async def write(self, pcm): self.chunks.append(pcm)
            async def finish(self): self.finishes += 1
        output = SegmentedOutput(None, 20, gain=1)
        output.output = Playback()
        data = b"\0\x01" * 16000
        await output.write(data[:333]); await output.write(data[333:]); await output.finish()
        self.assertEqual(b"".join(output.output.chunks), data)
        self.assertTrue(all(len(p) <= 12800 for p in output.output.chunks))
        self.assertEqual(output.bytes_sent, 32000)
        self.assertEqual(output.output.finishes, 1)

    async def test_playback_gain_doubles_quiet_samples_and_limits_peaks(self):
        import struct
        class Playback:
            def __init__(self): self.data = b""
            async def write(self, pcm): self.data += pcm
            async def finish(self): pass
        output = SegmentedOutput(None, 20, gain=3)
        output.output = Playback()
        await output.write(struct.pack('<hhhh', 1000, -1000, 20000, -20000))
        await output.finish()
        self.assertEqual(struct.unpack('<hhhh', output.output.data), (3000, -3000, 26213, -26213))


if __name__ == "__main__": unittest.main()
