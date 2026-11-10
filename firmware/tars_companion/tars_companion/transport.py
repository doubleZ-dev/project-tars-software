from __future__ import annotations

import asyncio
import json
from collections import deque
from abc import ABC, abstractmethod


class LineTransport(ABC):
    @abstractmethod
    async def write_line(self, line: bytes) -> None: ...

    @abstractmethod
    async def read_line(self) -> bytes: ...

    async def close(self) -> None:  # pragma: no cover - optional hook
        return None


class MemoryTransport(LineTransport):
    def __init__(self, incoming: asyncio.Queue[bytes], outgoing: asyncio.Queue[bytes]):
        self._incoming = incoming
        self._outgoing = outgoing

    async def write_line(self, line: bytes) -> None:
        await self._outgoing.put(line.rstrip(b"\r\n"))

    async def read_line(self) -> bytes:
        return await self._incoming.get()


def memory_transport_pair() -> tuple[MemoryTransport, MemoryTransport]:
    a_to_b: asyncio.Queue[bytes] = asyncio.Queue()
    b_to_a: asyncio.Queue[bytes] = asyncio.Queue()
    return MemoryTransport(b_to_a, a_to_b), MemoryTransport(a_to_b, b_to_a)


class SerialTransport(LineTransport):
    def __init__(self, port: str, baudrate: int = 115200):
        import serial

        # Configure the modem-control lines before opening the port.  Opening a
        # CH340 with pyserial's defaults can pulse the ESP32 auto-reset/boot
        # circuit and leave the application unavailable for the handshake.
        self._serial = serial.Serial()
        self._serial.port = port
        self._serial.baudrate = baudrate
        self._serial.timeout = 0.25
        self._serial.dtr = False
        self._serial.rts = False
        self._serial.open()
        self._write_lock = asyncio.Lock()

    async def write_line(self, line: bytes) -> None:
        wire = line.rstrip(b"\r\n") + b"\n"
        async with self._write_lock:
            await asyncio.to_thread(self._serial.write, wire)
            await asyncio.to_thread(self._serial.flush)

    async def read_line(self) -> bytes:
        while True:
            line = await asyncio.to_thread(self._serial.readline)
            if line:
                return line.rstrip(b"\r\n")
            await asyncio.sleep(0)

    async def close(self) -> None:
        await asyncio.to_thread(self._serial.close)


class TransportDisconnected(ConnectionError):
    pass


class AmbiguousUsbDevice(ConnectionError):
    pass


class UsbFrameCodec:
    """Explicit USB mux. Raw boot text can never become a protocol response."""
    PREFIX = b"@TARS2 "
    LOG_PREFIX = b"@LOG2 "

    def __init__(self):
        self.buffer = bytearray()
        self.discarding = False
        self.frames: deque[bytes] = deque()
        self.logs: deque[str] = deque(maxlen=100)

    @classmethod
    def encode(cls, line: bytes) -> bytes:
        if not 0 < len(line) <= 512 or b"\n" in line or b"\r" in line:
            raise ValueError("invalid protocol line")
        return cls.PREFIX + line + b"\n"

    def feed(self, chunk: bytes) -> None:
        for byte in chunk:
            if byte == 10:
                if not self.discarding:
                    line = bytes(self.buffer).rstrip(b"\r")
                    if line.startswith(self.LOG_PREFIX):
                        self.logs.append(line[len(self.LOG_PREFIX):].decode("utf-8", "replace"))
                    elif line.startswith(self.PREFIX):
                        raw = line[len(self.PREFIX):]
                        try:
                            obj = json.loads(raw)
                        except (ValueError, UnicodeDecodeError):
                            obj = None
                        if (0 < len(raw) <= 512 and isinstance(obj, dict)
                                and obj.get("v") == 2 and isinstance(obj.get("type"), str)):
                            self.frames.append(raw)
                self.buffer.clear()
                self.discarding = False
            elif len(self.buffer) < 543 and not self.discarding:
                self.buffer.append(byte)
            else:
                self.buffer.clear()
                self.discarding = True


