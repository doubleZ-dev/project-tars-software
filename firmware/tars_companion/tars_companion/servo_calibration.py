"""Loopback-only, manual single-servo calibration. No Brain/LLM imports."""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import secrets
import time
from datetime import datetime, timezone
from pathlib import Path

from .client import TarsClient
from .transport import UsbSerialTransport, memory_transport_pair
from .simulator import SimulatedTarsDevice

SERVO_POSITIONS = {1: "左侧外腿", 4: "右侧外腿"}  # Legacy numbering retained; central pair 2/3 left-right positions await confirmation.
WEB_LEASE = 0.9
STATIC = Path(__file__).with_name("servo_web")


class CalibrationBridge:
    def __init__(self, mock=False):
        self.mock = mock
        self.client = None
        self.device = None
        self.owner = None
        self.last_ping = 0.0
        self.lease_id = None
        self.boot_id = None
        self.generation = None
        self.status = {"active": False, "servo": 0}
        self.connection = "未连接"
        self.records = []
        self.last_target = None
        self.lock = asyncio.Lock()

    def snapshot(self):
        return {"mock": self.mock, "connection": self.connection,
                "connected": self.client is not None and self.client.boot_id == self.boot_id,
                "status": self.status, "last_target": self.last_target,
                "records": self.records, "actual_angle": None,
                "servo_positions": SERVO_POSITIONS}

    async def command(self, name, payload):
        if not self.client:
            raise ValueError("请先连接 USB")
        if (self.client.boot_id != self.boot_id or self.client._generation != self.generation):
            await self.drop()
            raise ConnectionError("USB 会话变化，请手动重新连接并重新确认")
        try:
            result = await self.client.command(name, payload, terminal_timeout=1.0)
            if not result.succeeded:
                raise ValueError(result.message.get("error", {}).get("code", "设备拒绝命令"))
            data = result.message.get("payload", {}).get("result")
            if name.startswith("calibration.") and data is not None:
                self.status = data
                if not data.get("active"):
                    self.lease_id = None
                    self.last_target = None
            return result
        except (ConnectionError, TimeoutError):
            await self.drop()
            raise

    async def drop(self):
        client, self.client = self.client, None
        self.lease_id = None
        self.last_target = None
        self.status = {"active": False, "servo": 0, "connection_error": True}
        self.connection = "连接异常（执行结果未知，等待固件超时关闭）"
        if client:
            await client.close()
        if self.device:
            await self.device.close()
            self.device = None

    async def stop(self):
        self.lease_id = None
        self.last_target = None
        if self.client:
            await self.command("calibration.end", {})

    async def watchdog(self):
        while True:
            await asyncio.sleep(0.05)
            async with self.lock:
                if self.owner and time.monotonic() - self.last_ping > WEB_LEASE:
                    self.owner = None
                    try:
                        await self.stop()
                    except (ConnectionError, TimeoutError, ValueError):
                        self.status = {"connection_error": True}
                    else:
                        if not self.status.get("connection_error"):
                            self.status = {"active": False, "servo": 0, "timed_out": True}
                if self.client and (self.client.boot_id != self.boot_id or
                                    self.client._generation != self.generation):
                    await self.drop()

    async def handle(self, action, data):
        async with self.lock:
            if action == "claim":
                if self.owner and time.monotonic() - self.last_ping > WEB_LEASE:
                    self.owner = None
                    await self.stop()
                if self.owner:
                    raise ValueError("已有网页持有控制权；请关闭旧页面或等待其租约释放")
                self.owner = secrets.token_hex(24)
                self.last_ping = time.monotonic()
                return {"owner": self.owner, **self.snapshot()}
            if not self.owner or not secrets.compare_digest(str(data.get("owner", "")), self.owner):
                raise ValueError("网页没有控制权，请点击获取网页控制权")
            if time.monotonic() - self.last_ping > WEB_LEASE:
                self.owner = None
                await self.stop()
                raise ValueError("网页心跳超时，请重新获取控制权")
            if action == "ping":
                self.last_ping = time.monotonic()
                if self.client:
                    if self.lease_id:
                        await self.command("calibration.renew", {"calibration_id": self.lease_id})
                    else:
                        await self.command("calibration.status", {})
            elif action == "connect":
                if self.client:
                    raise ValueError("已有连接，请先断开")
                self.lease_id = None
                self.last_target = None
                if self.mock:
                    pc, body = memory_transport_pair()
                    self.device = SimulatedTarsDevice(body, native_session_required=True)
                    pc.requires_session_id = True
                    await self.device.start()
                    self.client = TarsClient(pc)
                else:
                    serial = data.get("serial")
                    if not isinstance(serial, str) or not serial or len(serial) > 128:
                        raise ValueError("请选择带序列号的原生 USB 设备")
                    self.client = TarsClient(UsbSerialTransport(serial_number=serial))
                try:
                    await self.client.start()
                    self.boot_id = self.client.boot_id
                    self.generation = self.client._generation
                    await self.command("calibration.end", {})
                    self.connection = "Mock 已连接（无硬件）" if self.mock else "USB 已握手连接"
                except Exception:
                    await self.drop()
                    raise
            elif action == "disconnect":
                try:
                    await self.stop()
                finally:
                    await self.drop()
                self.connection = "未连接"
                self.status = {"active": False, "servo": 0}
            elif action == "begin":
                servo = data.get("servo")
                if data.get("confirm") is not True or type(servo) is not int or servo not in range(1, 5):
                    raise ValueError("必须明确确认并选择 Servo1–4")
                await self.stop()
                lease = TarsClient.new_command_id()
                result = await self.command("calibration.begin", {"servo": servo, "confirm": True, "calibration_id": lease})
                self.lease_id = lease
                self.last_target = self.target_record(result)
            elif action == "set":
                angle = data.get("angle_deg")
                if type(angle) is not int or not -20 <= angle <= 20:
                    raise ValueError("角度必须为 −20～+20 的整数")
                if not self.lease_id or data.get("servo") != self.status.get("servo"):
                    raise ValueError("请明确使能所选舵机")
                result = await self.command("calibration.set", {"calibration_id": self.lease_id, "angle_deg": angle})
                self.last_target = self.target_record(result)
            elif action == "mark":
                await self.command("calibration.status", {})
                if (not self.status.get("active") or not self.last_target or
                        data.get("servo") != self.status.get("servo") or data.get("confirm") is not True):
                    raise ValueError("仅可记录已获设备完成响应且由用户确认的当前目标")
                self.records.append({**self.last_target, "user_confirmed_mechanical_zero_deg": self.last_target["calibration_angle_deg"],
                                     "confirmed_at": datetime.now(timezone.utc).isoformat(), "actual_angle_deg": None})
            elif action == "off":
                self.lease_id = None
                self.last_target = None
                await self.command("safety.disarm", {})
                await self.command("calibration.status", {})
            elif action == "estop":
                self.lease_id = None
                self.last_target = None
                await self.command("safety.estop", {})
                await self.command("calibration.status", {})
            elif action == "clear":
                await self.command("safety.clear", {})
                await self.command("calibration.status", {})
            elif action == "release":
                self.owner = None
                await self.stop()
            elif action == "export":
                return {"schema_version": 1, "mock": self.mock, "mapping": "500 + (angle_deg + 90) * 2000 // 180",
                        "reference_midpoint_deg": 0, "reference_midpoint_us": 1500,
                        "actual_angle_deg": None, "records": self.records}
            else:
                raise ValueError("未知操作")
            return self.snapshot()

    def target_record(self, result):
        if not self.status.get("active") or self.status.get("servo") not in range(1, 5):
            raise ValueError("校准已超时或关闭，未记录有效目标")
        return {"servo": self.status["servo"], "gpio": self.status["servo"] + 37,
                "position_description": SERVO_POSITIONS.get(self.status["servo"], "未确认"),
                "reference_angle_deg": 0, "reference_pulse_us": 1500,
                "calibration_angle_deg": self.status["angle_deg"], "pulse_us": self.status["pulse_us"],
                "sent_at": datetime.now(timezone.utc).isoformat(), "boot_id": self.boot_id,
                "command_id": result.command_id, "evidence": "mock" if self.mock else "firmware_command_completed"}


