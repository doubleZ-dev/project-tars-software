import asyncio
import json
import unittest

from tars_companion.client import CommandResult, DeviceRestartedError, TarsClient
from tars_companion.hil import safe_protocol_probe
from tars_companion.personality import PersonalityCommandIdConflict, PersonalityRenderer
from tars_companion.simulator import SimulatedTarsDevice
from tars_companion.transport import LineTransport, memory_transport_pair


class DropFirstWrite(LineTransport):
    def __init__(self, inner: LineTransport):
        self.inner = inner
        self.dropped = False

    async def write_line(self, line: bytes) -> None:
        if not self.dropped:
            self.dropped = True
            return
        await self.inner.write_line(line)

    async def read_line(self) -> bytes:
        return await self.inner.read_line()


class CompanionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.pc_transport, device_transport = memory_transport_pair()
        self.device = SimulatedTarsDevice(device_transport, motion_delay=0.001)
        await self.device.start()
        self.client = TarsClient(self.pc_transport)
        await self.client.start()
        await self.client.arm()

    async def asyncTearDown(self):
        await self.client.close()
        await self.device.close()

    async def test_execute_end_to_end(self):
        result = await self.client.execute("walk_forward", command_id="pc-one")
        self.assertTrue(result.succeeded)
        self.assertEqual(self.device.execution_count, 1)

    async def test_dialogue_append_preserves_utf8_without_motion(self):
        result = await self.client.display_dialogue("user", "地球无法移动，我们仍要救人。")
        self.assertTrue(result.succeeded)
        self.assertEqual(self.device.dialogue, [("user", "地球无法移动，我们仍要救人。")])
        self.assertEqual(self.device.execution_count, 0)

    async def test_dialogue_append_compacts_whitespace_and_clips_on_utf8_boundary(self):
        result = await self.client.display_dialogue("tars", "回答：\n" + "人类" * 100)
        self.assertTrue(result.succeeded)
        role, text = self.device.dialogue[-1]
        self.assertEqual(role, "tars")
        self.assertNotIn("\n", text)
        self.assertTrue(text.endswith("…"))
        self.assertLessEqual(len(text.encode("utf-8")), 300)

    async def test_dialogue_append_rejects_role_and_control_characters_locally(self):
        with self.assertRaises(ValueError):
            await self.client.display_dialogue("system", "hello")
        with self.assertRaises(ValueError):
            await self.client.display_dialogue("user", "bad\x00text")
        self.assertEqual(self.device.dialogue, [])

    async def test_dialogue_protocol_rejects_invalid_payloads(self):
        too_long = await self.client.command(
            "dialogue.append", {"role": "user", "text": "x" * 301}
        )
        control = await self.client.command(
            "dialogue.append", {"role": "tars", "text": "bad\ntext"}
        )
        invalid_role = await self.client.command(
            "dialogue.append", {"role": "system", "text": "hello"}
        )
        self.assertFalse(too_long.accepted)
        self.assertFalse(control.accepted)
        self.assertFalse(invalid_role.accepted)
        self.assertEqual(self.device.dialogue, [])

    async def test_ack_retry_reuses_same_command(self):
        await self.client.close()
        retrying = TarsClient(DropFirstWrite(self.pc_transport))
        await retrying.start()
        result = await retrying.execute("turn_left", command_id="pc-retry", ack_timeout=0.005, retries=1)
        self.assertTrue(result.succeeded)
        self.assertEqual(self.device.execution_count, 1)
        await retrying.close()

    async def test_duplicate_wire_does_not_execute_twice(self):
        raw_pc, raw_device = memory_transport_pair()
        device = SimulatedTarsDevice(raw_device, motion_delay=0)
        await device.start()
        device.armed = True
        await raw_pc.read_line()  # hello
        wire = json.dumps({"v": 2, "type": "motion.execute", "target_boot_id": device.boot_id,
                           "command_id": "same", "payload": {"name": "walk_forward", "steps": 1, "speed_pct": 50}},
                          separators=(",", ":")).encode()
        await raw_pc.write_line(wire)
        await raw_pc.read_line()  # ack
        await raw_pc.read_line()  # terminal
        await raw_pc.write_line(wire)
        replay = json.loads(await raw_pc.read_line())
        self.assertEqual(replay["type"], "command.completed")
        self.assertEqual(device.execution_count, 1)
        await device.close()

    async def test_same_id_different_request_conflicts(self):
        raw_pc, raw_device = memory_transport_pair()
        device = SimulatedTarsDevice(raw_device, motion_delay=0)
        await device.start(); await raw_pc.read_line()
        first = json.dumps({"v": 2, "type": "state.get", "target_boot_id": device.boot_id,
                            "command_id": "same", "payload": {}}, separators=(",", ":")).encode()
        second = json.dumps({"v": 2, "type": "safety.estop", "target_boot_id": device.boot_id,
                             "command_id": "same", "payload": {}}, separators=(",", ":")).encode()
        await raw_pc.write_line(first); await raw_pc.read_line(); await raw_pc.read_line()
        await raw_pc.write_line(second)
        conflict = json.loads(await raw_pc.read_line())
        self.assertEqual(conflict["error"]["code"], "command_id_conflict")
        await device.close()

    async def test_estop_then_clear_never_resumes_old_motion(self):
        await self.device.close()
        pc, device_transport = memory_transport_pair()
        device = SimulatedTarsDevice(device_transport, motion_delay=0.2)
        await device.start()
        client = TarsClient(pc); await client.start()
        await client.arm()
        motion = asyncio.create_task(client.execute("walk_forward", command_id="long-motion"))
        await asyncio.sleep(0.01)
        estop = await client.estop(command_id="stop-now")
        clear = await client.clear_estop(command_id="clear-after")
        motion_result = await motion
        self.assertTrue(estop.succeeded)
        self.assertTrue(clear.succeeded)
        self.assertEqual(motion_result.terminal_type, "command.cancelled")
        self.assertEqual(device.execution_count, 1)
        await client.close(); await device.close()

    async def test_reboot_makes_pending_motion_unknown_and_not_retried(self):
        await self.device.close()
        pc, device_transport = memory_transport_pair()
        device = SimulatedTarsDevice(device_transport, motion_delay=1.0)
        await device.start()
        client = TarsClient(pc); await client.start()
        await client.arm()
        pending = asyncio.create_task(client.execute("walk_forward", command_id="before-reboot", retries=0))
        await asyncio.sleep(0.01)
        await device.reboot()
        with self.assertRaises(DeviceRestartedError):
            await pending
        self.assertEqual(device.execution_count, 1)
        await client.close(); await device.close()

    async def test_session_open_recovers_missed_boot_hello(self):
        pc, device_transport = memory_transport_pair()
        device = SimulatedTarsDevice(device_transport)
        await device.start()
        await pc.read_line()  # deliberately lose boot-time hello
        client = TarsClient(pc)
        await client.start()
        self.assertEqual(client.boot_id, device.boot_id)
        await client.close(); await device.close()

    async def test_safe_hil_probe_has_no_motion_side_effect(self):
        checks = await safe_protocol_probe(self.client)
        self.assertTrue(all(check.passed for check in checks))
        self.assertEqual(self.device.execution_count, 0)

    async def test_boot_is_disarmed_and_clear_does_not_arm(self):
        pc, device_transport = memory_transport_pair()
        device = SimulatedTarsDevice(device_transport)
        await device.start()
        client = TarsClient(pc); await client.start()
        state = await client.state()
        snapshot = state.message["payload"]["result"]
        self.assertFalse(snapshot["armed"])
        rejected = await client.execute("walk_forward")
        self.assertEqual(rejected.message["error"]["code"], "disarmed")
        await client.estop(); await client.clear_estop()
        state = await client.state()
        self.assertFalse(state.message["payload"]["result"]["armed"])
        await client.close(); await device.close()

    async def test_control_lease_disarms_after_client_closes(self):
        pc, device_transport = memory_transport_pair()
        device = SimulatedTarsDevice(device_transport, lease_timeout=0.08)
        await device.start()
        client = TarsClient(pc, heartbeat_interval=0.02)
        await client.start()
        await client.arm()
        await asyncio.sleep(0.12)
        self.assertTrue(device.armed)
        await client.close()
        await asyncio.sleep(0.12)
        self.assertFalse(device.armed)
        await device.close()

    async def test_explicit_disarm(self):
        result = await self.client.disarm()
        self.assertTrue(result.succeeded)
        self.assertFalse(self.device.armed)


