"""Integrated body voice + guarded JSON v2 control. Offline simulation by default."""
import argparse
import asyncio
import os
import sys
import json
from pathlib import Path
from datetime import datetime, timezone
from .audio import UtteranceCollector, MemoryAudioSource, MemoryAudioSink, ASR_PCM, make_test_tone
from .audio_levels import BodyMicrophone, BodySpeaker
from .brain import TarsBrain
from .client import TarsClient
from .hil import MOTION_CONFIRMATION
from .llm import ScriptedProvider, create_provider
from .speech import DashScopeFunAsr, create_dashscope_synthesizer, create_offline_speech, speech_error_details
from .simulator import SimulatedTarsDevice
from .transport import memory_transport_pair
from .voice import VoiceSession, VoiceStageError
from .wifi_audio import LanAudioServer, NetworkJsonTransport, load_profile
from .tools import ToolPolicyRejected


class RuntimeClient(TarsClient):
    def __init__(self, transport, *, allow_motion=False):
        super().__init__(transport)
        self.allow_motion = allow_motion

    async def arm(self, **kwargs):
        if not self.allow_motion:
            raise ToolPolicyRejected("physical_motion_not_enabled")
        return await super().arm(**kwargs)

    async def execute(self, *args, **kwargs):
        if not self.allow_motion:
            raise ToolPolicyRejected("physical_motion_not_enabled")
        return await super().execute(*args, **kwargs)


def providers(args):
    if args.mock:
        if args.allow_motion or args.provider != "local":
            raise ValueError("Mock forbids physical Arm and paid providers")
        asr, tts = create_offline_speech(mock=True, text=args.mock_text)
        return asr, tts, ScriptedProvider("Offline integration test. No cloud service is active.")
    if not args.live:
        raise ValueError("explicit --mock or --live is required")
    # Validate local configuration only: constructing adapters sends no request.
    for name in ("DASHSCOPE_API_KEY", "DASHSCOPE_VOICE_ID"):
        if not os.environ.get(name):
            raise ValueError(f"{name} is not configured")
    asr = DashScopeFunAsr(model=os.environ.get("DASHSCOPE_ASR_MODEL", "fun-asr-realtime"))
    tts = create_dashscope_synthesizer()
    brain = create_provider(args.provider, model=args.model)
    return asr, tts, brain


async def console(prompt):
    # Windows nonblocking input: no immortal executor input thread on Ctrl+C.
    if sys.platform == "win32" and sys.stdin.isatty():
        import msvcrt
        print(prompt, end="", flush=True)
        text = ""
        while True:
            if msvcrt.kbhit():
                char = msvcrt.getwch()
                if char == "\x03":
                    raise KeyboardInterrupt
                if char in ("\r", "\n"):
                    print(); return text
                if char in ("\x00", "\xe0"):
                    msvcrt.getwch()
                elif char == "\b":
                    if text:
                        text = text[:-1]; print("\b \b", end="", flush=True)
                else:
                    text += char; print(char, end="", flush=True)
            await asyncio.sleep(.03)
    return await asyncio.to_thread(input, prompt)


def make_session(client, asr, tts, provider, sink, *, link=None, wake_word="TARS|塔斯"):
    return VoiceSession(TarsBrain(client, provider=provider), asr, tts, sink,
                        UtteranceCollector(silence_ms=500), wake_word=wake_word,
                        dialogue_publisher=link.display_dialogue if link else None,
                        interaction_publisher=link.interaction if link else None)


async def operator_command(text, client, allow_motion):
    if text.startswith("/arm"):
        if not allow_motion or text != f"/arm {MOTION_CONFIRMATION}":
            print("Arm denied: explicit --allow-motion and physical safety confirmation required.")
        else:
            print((await client.arm()).message)
    elif text == "/disarm":
        print((await client.disarm()).message)
    elif text == "/stop":
        print((await client.estop()).message)
    elif text == "/state":
        print((await client.state()).message)
    elif text == "/diag":
        await save_diagnostics(client)
    else:
        return False
    return True


