from __future__ import annotations

import argparse
import asyncio
import json
from dataclasses import asdict, dataclass
from pathlib import Path

from .client import TarsClient
from .transport import UsbSerialTransport


MOTION_CONFIRMATION = "I_HAVE_LIFTED_TARS_AND_CAN_CUT_SERVO_POWER"


@dataclass
class Check:
    name: str
    passed: bool
    detail: str


async def safe_protocol_probe(client: TarsClient) -> list[Check]:
    await client.start()
    checks = [Check("hello_boot_id", bool(client.boot_id and len(client.boot_id) == 16), str(client.boot_id))]
    payload = (client.hello or {}).get("payload", {})
    known_release = (payload.get("firmware_version"), payload.get("release_id")) in {
        ("0.14.0-stage4", "tars-pcb-v1-stage4"),
        ("0.15.0-integrated", "tars-wireless-v1"),
        ("0.15.1-gaitrestore", "tars-wireless-gait-v1"),
        ("0.15.2-diag", "tars-wireless-diag-v1"),
        ("0.15.3-gaitlab", "tars-usb-gaitlab-v1"),
        ("0.15.4-drop30", "tars-gait-drop30-v1"),
        ("0.15.5-turnlab", "tars-usb-turnlab-v1"),
        ("0.15.6-dropramp", "tars-usb-dropramp-v1"),
        ("0.15.7-freeangles", "tars-usb-freeangles-v1"),
        ("0.15.8-turnsmooth", "tars-usb-turnsmooth-v1"),
        ("0.15.9-voicegait", "tars-wireless-voicegait-v1"),
    }
    checks.append(Check("firmware_version", known_release,
                        str(payload.get("firmware_version"))))
    checks.append(Check("release_id", known_release,
                        str(payload.get("release_id"))))
    checks.append(Check("boot_disarmed_contract", payload.get("boot_disarmed") is True,
                        str(payload.get("boot_disarmed"))))
    command_id = client.new_command_id()
    first = await client.state(command_id=command_id)
    second = await client.state(command_id=command_id)
    checks.append(Check("state_get", first.succeeded, first.terminal_type))
    checks.append(Check("exact_retry_replay", second.succeeded and second.message == first.message,
                        second.terminal_type))
    conflict = await client.command("state.get", {"probe": True}, command_id=command_id)
    code = conflict.message.get("error", {}).get("code")
    checks.append(Check("command_id_conflict", not conflict.accepted and code == "command_id_conflict", str(code)))
    return checks


async def motion_estop_probe(client: TarsClient) -> list[Check]:
    armed = await client.arm(command_id=client.new_command_id())
    if not armed.succeeded:
        return [Check("explicit_arm", False, armed.terminal_type)]
    motion = asyncio.create_task(client.execute("walk_forward", steps=20, speed_pct=10,
                                                command_id=client.new_command_id(), terminal_timeout=20))
    await asyncio.sleep(0.05)
    estop = await client.estop(command_id=client.new_command_id())
    motion_result = await motion
    clear = await client.clear_estop(command_id=client.new_command_id())
    return [
        Check("explicit_arm", armed.succeeded, armed.terminal_type),
        Check("software_estop_completed", estop.succeeded, estop.terminal_type),
        Check("old_motion_cancelled", motion_result.terminal_type == "command.cancelled", motion_result.terminal_type),
        Check("clear_after_cancel", clear.succeeded, clear.terminal_type),
    ]


async def run(args: argparse.Namespace) -> int:
    if args.list_ports:
        import serial.tools.list_ports
        for port in serial.tools.list_ports.comports():
            print(f"{port.device}\t{port.description}")
        return 0
    transport = UsbSerialTransport(args.port, args.baud, serial_number=args.usb_serial)
    client = TarsClient(transport)
    try:
        checks = await safe_protocol_probe(client)
        if args.motion:
            if args.confirm_lifted != MOTION_CONFIRMATION:
                raise SystemExit(f"motion test requires --confirm-lifted {MOTION_CONFIRMATION}")
            checks.extend(await motion_estop_probe(client))
        boot_id = client.boot_id
    finally:
        await client.close()
    report = {"port": transport.port_name, "usb_serial": transport.serial_number,
              "boot_id": boot_id, "checks": [asdict(c) for c in checks]}
    text = json.dumps(report, ensure_ascii=False, indent=2)
    print(text)
    if args.output:
        Path(args.output).write_text(text, encoding="utf-8")
    return 0 if all(c.passed for c in checks) else 1


def main() -> None:
    parser = argparse.ArgumentParser(description="TARS native USB hardware-in-the-loop probe")
    parser.add_argument("--list-ports", action="store_true")
    parser.add_argument("--port")
    parser.add_argument("--usb-serial", help="stable native USB serial identity; COM may change")
    parser.add_argument("--baud", type=int, default=115200)
    parser.add_argument("--motion", action="store_true", help="runs a real motion + estop test")
    parser.add_argument("--confirm-lifted")
    parser.add_argument("--output")
    raise SystemExit(asyncio.run(run(parser.parse_args())))


if __name__ == "__main__":
    main()
