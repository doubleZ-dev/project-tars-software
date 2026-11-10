"""TWA1 paired LAN audio plus framed existing JSON v2 control.

Wire header !4sBBHII: magic, kind, reserved=0, payload length <=1024,
connection session ID, per-direction monotonic sequence. PCM16 mono 16kHz LE.
Mutual nonce HMAC authenticates pairing, NOT encryption. Trusted WPA2 LAN only.
"""
from __future__ import annotations
import asyncio
import hashlib
import hmac
import ipaddress
import json
import re
import struct
import time
from pathlib import Path
from .audio import ASR_PCM, PcmFormat
from .client import TarsClient
from .transport import LineTransport

HELLO, AUTH, READY, ACK, ERROR, PING = 1, 2, 3, 4, 5, 6
CAP_START, CAP_STOP, MIC_PCM = 10, 11, 12
PLAY_START, PLAY_PCM, PLAY_END = 20, 21, 22
STATE, TEXT = 30, 31
CONTROL_JSON, CONTROL_RESULT = 40, 41
STATES = ("IDLE", "LISTENING", "THINKING", "SPEAKING")
HEADER = struct.Struct("!4sBBHII")
LIMIT = 1024


def load_profile(path: str) -> dict:
    value = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    if not isinstance(value, dict):
        raise ValueError("invalid Wi-Fi profile")
    if not isinstance(value.get("ssid"), str) or not 1 <= len(value["ssid"].encode()) <= 32:
        raise ValueError("SSID must contain 1..32 UTF-8 bytes")
    if not isinstance(value.get("password"), str) or not 8 <= len(value["password"].encode()) <= 63:
        raise ValueError("WPA2 password must contain 8..63 UTF-8 bytes")
    try:
        ipaddress.IPv4Address(value["host"])
    except (ValueError, KeyError, TypeError):
        raise ValueError("host must be the PC IPv4 address") from None
    if type(value.get("port")) is not int or not 1024 <= value["port"] <= 65535:
        raise ValueError("port must be 1024..65535")
    validate_token(value.get("token"))
    return {key: value[key] for key in ("ssid", "password", "host", "port", "token")}


def validate_token(token: str):
    if not isinstance(token, str) or not re.fullmatch(r"[0-9a-f]{32,64}", token):
        raise ValueError("pairing token must be 32..64 lowercase hexadecimal characters")


def proof(token: str, role: bytes, hello: bytes, session: int) -> bytes:
    return hmac.new(token.encode(), role + hello + struct.pack("!I", session), hashlib.sha256).digest()


def encode(kind: int, session: int, sequence: int, payload: bytes = b"") -> bytes:
    if len(payload) > LIMIT or not 0 <= sequence < 0xffffffff or not 0 < session <= 0xffffffff:
        raise ValueError("invalid TWA1 frame")
    return HEADER.pack(b"TWA1", kind, 0, len(payload), session, sequence) + payload


async def read_frame(reader: asyncio.StreamReader):
    header = await reader.readexactly(HEADER.size)
    magic, kind, flags, size, session, sequence = HEADER.unpack(header)
    if magic != b"TWA1" or flags or size > LIMIT or not session or sequence == 0xffffffff:
        raise ConnectionError("invalid_audio_frame")
    return kind, session, sequence, await reader.readexactly(size)