async def save_diagnostics(client, directory=None):
    """Read-only protocol queries; no cloud, Arm, replay or secret material."""
    try:
        boot_id=client.boot_id
        if not isinstance(boot_id,str) or len(boot_id)!=16 or any(c not in '0123456789abcdefABCDEF' for c in boot_id):
            print("No valid boot identity for diagnostics.")
            return None
        records={}
        for view in ("boot", "previous", "current"):
            result=await client.diagnostics(view)
            if not result.succeeded:
                print("Diagnostic query unavailable on this firmware.")
                return None
            records[view]=result.message.get("payload",{}).get("result",{})
        if client.boot_id!=boot_id:
            print("Boot changed during diagnostic capture; incomplete record discarded.")
            return None
        now=datetime.now(timezone.utc)
        report={"captured_utc":now.isoformat(),"boot_id":boot_id,"records":records}
        directory=Path(directory) if directory else Path(__file__).resolve().parents[1]/"evidence/runtime-diagnostics"
        directory.mkdir(parents=True,exist_ok=True)
        path=directory/(now.strftime('%Y%m%dT%H%M%S%fZ')+'-'+boot_id+'.json')
        path.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding="utf-8")
        print("Diagnostics: "+json.dumps(records,ensure_ascii=False))
        print("Diagnostic record saved: "+str(path))
        return report
    except (ConnectionError,TimeoutError):
        print("Diagnostic capture interrupted; no action retry.")
    except OSError:
        print("Diagnostic file could not be saved; no action retry.")
    return None


async def simulated_turn(asr, tts, provider):
    pc, peer = memory_transport_pair()
    device, client = SimulatedTarsDevice(peer), TarsClient(pc)
    try:
        await device.start(); await client.start()
        sink = MemoryAudioSink()
        session = make_session(client, asr, tts, provider, sink)
        pcm = make_test_tone(300, volume_pct=10) + bytes(32000)
        frames = [pcm[i:i + ASR_PCM.frame_bytes] for i in range(0, len(pcm), ASR_PCM.frame_bytes)]
        turn = await session.run_turn(MemoryAudioSource(frames))
        print(f"Mock transcript: {turn.transcript}\nTARS: {turn.response.text}\nSpeech error: {turn.speech_error}")
        return turn
    finally:
        await client.close(); await device.close()


async def interact(args, client, link, session, source):
    continuous = False
    turn_task = None
    input_task = asyncio.create_task(console("TARS > "))
    disconnect_task = asyncio.create_task(link.closed.wait())
    try:
        while not link.closed.is_set():
            waiting = {input_task, disconnect_task}
            if turn_task:
                waiting.add(turn_task)
            done, _ = await asyncio.wait(waiting, return_when=asyncio.FIRST_COMPLETED)
            if disconnect_task in done:
                raise ConnectionError("paired_link_lost")
            if input_task in done:
                text = input_task.result().strip()
                if text in {"/quit", "quit", "exit"}:
                    return
                if text in {"/stop", "/disarm", "/pause"}:
                    continuous = False
                    session.barge_in()
                    # Safety frame first, before waiting for capture/TTS cleanup.
                    if text == "/pause":
                        await client.disarm()
                    else:
                        await operator_command(text, client, args.allow_motion)
                    if turn_task:
                        turn_task.cancel()
                        await asyncio.gather(turn_task, return_exceptions=True)
                        turn_task = None
                elif await operator_command(text, client, args.allow_motion):
                    pass
                elif text in {"", "/listen"}:
                    continuous = continuous or text == "/listen"
                    if not turn_task:
                        print("Preparing microphone; wait for LISTENING on screen.")
                        turn_task = asyncio.create_task(session.run_turn(source))
                else:
                    print("Use Enter for voice or a listed operator command.")
                input_task = asyncio.create_task(console("TARS > "))
            if turn_task and turn_task in done:
                try:
                    turn = turn_task.result()
                    print(f"You: {turn.transcript}\nTARS: {turn.response.text}")
                    if turn.speech_error:
                        print(f"Speech failed: {turn.speech_error}; disarming, no action replay.")
                        print('TTS diagnostic: '+json.dumps(speech_error_details(session.synthesizer)))
                        await client.disarm(); continuous = False
                    if (client.hello or {}).get("payload",{}).get("firmware_version") in {"0.15.2-diag","0.15.3-gaitlab","0.15.4-drop30","0.15.5-turnlab","0.15.6-dropramp","0.15.7-freeangles","0.15.8-turnsmooth","0.15.9-voicegait"}:
                        await save_diagnostics(client)
                except VoiceStageError as error:
                    print(f"Voice failed: {error.stage}/{error.code}; disarming, no action replay.")
                    if error.stage=='asr':print('ASR diagnostic: '+json.dumps(speech_error_details(session.recognizer)))
                    await client.disarm(); continuous = False
                turn_task = None
                if args.once:
                    return
                if continuous and not link.closed.is_set():
                    turn_task = asyncio.create_task(session.run_turn(source))
    finally:
        input_task.cancel()
        disconnect_task.cancel()
        if turn_task:
            turn_task.cancel()
        await asyncio.gather(input_task, disconnect_task, *([turn_task] if turn_task else []), return_exceptions=True)