class UsbSerialTransport(LineTransport):
    """Lazy native USB open, pinned serial identity and bounded frame decoding.

    Link loss is reported, never hidden by resending a command. Subsequent
    reader/handshake attempts rediscover the same serial on its new COM port.
    """
    requires_session_id = True

    def __init__(self, port: str | None = None, baudrate: int = 115200, *,
                 serial_number: str | None = None):
        self.port_hint = port
        self.port_name = None
        self.baudrate = baudrate  # USB line coding only, not the bus speed.
        self.serial_number = serial_number
        self.connection_epoch = 0
        self._confirmed_epoch = 0
        self._serial = None
        self._closed = False
        self._connection_lock = asyncio.Lock()
        self._write_lock = asyncio.Lock()
        self.codec = UsbFrameCodec()

    @staticmethod
    def devices():
        from serial.tools import list_ports
        return [p for p in list_ports.comports() if (p.vid, p.pid) == (0x303A, 0x1001)]

    def _choose(self):
        candidates = self.devices()
        if self.serial_number:
            candidates = [p for p in candidates if (p.serial_number or "").casefold()
                          == self.serial_number.casefold()]
        elif self.port_hint:
            candidates = [p for p in candidates if p.device.casefold() == self.port_hint.casefold()]
        if len(candidates) > 1:
            raise AmbiguousUsbDevice("Multiple native USB devices; select --usb-serial")
        if not candidates:
            raise TransportDisconnected("Selected native USB device is absent")
        if not candidates[0].serial_number:
            raise AmbiguousUsbDevice("USB serial identity is missing; refusing unpinned connection")
        return candidates[0]

    async def _open(self):
        import serial
        async with self._connection_lock:
            if self._closed:
                raise TransportDisconnected("Transport is closed")
            if self._serial is not None:
                return self._serial
            device = await asyncio.to_thread(self._choose)
            port = serial.Serial()
            port.port = device.device
            port.baudrate = self.baudrate
            port.timeout = 0.1
            port.write_timeout = 0.5
            # Do not pulse ESP32's native USB boot/reset control signals.
            port.dtr = False
            port.rts = False
            try:
                await asyncio.to_thread(port.open)
            except (OSError, serial.SerialException) as exc:
                await asyncio.to_thread(port.close)
                raise TransportDisconnected("Native USB open failed") from exc
            self.serial_number = device.serial_number
            self.port_name = device.device
            self.codec = UsbFrameCodec()
            self._serial = port
            self.connection_epoch += 1
            self._confirmed_epoch = 0
            return port

    def confirm_session(self) -> None:
        self._confirmed_epoch = self.connection_epoch

    async def _lost(self, port) -> None:
        async with self._connection_lock:
            if self._serial is port:
                self._serial = None
                self.codec = UsbFrameCodec()
                try:
                    await asyncio.to_thread(port.close)
                except Exception:
                    pass

    async def write_line(self, line: bytes) -> None:
        wire = self.codec.encode(line)
        async with self._write_lock:
            is_probe = json.loads(line).get("type") == "session.open"
            if not is_probe and (self._serial is None or
                                 self._confirmed_epoch != self.connection_epoch):
                raise TransportDisconnected("USB session must handshake before commands")
            port = await self._open()
            try:
                written = await asyncio.to_thread(port.write, wire)
                if written != len(wire):
                    raise OSError("partial USB write")
            except Exception as exc:
                await self._lost(port)
                raise TransportDisconnected("Native USB write lost; command not replayed") from exc

    async def read_line(self) -> bytes:
        port = await self._open()
        while not self._closed and self._serial is port:
            if self.codec.frames:
                return self.codec.frames.popleft()
            try:
                chunk = await asyncio.to_thread(port.read, 256)
            except Exception as exc:
                await self._lost(port)
                raise TransportDisconnected("Native USB read lost") from exc
            if self._serial is port:
                self.codec.feed(chunk)
        raise TransportDisconnected("USB session changed")

    async def close(self) -> None:
        self._closed = True
        if self._serial is not None:
            await self._lost(self._serial)
