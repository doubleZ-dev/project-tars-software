"""LAN body microphone/speaker with explicit offline mock providers only."""
import argparse
import asyncio
from .wifi_audio import LanAudioServer, load_profile
from .audio import UtteranceCollector
from .brain import TarsBrain
from .client import TarsClient
from .llm import ScriptedProvider
from .speech import create_offline_speech, SpeechOptions
from .simulator import SimulatedTarsDevice
from .transport import memory_transport_pair
from .voice import VoiceSession, VoiceStageError


async def run(args):
    if not args.mock:
        raise ValueError("ASR/TTS not selected; --mock is required in this stage, no cloud fallback")
    config = load_profile(args.config)
    server = LanAudioServer(config["token"])
    pc, peer = memory_transport_pair()
    device = SimulatedTarsDevice(peer)
    client = TarsClient(pc)
    try:
        await device.start()
        await client.start()  # no USB port, no Arm endpoint in this CLI
        await server.start(args.bind or config["host"], config["port"])
        print("LAN audio listener ready. Providers are MOCK; output is a low-volume tone, NOT speech.")
        print("No physical motion control. Waiting for the configured PCB...")
        while True:
            link = await server.wait_connected(args.wait_seconds)
            prompt = await asyncio.to_thread(input, "Enter: one microphone turn; quit: exit > ")
            if prompt.strip().lower() in {"quit", "exit"}:
                break
            asr, tts = create_offline_speech(mock=True, text=args.mock_text,
                                            options=SpeechOptions("auto", "mock-tone"))
            session = VoiceSession(TarsBrain(client, provider=ScriptedProvider(
                "Offline audio framework test; no real speech recognition or cloud model is active.")),
                asr, tts, link, UtteranceCollector(silence_ms=500), capture_timeout=15,
                dialogue_publisher=link.display_dialogue, interaction_publisher=link.interaction)
            try:
                result = await session.run_turn(link)
                print(f"Mock transcript: {result.transcript}; playback error={result.speech_error}")
            except VoiceStageError as error:
                print(f"Audio turn failed: {error.stage}/{error.code}; no automatic audio replay.")
            if args.once:
                break
    finally:
        await server.close()
        await client.close()
        await device.close()


def main():
    parser = argparse.ArgumentParser(description="Trusted-LAN body audio; offline providers only")
    parser.add_argument("--config", required=True)
    parser.add_argument("--mock", action="store_true")
    parser.add_argument("--mock-text", default="介绍一下你的任务")
    parser.add_argument("--bind", help="defaults to the configured PC LAN IPv4, not all interfaces")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--wait-seconds", type=float, default=30)
    args = parser.parse_args()
    try:
        asyncio.run(run(args))
    except KeyboardInterrupt:
        pass
    except Exception as error:
        raise SystemExit(f"LAN audio stopped ({type(error).__name__}); no credentials logged") from None


if __name__ == "__main__":
    main()
