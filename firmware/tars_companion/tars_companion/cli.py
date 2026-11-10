from __future__ import annotations

import argparse
import asyncio

from .client import TarsClient
from .personality import PersonalityRenderer
from .simulator import SimulatedTarsDevice
from .transport import UsbSerialTransport, memory_transport_pair


async def run(args: argparse.Namespace) -> None:
    simulator = None
    if args.simulate:
        pc_transport, device_transport = memory_transport_pair()
        simulator = SimulatedTarsDevice(device_transport)
        await simulator.start()
    else:
        pc_transport = UsbSerialTransport(args.port, args.baud, serial_number=args.usb_serial)
    client = TarsClient(pc_transport)
    renderer = PersonalityRenderer()
    try:
        await client.start()
    except Exception:
        await client.close()
        if simulator:
            await simulator.close()
        raise
    print("TARS ready, disarmed. commands: forward N, left, right, state, estop, clear, disarm, quit")
    print("Explicit arm: arm I_HAVE_LIFTED_TARS_AND_CAN_CUT_SERVO_POWER")
    try:
        while True:
            raw = (await asyncio.to_thread(input, "> ")).strip().lower()
            if raw in {"quit", "exit", "退出"}:
                break
            try:
                if raw == "arm i_have_lifted_tars_and_can_cut_servo_power":
                    result = await client.arm(); print(result.message)
                elif raw == "disarm":
                    result = await client.disarm(); print(result.message)
                elif raw.startswith(("forward", "前进")):
                    parts = raw.split(); steps = int(parts[1]) if len(parts) > 1 else 1
                    result = await client.execute("walk_forward", steps=steps)
                    print(renderer.render(result, action=f"前进 {steps} 步", command_type="motion.execute"))
                elif raw in {"left", "左转"}:
                    result = await client.execute("turn_left")
                    print(renderer.render(result, action="左转", command_type="motion.execute"))
                elif raw in {"right", "右转"}:
                    result = await client.execute("turn_right")
                    print(renderer.render(result, action="右转", command_type="motion.execute"))
                elif raw in {"estop", "急停"}:
                    result = await client.estop(); print(renderer.render(result, action="软件急停", command_type="safety.estop"))
                elif raw in {"clear", "解除急停"}:
                    result = await client.clear_estop(); print(renderer.render(result, action="解除软件急停", command_type="safety.clear"))
                elif raw in {"state", "状态"}:
                    result = await client.state(); print(result.message)
                else:
                    print("未知命令。人类语言很丰富，当前协议没有。")
            except Exception as exc:
                print(f"命令失败：{exc}")
    finally:
        await client.close()
        if simulator:
            await simulator.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--simulate", action="store_true")
    parser.add_argument("--port")
    parser.add_argument("--usb-serial")
    parser.add_argument("--baud", type=int, default=115200)
    asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    main()