async def run(args):
    if getattr(args, "voice_mode", "legacy") == "doubao":
        from .doubao_runtime import run as run_doubao
        return await run_doubao(args)
    if not args.robot and (args.live or args.allow_motion):
        raise ValueError("live body interaction requires explicit --robot")
    asr, tts, provider = providers(args)
    server = None
    try:
        if not args.robot:
            return await simulated_turn(asr, tts, provider)
        if not args.config:
            raise ValueError("--config is required for the paired PCB")
        profile = load_profile(args.config)
        server = LanAudioServer(profile["token"])
        await server.start(profile["host"], profile["port"])
        print("Paired LAN body runtime; no USB port opened. New connections are disarmed.")
        print(f"Speech={'MOCK' if args.mock else 'DashScope'}; Brain={args.provider}; motion permission={args.allow_motion}")
        print("Enter: one voice turn; /listen: continuous; /pause /state /diag /stop /disarm /quit")
        if args.mock:
            print("MOCK ASR/brain/TTS: fixed transcript and test tone, NOT real recognized speech.")
        print(f"Arm (only with --allow-motion): /arm {MOTION_CONFIRMATION}")
        while True:
            try:
                link = await server.wait_connected(60)
            except TimeoutError:
                continue
            client = RuntimeClient(NetworkJsonTransport(link), allow_motion=args.allow_motion and not args.mock)
            try:
                await client.start()
                await client.disarm()  # explicit per-connection fail-safe, never auto-Arm
                if (client.hello or {}).get("payload",{}).get("firmware_version") in {"0.15.2-diag","0.15.3-gaitlab","0.15.4-drop30","0.15.5-turnlab","0.15.6-dropramp","0.15.7-freeangles","0.15.8-turnsmooth","0.15.9-voicegait"}:
                    await save_diagnostics(client)
                source = BodyMicrophone(link, gain=args.mic_gain)
                session = make_session(client, asr, tts, provider,
                                       BodySpeaker(link, volume_pct=args.volume), link=link)
                await interact(args, client, link, session, source)
                if not link.closed.is_set() or args.once:
                    return
            except (ConnectionError, TimeoutError):
                print(f"Connection lost ({link.close_reason or 'control_link_lost'}); waiting to reconnect. No action replay or automatic Arm.")
            finally:
                if not link.closed.is_set():
                    try:
                        await asyncio.wait_for(client.disarm(), 1)
                    except Exception:
                        pass
                await client.close()
    finally:
        if server:
            await server.close()
        await provider.aclose()


def parser():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--voice-mode", choices=("legacy", "doubao"), default="legacy")
    p.add_argument("--doubao-config", default="doubao.example.json")
    p.add_argument("--doubao-seconds", type=int, default=180, help="live test wall-clock cap, 1..600 seconds")
    p.add_argument("--approve-doubao-cost", action="store_true")
    p.add_argument("--speaker-compatible", action="store_true", help="operator verified SC2.0 registration")
    p.add_argument("--standalone-verified", action="store_true", help="operator completed standalone voice evaluation")
    mode = p.add_mutually_exclusive_group(required=True)
    mode.add_argument("--mock", action="store_true", help="no cloud; no motion Arm")
    mode.add_argument("--live", action="store_true", help="explicit paid DashScope ASR/TTS permission")
    p.add_argument("--robot", action="store_true", help="connect real paired PCB; otherwise entirely simulated")
    p.add_argument("--config")
    p.add_argument("--provider", choices=("local", "openai", "deepseek"), default="local")
    p.add_argument("--model")
    p.add_argument("--allow-motion", action="store_true")
    p.add_argument("--volume", type=int, choices=range(1, 21), default=10)
    p.add_argument("--mic-gain", type=float, default=4)
    p.add_argument("--mock-text", default="Hi TARS, introduce yourself")
    p.add_argument("--once", action="store_true")
    return p


def main():
    try:
        # This runtime uses TCP and console polling, not asyncio subprocess pipes.
        # Avoid Windows Proactor overlapped-pipe cancellation at console shutdown.
        factory = asyncio.SelectorEventLoop if sys.platform == "win32" else None
        with asyncio.Runner(loop_factory=factory) as runner:
            runner.run(run(parser().parse_args()))
    except KeyboardInterrupt:
        pass
    except EOFError:
        print("Console input closed; runtime exited. Restart with console input attached; do not enter /arm at the Windows prompt.")
    except Exception as error:
        raise SystemExit(f"Runtime stopped ({type(error).__name__}); no credentials logged.") from None


if __name__ == "__main__":
    main()