class LocalServer:
    def __init__(self, bridge, port=8766, static=None):
        self.bridge = bridge
        self.static = static or STATIC
        self.port = port
        self.secret = secrets.token_hex(32)
        self.server = None

    async def start(self):
        self.server = await asyncio.start_server(self.serve, "127.0.0.1", self.port, limit=16384)
        self.port = self.server.sockets[0].getsockname()[1]
        self.origin = f"http://127.0.0.1:{self.port}"

    async def serve(self, reader, writer):
        status, content, mime = 200, b"", "application/json; charset=utf-8"
        try:
            head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 2)
            lines = head.decode("ascii").split("\r\n")
            method, path, version = lines[0].split(" ")
            headers = {}
            for line in lines[1:]:
                if not line:
                    continue
                key, value = line.split(":", 1)
                key = key.lower()
                if key in headers:
                    raise ValueError("重复请求头")
                headers[key] = value.strip()
            if headers.get("host") != f"127.0.0.1:{self.port}":
                raise PermissionError("Host 校验失败")
            if headers.get("sec-fetch-site", "same-origin") not in {"same-origin", "none"}:
                raise PermissionError("禁止跨站请求")
            if "transfer-encoding" in headers:
                raise ValueError("不支持分块请求")
            if method == "GET" and path in {"/", "/app.js", "/style.css"}:
                filename, mime = {"/": ("index.html", "text/html; charset=utf-8"),
                                  "/app.js": ("app.js", "text/javascript; charset=utf-8"),
                                  "/style.css": ("style.css", "text/css; charset=utf-8")}[path]
                asset=self.static / filename
                content = asset.read_bytes()
            elif method == "GET" and path == "/favicon.ico":
                status,content,mime=204,b"","image/x-icon"
            elif method == "POST" and path.startswith("/api/"):
                if (headers.get("origin") != self.origin or
                        not secrets.compare_digest(headers.get("x-tars-token", ""), self.secret)):
                    raise PermissionError("Origin 或令牌校验失败")
                if headers.get("content-type") != "application/json":
                    raise ValueError("要求 application/json")
                length = int(headers.get("content-length", "0"))
                if not 0 < length <= 4096:
                    raise ValueError("请求长度不合法")
                raw = await asyncio.wait_for(reader.readexactly(length), 2)
                data = json.loads(raw)
                if not isinstance(data, dict):
                    raise ValueError("要求 JSON 对象")
                action = path[5:]
                if action == "devices":
                    devices = ([{"serial": "mock", "port": "Mock", "label": "Mock 虚拟设备（无硬件）"}]
                               if self.bridge.mock else
                               [{"serial": p.serial_number, "port": p.device,
                                 "label": f"{p.device} · {p.serial_number}"} for p in UsbSerialTransport.devices() if p.serial_number])
                    result = {"devices": devices, "servo_positions": SERVO_POSITIONS, "mock":self.bridge.mock}
                else:
                    result = await self.bridge.handle(action, data)
                content = json.dumps(result, ensure_ascii=False).encode()
            else:
                status = 404
                content = b'{"error":"not_found"}'
        except PermissionError as exc:
            status, content = 403, json.dumps({"error": str(exc)}, ensure_ascii=False).encode()
        except Exception as exc:
            status, content = 400, json.dumps({"error": str(exc), "snapshot": self.bridge.snapshot()}, ensure_ascii=False).encode()
        try:
            writer.write((f"HTTP/1.1 {status} Response\r\nContent-Type: {mime}\r\nContent-Length: {len(content)}\r\n"
                          "Connection: close\r\nCache-Control: no-store\r\nX-Content-Type-Options: nosniff\r\n"
                          "Content-Security-Policy: default-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'\r\n"
                          "Referrer-Policy: no-referrer\r\n\r\n").encode() + content)
            await writer.drain()
        except ConnectionError:
            pass
        finally:
            writer.close()
            await writer.wait_closed()


async def run(args):
    bridge = CalibrationBridge(args.mock)
    server = LocalServer(bridge, args.port)
    await server.start()
    print(f"仅本机访问：{server.origin}/#{server.secret}", flush=True)
    print("Mock 模式，无硬件" if args.mock else "尚未连接设备；在页面中手动选择并连接。", flush=True)
    watcher = asyncio.create_task(bridge.watchdog())
    try:
        async with server.server:
            await server.server.serve_forever()
    finally:
        watcher.cancel()
        await asyncio.gather(watcher, return_exceptions=True)
        with contextlib.suppress(Exception):
            await bridge.stop()
        await bridge.drop()


def main():
    parser = argparse.ArgumentParser(description="TARS 本机单轴机械零点校准（不接入 GPT）")
    parser.add_argument("--mock", action="store_true", help="离线虚拟设备，完全不打开串口")
    parser.add_argument("--port", type=int, default=8766)
    args = parser.parse_args()
    if not 1024 <= args.port <= 65535:
        parser.error("端口必须为 1024..65535")
    try:
        asyncio.run(run(args))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
