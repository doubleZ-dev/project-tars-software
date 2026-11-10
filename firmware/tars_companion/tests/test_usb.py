import asyncio
import json
import queue
import types
import unittest
from unittest.mock import patch

from tars_companion.client import DeviceRestartedError, TarsClient
from tars_companion.simulator import SimulatedTarsDevice
from tars_companion.transport import (AmbiguousUsbDevice, LineTransport,
                                      TransportDisconnected, UsbFrameCodec, UsbSerialTransport)


class CodecTests(unittest.TestCase):
    def test_fragmentation_logs_boot_noise_and_utf8(self):
        codec = UsbFrameCodec()
        message = json.dumps({"v": 2, "type": "command.completed", "text": "收到"},
                             ensure_ascii=False).encode()
        raw = (b"ROM startup\n{\"v\":2,\"type\":\"hello\"}\n"
               b"@LOG2 log says {\"type\":\"hello\"}\n" + codec.encode(message))
        for byte in raw:
            codec.feed(bytes([byte]))
        self.assertEqual(list(codec.frames), [message])
        self.assertEqual(len(codec.logs), 1)

    def test_bad_frames_and_oversize_recover_at_newline(self):
        codec = UsbFrameCodec()
        for raw in (b"@TARS2 []\n", b"@TARS2 not-json\n",
                    b'@TARS2 {"v":1,"type":"hello"}\n', b"x" * 600 + b"\n"):
            codec.feed(raw)
        good = b'{"v":2,"type":"hello"}'
        codec.feed(codec.encode(good))
        self.assertEqual(list(codec.frames), [good])
        with self.assertRaises(ValueError):
            codec.encode(b"{}\n{}")


class FakeUsbBus(LineTransport):
    """In-memory USB wire, with real transport/client and simulated device."""
    def __init__(self):
        self.loop = asyncio.get_running_loop()
        self.requests = asyncio.Queue()
        self.outbound = queue.Queue()
        self.connected = True
        self.name = "COM46"
        self.serial = "TARS-TEST-001"
        self.writes = []
        self.open_snapshots = []
        self.drop_ack_once = False
        self.partial_write_once = False

    def ports(self):
        return ([types.SimpleNamespace(device=self.name, vid=0x303A, pid=0x1001,
                                       serial_number=self.serial)] if self.connected else [])

    async def read_line(self):
        return await self.requests.get()

    async def write_line(self, line):
        obj = json.loads(line)
        if self.drop_ack_once and obj.get("type") == "command.ack":
            self.drop_ack_once = False
            return
        self.outbound.put(b"@LOG2 unrelated debug output\n")
        encoded = UsbFrameCodec.encode(line)
        self.outbound.put(encoded[:9])
        self.outbound.put(encoded[9:])

    def serial_factory(self):
        bus = self

        class Port:
            dtr = True
            rts = True
            port = None

            def open(self):
                if not bus.connected or self.port != bus.name:
                    raise OSError("absent")
                bus.open_snapshots.append((self.port, self.dtr, self.rts))

            def close(self):
                pass

            def read(self, count):
                if not bus.connected or self.port != bus.name:
                    raise OSError("USB unplugged")
                try:
                    return bus.outbound.get(timeout=0.025)
                except queue.Empty:
                    return b""

            def write(self, wire):
                if not bus.connected or self.port != bus.name:
                    raise OSError("USB unplugged")
                assert wire.startswith(UsbFrameCodec.PREFIX)
                line = wire[len(UsbFrameCodec.PREFIX):].rstrip(b"\n")
                bus.writes.append(line)
                bus.loop.call_soon_threadsafe(bus.requests.put_nowait, line)
                if bus.partial_write_once:
                    bus.partial_write_once = False
                    return len(wire) - 1
                return len(wire)

        return Port()


class NativeUsbTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.bus = FakeUsbBus()
        self.serial_patch = patch.dict("sys.modules", {"serial": types.SimpleNamespace(
            Serial=self.bus.serial_factory, SerialException=OSError)})
        self.serial_patch.start()
        self.device_patch = patch.object(UsbSerialTransport, "devices", side_effect=self.bus.ports)
        self.device_patch.start()
        self.device = SimulatedTarsDevice(self.bus, motion_delay=0.3, lease_timeout=0.12,
                                          native_session_required=True)
        await self.device.start()
        self.transport = UsbSerialTransport()
        self.client = TarsClient(self.transport, heartbeat_interval=0.02)
        await self.client.start()

    async def asyncTearDown(self):
        await self.client.close()
        await self.device.close()
        self.device_patch.stop()
        self.serial_patch.stop()

    async def test_handshake_state_display_and_modem_control(self):
        state = await self.client.state()
        self.assertFalse(state.message["payload"]["result"]["armed"])
        display = await self.client.display_dialogue("user", "USB 屏幕链路测试")
        self.assertTrue(display.succeeded)
        self.assertEqual(self.device.dialogue, [("user", "USB 屏幕链路测试")])
        self.assertEqual(self.bus.open_snapshots, [("COM46", False, False)])
        self.assertTrue(self.transport.codec.logs)

    async def test_disconnect_reenumeration_cancels_pending_and_never_rearms(self):
        await self.client.arm()
        pending = asyncio.create_task(self.client.execute("walk_forward", command_id="usb-motion"))
        await asyncio.sleep(0.03)
        self.bus.connected = False
        with self.assertRaises(TransportDisconnected):
            await asyncio.wait_for(pending, 1)
        self.bus.name = "COM99"
        self.bus.connected = True
        await self.client.start()
        state = await self.client.state()
        self.assertFalse(state.message["payload"]["result"]["armed"])
        self.assertEqual(self.device.execution_count, 1)
        writes = [json.loads(w) for w in self.bus.writes]
        self.assertEqual(sum(w["type"] == "safety.arm" for w in writes), 1)
        self.assertEqual(sum(w["type"] == "motion.execute" for w in writes), 1)
        self.assertEqual(self.bus.open_snapshots[-1][0], "COM99")

    async def test_device_reboot_invalidates_pending_then_recovers(self):
        await self.client.arm()
        old_boot = self.client.boot_id
        pending = asyncio.create_task(self.client.execute("walk_forward"))
        await asyncio.sleep(0.03)
        await self.device.reboot()
        with self.assertRaises(DeviceRestartedError):
            await asyncio.wait_for(pending, 1)
        await self.client.start()
        self.assertNotEqual(self.client.boot_id, old_boot)
        self.assertFalse(self.device.armed)

    async def test_lost_ack_retries_same_wire_without_duplicate_motion(self):
        await self.client.arm()
        self.bus.drop_ack_once = True
        result = await self.client.execute("walk_forward", command_id="same-usb",
                                           ack_timeout=0.05, retries=1)
        self.assertTrue(result.succeeded)
        wires = [w for w in self.bus.writes if json.loads(w).get("command_id") == "same-usb"]
        self.assertEqual(len(wires), 2)
        self.assertEqual(wires[0], wires[1])
        self.assertEqual(self.device.execution_count, 1)

    async def test_duplicate_and_conflict(self):
        one = await self.client.state(command_id="usb-replay")
        two = await self.client.state(command_id="usb-replay")
        self.assertEqual(one.message, two.message)
        conflict = await self.client.command("state.get", {"different": True}, command_id="usb-replay")
        self.assertFalse(conflict.accepted)
        self.assertEqual(conflict.message["error"]["code"], "command_id_conflict")

    async def test_partial_write_is_unknown_and_not_replayed_after_reconnect(self):
        await self.client.arm()
        self.bus.partial_write_once = True
        with self.assertRaises(TransportDisconnected):
            await self.client.execute("walk_forward", command_id="uncertain-write")
        await self.client.start()
        self.assertFalse(self.device.armed)
        wires = [w for w in self.bus.writes if json.loads(w).get("command_id") == "uncertain-write"]
        self.assertEqual(len(wires), 1)
        self.assertLessEqual(self.device.execution_count, 1)

    async def test_estop_clear_and_lease_stay_safe_over_usb(self):
        await self.client.arm()
        motion = asyncio.create_task(self.client.execute("walk_forward"))
        await asyncio.sleep(0.03)
        self.assertTrue((await self.client.estop()).succeeded)
        self.assertEqual((await motion).terminal_type, "command.cancelled")
        await self.client.clear_estop()
        self.assertFalse(self.device.armed)
        self.assertFalse((await self.client.execute("walk_forward")).accepted)
        await self.client.arm()
        await self.client.close()
        await asyncio.sleep(0.2)
        self.assertFalse(self.device.armed)

    async def test_old_session_hello_cannot_satisfy_handshake(self):
        old = self.client._session_nonce
        self.client._invalidate_session(TransportDisconnected("test lost link"))
        await self.device._hello()  # stale buffered hello has the previous nonce
        await self.client.start()
        self.assertNotEqual(self.client._session_nonce, old)
        self.assertEqual(self.client.hello["payload"]["session_id"], self.client._session_nonce)

    async def test_identity_selection_refuses_ambiguity_and_replacement(self):
        first = self.bus.ports()[0]
        other = types.SimpleNamespace(device="COM7", vid=0x303A, pid=0x1001, serial_number="OTHER")
        with patch.object(UsbSerialTransport, "devices", return_value=[first, other]):
            with self.assertRaises(AmbiguousUsbDevice):
                UsbSerialTransport()._choose()
        self.bus.serial = "OTHER"
        with self.assertRaises(TransportDisconnected):
            self.transport._choose()


if __name__ == "__main__":
    unittest.main()
