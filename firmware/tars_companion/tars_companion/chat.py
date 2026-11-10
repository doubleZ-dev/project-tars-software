from __future__ import annotations

import argparse
import asyncio
import os
import json
from dataclasses import asdict

from .brain import TarsBrain
from .client import TarsClient
from .hil import MOTION_CONFIRMATION
from .intent import MotionIntent, parse_intent
from .llm import LLMProviderError, create_provider
from .memory import MemoryStore
from .simulator import SimulatedTarsDevice
from .transport import UsbSerialTransport, memory_transport_pair


async def run(args: argparse.Namespace) -> None:
    if not args.robot and (args.port or args.usb_serial):
        raise ValueError("USB selection requires --robot; default mode never opens hardware")
    provider = create_provider(args.provider, model=args.model)
    device = None
    client = None
    memory = None
    try:
        if not args.robot:
            pc_transport, device_transport = memory_transport_pair()
            device = SimulatedTarsDevice(device_transport)
            await device.start()
        else:
            pc_transport = UsbSerialTransport(args.port, args.baud, serial_number=args.usb_serial)
        client = TarsClient(pc_transport)
        memory = MemoryStore(args.memory) if args.memory else None
        brain = TarsBrain(client, provider=provider, memory=memory)
        await client.start()
        mode = "REAL USB" if args.robot else "SIMULATED ROBOT; no hardware port opened"
        print(f"TARS text ready | {mode} | provider={args.provider} | disarmed")
        print("Chat in English or Chinese. quit / /state / /disarm. Backward gait unavailable.")
        print(f"Explicit Arm: /arm {MOTION_CONFIRMATION}")
        if args.robot and not args.allow_motion:
            print("Real motion and Arm blocked: --allow-motion was not supplied.")
        while True:
            try:
                text = (await asyncio.to_thread(input, "> ")).strip()
            except (EOFError, KeyboardInterrupt):
                break
            if text.lower() in {"quit", "exit", "退出"}:
                break
            if not text:
                continue
            try:
                if text == f"/arm {MOTION_CONFIRMATION}":
                    if args.robot and not args.allow_motion:
                        print("Arm blocked; real motion was not explicitly enabled.")
                    else:
                        print((await client.arm()).message)
                    continue
                if text == "/disarm":
                    print((await client.disarm()).message)
                    continue
                if text == "/state":
                    print((await client.state()).message)
                    continue
                intent = parse_intent(text)
                print("intent=" + json.dumps({"type": type(intent).__name__, **asdict(intent)}, ensure_ascii=False))
                if args.robot and not args.allow_motion and isinstance(intent, MotionIntent):
                    print("Real motion blocked; no command sent. Use simulated mode for action tests.")
                    continue
                response = await brain.handle_text(text)
                print("TARS: " + response.text)
            except LLMProviderError as exc:
                print(f"LLM failed: {exc.code}; no automatic provider fallback or motion.")
            except Exception as exc:
                print(f"Request failed ({type(exc).__name__}); execution is not confirmed.")
    finally:
        if memory:
            memory.close()
        if client:
            await client.close()
        if device:
            await device.close()
        await provider.aclose()


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="TARS text brain; simulated robot by default")
    target = result.add_mutually_exclusive_group()
    target.add_argument("--simulate", action="store_true", help="simulated robot (default)")
    target.add_argument("--robot", action="store_true", help="explicitly connect real native USB hardware")
    result.add_argument("--port")
    result.add_argument("--usb-serial")
    result.add_argument("--baud", type=int, default=115200)
    provider = result.add_mutually_exclusive_group()
    provider.add_argument("--provider", choices=("local", "openai", "deepseek"))
    provider.add_argument("--openai", action="store_const", const="openai", dest="provider")
    provider.add_argument("--deepseek", action="store_const", const="deepseek", dest="provider")
    result.set_defaults(provider=os.environ.get("TARS_LLM_PROVIDER", "local"))
    result.add_argument("--model", help="otherwise uses OPENAI_MODEL or DEEPSEEK_MODEL")
    result.add_argument("--memory", help="optional existing SQLite history; otherwise bounded session history only")
    result.add_argument("--allow-motion", action="store_true", help="allow real movement requests; still needs explicit /arm and guards")
    return result


def main() -> None:
    arg_parser = parser()
    try:
        asyncio.run(run(arg_parser.parse_args()))
    except ValueError as exc:
        arg_parser.error(str(exc))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