class LanAudioConnection:
    def __init__(self, reader, writer, session: int, hello: bytes, *, capture_queue_packets=32):
        if not 32 <= capture_queue_packets <= 256:
            raise ValueError('invalid_capture_queue_capacity')
        self.reader, self.writer = reader, writer
        self.session = session
        self.device_id = hello[32:].hex(":")
        self.closed = asyncio.Event()
        self.close_reason = None
        self.diagnostics = {'max_ack_ms': 0, 'max_rx_gap_ms': 0, 'max_heartbeat_lag_ms': 0}
        self._last_rx_at = time.monotonic()
        self.mode = "idle"
        self.tx_sequence, self.rx_sequence = 1, 2  # AUTH already sent, HELLO/READY received
        self.pending = {}
        self.frames = asyncio.Queue(maxsize=capture_queue_packets)
        self.control = asyncio.Queue(maxsize=32)
        self.buffer = bytearray()
        self.write_lock = asyncio.Lock()
        self._reader_task = asyncio.create_task(self._receive())
        self._heartbeat_task = asyncio.create_task(self._heartbeat())

    async def command(self, kind: int, payload: bytes = b"", *, timeout=2):
        if self.closed.is_set():
            raise ConnectionError("audio_connection_closed")
        async with self.write_lock:
            sequence = self.tx_sequence
            wire = encode(kind, self.session, sequence, payload)
            self.tx_sequence += 1
            future = asyncio.get_running_loop().create_future()
            future.add_done_callback(lambda f: None if f.cancelled() else f.exception())
            self.pending[sequence] = future
            sent_at = time.monotonic()
            self.writer.write(wire)
            try:
                await asyncio.wait_for(self.writer.drain(), 1)
            except BaseException:
                await self.close()
                raise
        try:
            status = await asyncio.wait_for(asyncio.shield(future), timeout)
            if kind != 25:  # cached playback ACK deliberately waits for acoustic duration
                self.diagnostics['max_ack_ms'] = max(self.diagnostics['max_ack_ms'], round((time.monotonic()-sent_at)*1000))
            if status:
                self.close_reason = f'audio_request_rejected_kind_{kind}_status_{status}'
                raise ConnectionError("audio_request_rejected")
        except BaseException as error:
            if self.close_reason is None:
                self.close_reason = f'audio_command_kind_{kind}_{type(error).__name__}'
            await self.close()
            raise
        finally:
            self.pending.pop(sequence, None)

    async def _receive(self):
        try:
            while True:
                kind, session, sequence, payload = await asyncio.wait_for(read_frame(self.reader), 3)
                now = time.monotonic()
                self.diagnostics['max_rx_gap_ms'] = max(self.diagnostics['max_rx_gap_ms'], round((now-self._last_rx_at)*1000))
                self._last_rx_at = now
                if session != self.session or sequence != self.rx_sequence:
                    raise ConnectionError("audio_session_or_sequence_mismatch")
                self.rx_sequence += 1
                if kind == ACK and len(payload) == 5:
                    request, status = struct.unpack("!IB", payload)
                    future = self.pending.get(request)
                    if future is None or future.done():
                        raise ConnectionError("unexpected_audio_ack")
                    future.set_result(status)
                elif kind == CONTROL_RESULT and 0 < len(payload) <= 512:
                    self.control.put_nowait(payload)
                elif kind == MIC_PCM and payload and not len(payload) % 2 and self.mode == "capture":
                    try:
                        self.frames.put_nowait(payload)
                    except asyncio.QueueFull:
                        raise ConnectionError("microphone_buffer_overflow") from None
                else:
                    raise ConnectionError("unexpected_audio_frame")
        except asyncio.CancelledError:
            await self.close()
        except Exception as error:
            codes = {'invalid_audio_frame', 'audio_session_or_sequence_mismatch',
                     'unexpected_audio_ack', 'microphone_buffer_overflow', 'unexpected_audio_frame'}
            if self.close_reason is None:
                self.close_reason = str(error) if str(error) in codes else type(error).__name__
            await self.close()

    async def _heartbeat(self):
        pending = set()
        try:
            while True:
                expected = time.monotonic() + .5
                await asyncio.sleep(.5)
                self.diagnostics['max_heartbeat_lag_ms'] = max(self.diagnostics['max_heartbeat_lag_ms'], round((time.monotonic()-expected)*1000))
                for task in list(pending):
                    if task.done():
                        pending.remove(task)
                        task.result()
                if len(pending) >= 4: raise ConnectionError('heartbeat_backlog')
                pending.add(asyncio.create_task(self.command(PING)))
        except (Exception, asyncio.CancelledError) as error:
            if self.close_reason is None and not isinstance(error, asyncio.CancelledError):
                self.close_reason = "heartbeat_" + type(error).__name__
            await self.close()
        finally:
            for task in pending: task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)

    def _clear_capture(self):
        self.buffer.clear()
        while not self.frames.empty():
            self.frames.get_nowait()

    async def start_capture(self):
        if self.mode != "idle":
            raise RuntimeError("half_duplex_busy")
        self._clear_capture()
        self.mode = "capture"  # RX may arrive immediately after ACK
        try:
            await self.command(CAP_START)
        except BaseException:
            self.mode = "idle"
            raise

    async def read_frame(self) -> bytes:
        while len(self.buffer) < ASR_PCM.frame_bytes:
            if self.closed.is_set() or self.mode != "capture":
                raise ConnectionError("capture_interrupted")
            try:
                payload = await asyncio.wait_for(self.frames.get(), .1)
            except TimeoutError:
                continue
            self.buffer.extend(payload)
        result = bytes(self.buffer[:ASR_PCM.frame_bytes])
        del self.buffer[:ASR_PCM.frame_bytes]
        return result

    async def stop_capture(self):
        if self.mode == "capture":
            try:
                await self.command(CAP_STOP)
            finally:
                self.mode = "idle"
                self._clear_capture()

    async def play(self, pcm: bytes, pcm_format: PcmFormat, cancel: asyncio.Event, volume_pct: int = 5):
        if self.mode != "idle":
            raise RuntimeError("half_duplex_busy")
        if (pcm_format.sample_rate, pcm_format.channels, pcm_format.sample_width) != (16000, 1, 2):
            raise ValueError("LAN audio requires 16kHz PCM16 mono; resample in the provider adapter")
        if not pcm or len(pcm) % 2 or len(pcm) > 320000 or not 0 <= volume_pct <= 20:
            raise ValueError("invalid playback size or volume")
        self.mode = "playback"
        try:
            await self.command(PLAY_START, bytes([volume_pct]))
            for offset in range(0, len(pcm), 512):
                if cancel.is_set():
                    break
                # One acknowledged chunk outstanding bounds board buffering and
                # provides backpressure. Never retry/replay uncertain PCM.
                await self.command(PLAY_PCM, pcm[offset:offset + 512])
            await self.command(PLAY_END)
        except BaseException:
            await self.close()  # board heartbeat timeout/connection loss mutes
            raise
        finally:
            self.mode = "idle"

    async def interaction(self, state: str):
        if state not in STATES:
            raise ValueError("invalid interaction")
        await self.command(STATE, bytes([STATES.index(state)]))

    async def display_dialogue(self, role: str, text: str):
        if role not in {"user", "tars"}:
            raise ValueError("invalid dialogue role")
        text = TarsClient._fit_dialogue_text(" ".join(text.split()))
        if not text or any(ord(c) < 32 or 127 <= ord(c) <= 159 for c in text):
            raise ValueError("invalid dialogue text")
        await self.command(TEXT, bytes([role == "tars"]) + text.encode())

    async def close(self):
        if self.closed.is_set():
            return
        self.closed.set()
        self.mode = "idle"
        self._clear_capture()
        for future in self.pending.values():
            if not future.done():
                future.set_exception(ConnectionError("audio_link_lost_outcome_unknown"))
        current = asyncio.current_task()
        for task in (self._reader_task, self._heartbeat_task):
            if task is not current:
                task.cancel()
        self.writer.close()
        try:
            await asyncio.wait_for(self.writer.wait_closed(), .5)
        except Exception:
            pass