class PersonalityTests(unittest.TestCase):
    def setUp(self):
        self.renderer = PersonalityRenderer()

    def test_normal_success_always_has_one_quip(self):
        result = CommandResult("x1", True, "command.completed", {})
        text = self.renderer.render(result, action="前进")
        self.assertTrue(text.startswith("前进已完成。"))
        self.assertGreater(len(text), len("前进已完成。"))

    def test_safety_context_has_no_quip(self):
        result = CommandResult("x2", True, "command.completed", {})
        self.assertEqual(self.renderer.render(result, action="软件急停", context="safety"), "软件急停已完成。")

    def test_cancel_has_no_quip(self):
        result = CommandResult("x3", True, "command.cancelled", {})
        self.assertEqual(self.renderer.render(result, action="前进"), "前进已取消。")

    def test_same_command_renders_identically_and_speaks_once(self):
        result = CommandResult("same-render", True, "command.completed", {})
        first = self.renderer.render(result, action="前进")
        second = self.renderer.render(result, action="前进")
        self.assertEqual(first, second)
        self.assertTrue(self.renderer.claim_speech(result.command_id))
        self.assertFalse(self.renderer.claim_speech(result.command_id))

    def test_same_id_different_semantics_conflicts(self):
        result = CommandResult("personality-conflict", True, "command.completed", {})
        self.renderer.render(result, action="前进")
        with self.assertRaises(PersonalityCommandIdConflict):
            self.renderer.render(result, action="左转")

    def test_sensitive_label_suppresses_without_manual_context(self):
        result = CommandResult("low-power", True, "command.completed", {})
        self.assertEqual(self.renderer.render(result, action="状态查询",
                                              context_labels=frozenset({"low_battery"})), "状态查询已完成。")


if __name__ == "__main__":
    unittest.main()
