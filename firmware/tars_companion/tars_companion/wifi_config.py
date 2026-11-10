"""Explicit local USB provisioning; never used automatically by LAN audio."""
import argparse
import asyncio
from .wifi_audio import load_profile
from .client import TarsClient
from .transport import UsbSerialTransport


async def provision(args):
    profile = load_profile(args.config)
    if not args.apply:
        print("Profile syntax valid. No port opened or configuration written. Use --apply to provision.")
        return
    client = TarsClient(UsbSerialTransport(serial_number=args.usb_serial))
    try:
        await client.start()
        state = await client.state()
        value = state.message.get("payload", {}).get("result", {})
        if not state.succeeded or value.get("armed") is not False or value.get("active_command_id"):
            raise RuntimeError("provisioning_requires_disarmed_idle")
        result = await client.command("audio.wifi.configure", profile)
        if not result.succeeded:
            raise RuntimeError("wifi_profile_not_saved")
        print("Profile saved. Reboot manually to apply. Credentials are not displayed.")
    finally:
        await client.close()


def main():
    parser = argparse.ArgumentParser(description="Validate or explicitly provision Wi-Fi audio over USB")
    parser.add_argument("--config", required=True)
    parser.add_argument("--usb-serial")
    parser.add_argument("--apply", action="store_true", help="writes board NVS; use only after hardware authorization")
    args = parser.parse_args()
    try:
        asyncio.run(provision(args))
    except Exception as error:
        raise SystemExit(f"Configuration failed ({type(error).__name__}); credentials suppressed") from None


if __name__ == "__main__":
    main()