class NetworkJsonTransport(LineTransport):
    """One connection, one fresh TarsClient session. Never reconnect/replay here."""
    requires_session_id = True

    def __init__(self, link: LanAudioConnection):
        self.link = link

    async def write_line(self, line: bytes):
        payload = line.rstrip(b"\r\n")
        if not 0 < len(payload) <= 512:
            raise ValueError("invalid control frame size")
        await self.link.command(CONTROL_JSON, payload)

    async def read_line(self):
        while not self.link.closed.is_set():
            try:
                return await asyncio.wait_for(self.link.control.get(), .1)
            except TimeoutError:
                continue
        raise ConnectionError("wireless_control_disconnected")

    async def close(self):
        await self.link.close()


class LanAudioServer:
    def __init__(self, token: str, *, capture_queue_packets=32):
        validate_token(token)
        self._token = token
        self.capture_queue_packets = capture_queue_packets
        self.current = None
        self._accepting = False
        self._server = None
        self.connected = asyncio.Event()
        self.last_error = None

    async def start(self, host="0.0.0.0", port=8765):
        self._server = await asyncio.start_server(self._accept, host, port, backlog=4)
        return self._server.sockets[0].getsockname()[1]

    async def _accept(self, reader, writer):
        if self._accepting or self.current is not None:
            writer.close()
            return
        self._accepting = True
        connection = None
        try:
            kind, session, sequence, hello = await asyncio.wait_for(read_frame(reader), 2)
            if kind != HELLO or sequence != 0 or len(hello) != 38:
                raise ConnectionError("invalid_audio_hello")
            writer.write(encode(AUTH, session, 0, proof(self._token, b"PC", hello, session)))
            await asyncio.wait_for(writer.drain(), 1)
            kind, received_session, sequence, payload = await asyncio.wait_for(read_frame(reader), 2)
            if (kind, received_session, sequence) != (READY, session, 1) or not hmac.compare_digest(
                    payload, proof(self._token, b"ESP", hello, session)):
                raise ConnectionError("audio_pairing_failed")
            connection = LanAudioConnection(reader, writer, session, hello,
                                            capture_queue_packets=self.capture_queue_packets)
            self.current = connection
            self.connected.set()
            await connection.closed.wait()
        except (Exception, asyncio.CancelledError):
            self.last_error = "audio_connection_or_pairing_failed"
        finally:
            if connection:
                await connection.close()
            writer.close()
            if self.current is connection:
                self.current = None
                self.connected.clear()
            self._accepting = False

    async def wait_connected(self, timeout: float = 30) -> LanAudioConnection:
        async with asyncio.timeout(timeout):
            while True:
                await self.connected.wait()
                if self.current is not None and not self.current.closed.is_set():
                    return self.current
                # Cleanup may still be clearing the previous session event.
                await asyncio.sleep(.01)

    async def close(self):
        if self.current:
            await self.current.close()
        if self._server:
            self._server.close()
            await self._server.wait_closed()
