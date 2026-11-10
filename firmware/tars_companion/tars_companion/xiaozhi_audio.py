"""Authenticated TLS Xiaozhi v1 Opus link; TARS extension ACKs completed playback.

Wake/VAD remains local on Companion. Motion uses a separate paired control socket.
"""
import asyncio
import contextlib
import hmac
import json
import secrets
import ssl
import time
from pathlib import Path
from .opus_codec import Opus
from .wifi_audio import LanAudioConnection, STATES
from .seeduplex import BufferedRobotOutput

PARAMS = dict(format='opus', sample_rate=16000, channels=1, frame_duration=60)

class XiaozhiConnection:
    _clear_capture = LanAudioConnection._clear_capture
    read_frame = LanAudioConnection.read_frame

    def __init__(self, ws, device_id):
        self.ws, self.device_id = ws, device_id
        self.session = secrets.token_hex(16)
        self.closed = asyncio.Event()
        self.close_reason = None
        self.mode = 'idle'
        self.diagnostics = {'opus_packets': 0, 'max_rx_gap_ms': 0}
        self.frames = asyncio.Queue(maxsize=128)
        self.buffer = bytearray()
        self.pending = {}
        self.sequence = 0
        self.codec = Opus()
        self._reader_task = None

    async def start(self):
        message = json.loads(await asyncio.wait_for(self.ws.recv(), 10))
        if (message.get('type') != 'hello' or message.get('version') != 1 or
            message.get('audio_params') != PARAMS or message.get('features', {}).get('tars_cached_audio') != 1):
            raise ConnectionError('incompatible_audio_hello')
        await self.ws.send(json.dumps(dict(type='hello', session_id=self.session,
            transport='websocket', audio_params=PARAMS, tars_cached_audio=1)))
        self._reader_task = asyncio.create_task(self._receive())

    async def command(self, op, *, timeout=10, **values):
        if self.closed.is_set(): raise ConnectionError('audio_connection_closed')
        self.sequence += 1
        identity = self.sequence
        future = asyncio.get_running_loop().create_future()
        self.pending[identity] = future
        try:
            await asyncio.wait_for(self.ws.send(json.dumps(dict(type='tars.audio', session_id=self.session,
                id=identity, op=op, **values), ensure_ascii=False)), 8)
            status = await asyncio.wait_for(future, timeout)
            if status: raise ConnectionError('audio_operation_rejected_' + str(status))
        except BaseException:
            # Never repeat an uncertain capture/play command on a fresh connection.
            await self.close()
            raise
        finally:
            self.pending.pop(identity, None)

    async def _receive(self):
        last = time.monotonic()
        try:
            async for message in self.ws:
                now = time.monotonic()
                self.diagnostics['max_rx_gap_ms'] = max(self.diagnostics['max_rx_gap_ms'], round((now-last)*1000))
                last = now
                if isinstance(message, bytes):
                    if self.mode != 'capture': raise ConnectionError('unexpected_opus')
                    self.frames.put_nowait(self.codec.decode(message))
                    self.diagnostics['opus_packets'] += 1
                else:
                    item = json.loads(message)
                    if item.get('type') != 'tars.ack' or item.get('session_id') != self.session:
                        raise ConnectionError('invalid_audio_ack')
                    future = self.pending.get(item.get('id'))
                    if future is None or future.done(): raise ConnectionError('stale_audio_ack')
                    future.set_result(item.get('status', -1))
        except asyncio.CancelledError:
            pass
        except Exception as error:
            self.close_reason = type(error).__name__
        finally:
            await self.close()

    async def start_capture(self):
        if self.mode != 'idle': raise RuntimeError('half_duplex_busy')
        self._clear_capture()
        # Encoder resets per utterance; matching decoder state is also fresh.
        self.codec.close(); self.codec = Opus()
        self.mode = 'capture'
        await self.command('capture_start')

    async def stop_capture(self):
        if self.mode == 'capture':
            try: await self.command('capture_stop')
            finally:
                self.mode = 'idle'; self._clear_capture()

    async def interaction(self, state):
        await self.command('state', value=STATES.index(state))

    async def display_dialogue(self, role, text):
        from .client import TarsClient
        await self.command('text', role=role, value=TarsClient._fit_dialogue_text(' '.join(text.split())))

    async def close(self):
        if self.closed.is_set(): return
        self.closed.set()
        self.mode = 'idle'; self._clear_capture()
        for future in self.pending.values():
            if not future.done(): future.set_exception(ConnectionError('audio_link_lost_outcome_unknown'))
        if self._reader_task and self._reader_task is not asyncio.current_task():
            self._reader_task.cancel()
        with contextlib.suppress(Exception): await self.ws.close()
        self.codec.close()

class XiaozhiServer:
    def __init__(self, token, *, device_id='84:c7:bb:77:b5:f0', tls_dir='data/tls'):
        self.token, self.device_id = token, device_id
        self.tls_dir = Path(tls_dir)
        self.current = None
        self.connected = asyncio.Event()
        self._server = None
        self.accepting = False

    async def start(self, host, port=8766):
        from websockets.asyncio.server import serve
        tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        tls.minimum_version = ssl.TLSVersion.TLSv1_2
        tls.load_cert_chain(self.tls_dir/'companion.pem', self.tls_dir/'companion-key.pem')
        self._server = await serve(self._accept, host, port, ssl=tls, compression=None,
            ping_interval=2, ping_timeout=8, close_timeout=1, max_size=1536, max_queue=128,
            process_request=self._authenticate)

    async def _authenticate(self, connection, request):
        try:
            valid = (request.path == '/tars' and
                hmac.compare_digest(request.headers.get('Authorization', ''), 'Bearer ' + self.token) and
                request.headers.get('Device-Id', '').lower() == self.device_id)
        except Exception: valid = False
        if not valid: return connection.respond(403, 'Pairing required')

    async def _accept(self, ws):
        if self.accepting:
            await ws.close(1013, 'Already connected'); return
        self.accepting = True
        link = None
        try:
            link = XiaozhiConnection(ws, self.device_id)
            await link.start()
            self.current = link; self.connected.set()
            await link.closed.wait()
        except Exception:
            pass
        finally:
            if link: await link.close()
            self.current = None; self.connected.clear(); self.accepting = False

    async def wait_connected(self, timeout=30):
        async with asyncio.timeout(timeout):
            while True:
                await self.connected.wait()
                if self.current and not self.current.closed.is_set(): return self.current
                await asyncio.sleep(.01)

    async def close(self):
        if self.current: await self.current.close()
        if self._server:
            self._server.close(); await self._server.wait_closed()

class XiaozhiOutput(BufferedRobotOutput):
    async def finish(self):
        try:
            for offset in range(0, len(self.buffer), 256000):
                if self.link.mode != 'idle': raise RuntimeError('half_duplex_busy')
                self.link.mode = 'buffered_playback'
                segment = bytes(self.buffer[offset:offset+256000])
                await self.link.command('play_begin', samples=len(segment)//2, volume=self.volume)
                codec = Opus()
                try:
                    for i in range(0, len(segment), 1920):
                        packet = codec.encode(segment[i:i+1920].ljust(1920, b'\0'))
                        await asyncio.wait_for(self.link.ws.send(packet), 8)
                finally: codec.close()
                await self.link.command('play_commit', timeout=18)
                self.link.mode = 'idle'
                self.bytes_sent += len(segment)
            self.buffer.clear()
        except BaseException:
            await self.link.close(); raise
