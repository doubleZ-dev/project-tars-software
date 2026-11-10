"""Explicit body-microphone A/B capture and replay; no ASR/model/PC microphone."""
import argparse
import asyncio
import contextlib
import json
import math
import struct
import wave
from pathlib import Path
from .audio import ASR_PCM, make_test_tone, pcm16_rms
from .wifi_audio import LanAudioServer, load_profile


def statistics(pcm):
    samples = struct.unpack(f"<{len(pcm)//2}h", pcm)
    dc = sum(samples) / len(samples)
    return {"samples": len(samples), "rms": round(pcm16_rms(pcm), 2),
            "ac_rms": round(math.sqrt(sum((x-dc)**2 for x in samples)/len(samples)), 2),
            "peak": max(abs(x) for x in samples), "dc": round(dc, 2),
            "clipped_samples": sum(abs(x) >= 32700 for x in samples)}


async def capture(link, seconds):
    await link.interaction("LISTENING")
    await link.start_capture()
    frames = []
    try:
        for _ in range(seconds * 10):
            frames.append(await asyncio.wait_for(link.read_frame(), 2))
        return b"".join(frames)
    finally:
        await link.stop_capture()


def save_wav(path, pcm):
    with wave.open(str(path), "wb") as out:
        out.setnchannels(1); out.setsampwidth(2); out.setframerate(16000)
        out.writeframes(pcm)


async def run(args):
    profile = load_profile(args.config)
    directory = Path(args.output_dir)
    if any((directory / name).exists() for name in ("quiet.wav", "speech.wav", "report.json")):
        raise ValueError("output_exists_use_a_new_directory")
    directory.mkdir(parents=True, exist_ok=True)
    server = LanAudioServer(profile["token"])
    link = None
    try:
        await server.start(profile["host"], profile["port"])
        print("Waiting for paired BODY microphone. No PC microphone or cloud API.", flush=True)
        link = await server.wait_connected(30)
        print("PAIRED. First cue in 8s: stay quiet 3s. Second cue: speak repeatedly for 5s.", flush=True)
        await asyncio.sleep(8)
        cue = make_test_tone(200, 880, volume_pct=50)
        await link.play(cue, ASR_PCM, asyncio.Event(), volume_pct=5)
        print("QUIET_CAPTURE_START", flush=True)
        quiet = await capture(link, 3)
        save_wav(directory / "quiet.wav", quiet)
        await link.interaction("IDLE")
        await asyncio.sleep(1)
        await link.play(cue, ASR_PCM, asyncio.Event(), volume_pct=5)
        print("SPEECH_CAPTURE_START: repeat the test phrase for 5 seconds", flush=True)
        speech = await capture(link, 5)
        save_wav(directory / "speech.wav", speech)
        print("CAPTURE_FINISHED. Replaying actual recorded speech in 2s at 5 percent.", flush=True)
        await asyncio.sleep(2)
        await link.interaction("SPEAKING")
        await link.play(speech, ASR_PCM, asyncio.Event(), volume_pct=5)
        report = {"source": "ESP32 body INMP441 via authenticated LAN",
                  "device_id": link.device_id, "session": link.session,
                  "format": "16000 Hz mono PCM16 little-endian",
                  "quiet": statistics(quiet), "speech": statistics(speech),
                  "body_playback_io_completed": True,
                  "speech_intelligibility": "pending user listening confirmation",
                  "asr_or_cloud_used": False, "motion_commands_sent": False}
        (directory / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(json.dumps(report, ensure_ascii=False), flush=True)
        print("FILES: " + str(directory.resolve()), flush=True)
    finally:
        if link and not link.closed.is_set():
            with contextlib.suppress(Exception):
                await link.interaction("IDLE")
        await server.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    parser.add_argument("--output-dir", required=True)
    args = parser.parse_args()
    try:
        asyncio.run(run(args))
    except KeyboardInterrupt:
        pass
    except Exception as error:
        raise SystemExit(f"Microphone test stopped ({type(error).__name__}); no credentials displayed") from None


if __name__ == "__main__":
    main()
