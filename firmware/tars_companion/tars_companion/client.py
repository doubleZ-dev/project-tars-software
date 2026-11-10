from __future__ import annotations

import asyncio
import json
import secrets
import unicodedata
from dataclasses import dataclass
from typing import Any

from .transport import LineTransport, TransportDisconnected


TERMINAL_TYPES = {"command.completed", "command.failed", "command.cancelled"}
DIALOGUE_TEXT_MAX_BYTES = 300
DIALOGUE_JSON_STRING_MAX_BYTES = 300


class DeviceRestartedError(ConnectionError):
    pass


@dataclass(frozen=True)
class CommandResult:
    command_id: str
    accepted: bool
    terminal_type: str
    message: dict[str, Any]

    @property
    def succeeded(self) -> bool:
        return self.terminal_type == "command.completed"


@dataclass
class _Pending:
    ack: asyncio.Future[dict[str, Any]]
    terminal: asyncio.Future[dict[str, Any]]
    wire: bytes


class TarsClient:
    def __init__(self, transport: LineTransport, *, heartbeat_interval: float = 0.5):
        self.transport = transport
        self._pending: dict[str, _Pending] = {}
        self._reader_task: asyncio.Task[None] | None = None
        self._heartbeat_task: asyncio.Task[None] | None = None
        self._heartbeat_interval = heartbeat_interval
        self.hello: dict[str, Any] | None = None
        self.boot_id: str | None = None
        self._hello_ready = asyncio.Event()
        self._start_lock = asyncio.Lock()
        self._session_nonce = self.new_command_id()
        self._generation = 0
        self._closed = False

    def _invalidate_session(self, error: ConnectionError) -> None:
        if self.boot_id is not None or self._hello_ready.is_set():
            self._generation += 1
            self._session_nonce = self.new_command_id()
        self.boot_id = None
        self.hello = None
        self._hello_ready.clear()
        for pending in self._pending.values():
            for future in (pending.ack, pending.terminal):
                if not future.done():
                    future.set_exception(error)

    async def _probe(self) -> None:
        probe = json.dumps({"v": 2, "type": "session.open",
                            "command_id": self.new_command_id(),
                            "payload": {"session_id": self._session_nonce}},
                           separators=(",", ":")).encode()
        await self.transport.write_line(probe)

    async def start(self) -> None:
        async with self._start_lock:
            if self._closed:
                raise ConnectionError("client is closed")
            if self._reader_task is None:
                self._reader_task = asyncio.create_task(self._reader(), name="tars-protocol-reader")
            self._ensure_heartbeat()
            if self._hello_ready.is_set():
                self._ensure_heartbeat()
                return
            attempts = 15 if getattr(self.transport, "requires_session_id", False) else 3
            for attempt in range(attempts):
                try:
                    await self._probe()
                    await asyncio.wait_for(self._hello_ready.wait(), 0.4)
                    self._ensure_heartbeat()
                    return
                except (TimeoutError, TransportDisconnected):
                    if attempt == attempts - 1:
                        raise TimeoutError("device hello timeout") from None
                    await asyncio.sleep(0.1)

    async def close(self) -> None:
        self._closed = True
        if self._heartbeat_task:
            self._heartbeat_task.cancel()
            await asyncio.gather(self._heartbeat_task, return_exceptions=True)
            self._heartbeat_task = None
        if self._reader_task:
            self._reader_task.cancel()
            await asyncio.gather(self._reader_task, return_exceptions=True)
        self._invalidate_session(TransportDisconnected("client closed"))
        await self.transport.close()

    @staticmethod
    def new_command_id() -> str:
        return f"pc-{secrets.token_hex(6)}"

    async def command(
        self,
        command_type: str,
        payload: dict[str, Any],
        *,
        command_id: str | None = None,
        ack_timeout: float = 0.4,
        terminal_timeout: float = 30.0,
        retries: int = 2,
    ) -> CommandResult:
        await self.start()
        command_id = command_id or self.new_command_id()
        if not self.boot_id:
            raise ConnectionError("device boot_id unavailable")
        envelope = {"v": 2, "type": command_type, "target_boot_id": self.boot_id,
                    "command_id": command_id, "payload": payload}
        if getattr(self.transport, "requires_session_id", False):
            envelope["session_id"] = self._session_nonce
        wire = json.dumps(envelope, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        if len(wire) > 512:
            raise ValueError("protocol line exceeds 512 bytes")
        loop = asyncio.get_running_loop()
        pending = _Pending(loop.create_future(), loop.create_future(), wire)
        for future in (pending.ack, pending.terminal):
            future.add_done_callback(lambda f: None if f.cancelled() else f.exception())
        generation = self._generation
        if command_id in self._pending:
            raise ValueError(f"command already pending: {command_id}")
        self._pending[command_id] = pending
        try:
            ack: dict[str, Any] | None = None
            for attempt in range(retries + 1):
                if generation != self._generation or not self._hello_ready.is_set():
                    raise TransportDisconnected("session changed; command not replayed")
                try:
                    await self.transport.write_line(wire)
                except ConnectionError as exc:
                    self._invalidate_session(exc)
                    raise
                try:
                    ack = await asyncio.wait_for(asyncio.shield(pending.ack), ack_timeout)
                    break
                except TimeoutError:
                    if attempt == retries:
                        raise TimeoutError(f"ACK timeout for {command_id}") from None
            assert ack is not None
            if ack.get("state") == "rejected":
                return CommandResult(command_id, False, "command.rejected", ack)
            terminal = await asyncio.wait_for(asyncio.shield(pending.terminal), terminal_timeout)
            return CommandResult(command_id, True, terminal["type"], terminal)
        finally:
            self._pending.pop(command_id, None)

    async def execute(self, name: str, steps: int = 1, speed_pct: int = 50,
                      support_confirm: bool = False, **kwargs: Any) -> CommandResult:
        payload = {"name": name, "steps": steps, "speed_pct": speed_pct}
        if support_confirm:
            payload['support_confirm'] = True
        return await self.command("motion.execute", payload, **kwargs)

    async def cancel(self, target_command_id: str, **kwargs: Any) -> CommandResult:
        return await self.command("motion.cancel", {"target_command_id": target_command_id}, **kwargs)

    async def estop(self, **kwargs: Any) -> CommandResult:
        return await self.command("safety.estop", {}, **kwargs)

    async def clear_estop(self, **kwargs: Any) -> CommandResult:
        return await self.command("safety.clear", {}, **kwargs)

    async def arm(self, **kwargs: Any) -> CommandResult:
        return await self.command("safety.arm", {}, **kwargs)

    async def disarm(self, **kwargs: Any) -> CommandResult:
        return await self.command("safety.disarm", {}, **kwargs)

    async def state(self, **kwargs: Any) -> CommandResult:
        return await self.command("state.get", {}, **kwargs)

    async def diagnostics(self, view: str = "boot", **kwargs: Any) -> CommandResult:
        if view not in {"boot", "current", "previous"}:
            raise ValueError("invalid diagnostic view")
        return await self.command("diagnostic.get", {"view": view}, **kwargs)

    async def interaction(self, state: str, **kwargs: Any) -> CommandResult:
        if state not in {"IDLE", "LISTENING", "THINKING", "SPEAKING"}:
            raise ValueError("invalid interaction state")
        return await self.command("interaction.set", {"state": state}, **kwargs)

    async def display_dialogue(self, role: str, text: str, **kwargs: Any) -> CommandResult:
        """Append safe, bounded UTF-8 dialogue to the robot's right display."""
        if role not in {"user", "tars"}:
            raise ValueError("dialogue role must be 'user' or 'tars'")
        if not isinstance(text, str):
            raise TypeError("dialogue text must be str")
        text = " ".join(text.replace("\r", " ").replace("\n", " ").replace("\t", " ").split())
        if not text:
            raise ValueError("dialogue text must not be empty")
        if any(unicodedata.category(char) == "Cc" for char in text):
            raise ValueError("dialogue text contains a control character")
        text = self._fit_dialogue_text(text)
        return await self.command("dialogue.append", {"role": role, "text": text}, **kwargs)

    @staticmethod
    def _fit_dialogue_text(text: str) -> str:
        def fits(value: str) -> bool:
            return (len(value.encode("utf-8")) <= DIALOGUE_TEXT_MAX_BYTES and
                    len(json.dumps(value, ensure_ascii=False).encode("utf-8")) <=
                    DIALOGUE_JSON_STRING_MAX_BYTES)

        if fits(text):
            return text
        kept: list[str] = []
        for char in text:
            candidate = "".join(kept) + char + "…"
            if not fits(candidate):
                break
            kept.append(char)
        clipped = "".join(kept).rstrip() + "…"
        if not kept or not fits(clipped):
            raise ValueError("dialogue text cannot fit protocol envelope")
        return clipped

    def _ensure_heartbeat(self) -> None:
        if self._heartbeat_task is None:
            self._heartbeat_task = asyncio.create_task(self._heartbeat(), name="tars-control-lease")

    async def _heartbeat(self) -> None:
        pending = set()
        try:
            await self._heartbeat_loop(pending)
        finally:
            for task in pending: task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)

    async def _heartbeat_loop(self, pending) -> None:
        while True:
            await asyncio.sleep(self._heartbeat_interval)
            for task in list(pending):
                if task.done():
                    pending.remove(task)
                    try: task.result()
                    except (ConnectionError, TimeoutError) as exc:
                        self._invalidate_session(exc)
            if len(pending) >= 4:
                self._invalidate_session(TransportDisconnected('keepalive_backlog'))
                await self.transport.close()
                return
            if not self.boot_id:
                try:
                    await self._probe()
                except ConnectionError:
                    pass
                continue
            envelope = {"v": 2, "type": "session.keepalive", "target_boot_id": self.boot_id,
                        "command_id": "pc-lease", "payload": {}}
            if getattr(self.transport, "requires_session_id", False):
                envelope["session_id"] = self._session_nonce
            wire = json.dumps(envelope, separators=(",", ":")).encode("utf-8")
            # Send on the lease cadence, not ACK round-trip + cadence. Only
            # idempotent keepalives overlap; motion requests are never replayed.
            pending.add(asyncio.create_task(self.transport.write_line(wire)))

    async def _reader(self) -> None:
        while True:
            try:
                line = await self.transport.read_line()
            except ConnectionError as exc:
                self._invalidate_session(exc)
                await asyncio.sleep(0.1)
                continue
            if len(line) > 512:
                continue
            try:
                message = json.loads(line)
            except (UnicodeDecodeError, json.JSONDecodeError):
                continue
            if not isinstance(message, dict) or message.get("v") != 2:
                continue
            if message.get("type") == "hello":
                new_boot_id = message.get("boot_id")
                payload = message.get("payload", {})
                if (not isinstance(payload, dict) or not isinstance(new_boot_id, str)
                        or len(new_boot_id) != 16):
                    continue
                if self.boot_id and new_boot_id and new_boot_id != self.boot_id:
                    error = DeviceRestartedError(f"device restarted: {self.boot_id} -> {new_boot_id}")
                    self._invalidate_session(error)
                    if getattr(self.transport, "requires_session_id", False):
                        continue
                if (getattr(self.transport, "requires_session_id", False) and
                        payload.get("session_id") != self._session_nonce):
                    continue
                if new_boot_id:
                    self.boot_id = new_boot_id
                    confirm = getattr(self.transport, "confirm_session", None)
                    if confirm:
                        confirm()
                    self._hello_ready.set()
                self.hello = message
                continue
            response_boot_id = message.get("boot_id")
            if self.boot_id and response_boot_id and response_boot_id != self.boot_id:
                self._invalidate_session(DeviceRestartedError("device rebooted; pending commands unknown"))
                continue
            if (getattr(self.transport, "requires_session_id", False) and
                    message.get("session_id") != self._session_nonce):
                continue
            error = message.get("error", {})
            if isinstance(error, dict) and error.get("code") == "session_required":
                self._invalidate_session(TransportDisconnected("device requires a new USB session"))
                continue
            command_id = message.get("command_id")
            if not isinstance(command_id, str):
                continue
            pending = self._pending.get(command_id)
            if not pending:
                continue
            msg_type = message.get("type")
            if msg_type == "command.ack" and not pending.ack.done():
                pending.ack.set_result(message)
            elif msg_type in TERMINAL_TYPES and not pending.terminal.done():
                if not pending.ack.done():
                    pending.ack.set_result({"type": "command.ack", "state": "accepted", "command_id": command_id})
                pending.terminal.set_result(message)
