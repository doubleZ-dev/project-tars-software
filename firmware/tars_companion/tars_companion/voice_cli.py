from __future__ import annotations

import argparse
import asyncio
import math

from .audio import SoundDeviceMicrophone, SoundDeviceSpeaker, UtteranceCollector, WavAudioSink, WavAudioSource
from .calibration import calibrate_source
from .brain import TarsBrain
from .client import TarsClient
from .llm import DeepSeekProvider, LocalFallbackProvider
from .memory import MemoryStore
from .simulator import SimulatedTarsDevice
from .speech import DashScopeFunAsr, create_dashscope_synthesizer
from .transport import UsbSerialTransport, memory_transport_pair
from .voice import VoiceSession
from .voice import VoiceStageError
from .observability import CompositeObserver, MetricsObserver, SafeJsonlObserver


ARM_CONFIRMATION = "I_HAVE_LIFTED_TARS_AND_CAN_CUT_SERVO_POWER"


def _audio_device(value: str) -> int | str:
    """Let users select PortAudio devices by stable numeric index or by name."""
    try:
        return int(value)
    except ValueError:
        return value


async def run(args: argparse.Namespace) -> None:
    if getattr(args, "mock", False):
        if args.arm_confirm or args.deepseek or args.port or args.usb_serial:
            raise SystemExit("--mock forbids cloud, hardware selection and Arm")
        from .voice_mock import run_mock
        await run_mock(args.mock_text, args.output_wav)
        return
    if not getattr(args, "legacy_cloud", False):
        raise SystemExit("ASR/TTS are unselected. Use --mock for fully offline testing; historical cloud mode requires --legacy-cloud.")
    device = None
    client = None
    memory = None
    microphone = None
    try:
        if args.input_wav and not args.simulate:
            raise SystemExit("--input-wav is restricted to --simulate")
        if args.input_wav and args.calibrate_seconds:
            raise SystemExit("live calibration cannot share --input-wav")
        if not args.simulate and not args.wake_word:
            raise SystemExit("real mode requires a non-empty wake word")
        if not math.isfinite(args.vad_threshold) or not 200 <= args.vad_threshold <= 20_000:
            raise SystemExit("--vad-threshold must be finite and within 200..20000")
        microphone = (WavAudioSource(args.input_wav) if args.input_wav else
                      SoundDeviceMicrophone(device=args.input_device))
        threshold = args.vad_threshold
        if args.calibrate_seconds:
            print(f"正在预热并标定 {args.calibrate_seconds:.1f} 秒，请保持安静。")
            calibration = await calibrate_source(microphone, args.calibrate_seconds)
            if not calibration.usable:
                raise SystemExit(f"audio calibration rejected: {','.join(calibration.warnings)}")
            threshold = calibration.threshold
            print(f"标定完成：noise={calibration.noise_rms:.0f}, VAD threshold={threshold:.0f}")
        if args.simulate:
            pc, device_side = memory_transport_pair()
            device = SimulatedTarsDevice(device_side)
            await device.start()
        else:
            pc = UsbSerialTransport(args.port, args.baud, serial_number=args.usb_serial)
        client = TarsClient(pc)
        memory = MemoryStore(args.memory)
        await client.start()
        hello_payload = (client.hello or {}).get("payload", {})
        if ((hello_payload.get("firmware_version"), hello_payload.get("release_id")) not in {
                ("0.12.0-stage2", "tars-pcb-v1-stage2"), ("0.14.0-stage4", "tars-pcb-v1-stage4")} or
                hello_payload.get("boot_disarmed") is not True):
            raise SystemExit("firmware compatibility check failed; expected PCB V1 Stage 2 boot-disarmed")
        if args.arm_confirm:
            if args.arm_confirm != ARM_CONFIRMATION:
                raise SystemExit(f"arming requires --arm-confirm {ARM_CONFIRMATION}")
            armed = await client.arm()
            if not armed.succeeded:
                raise SystemExit("device refused safety.arm")
            print("运动已显式解锁；重启或急停后会自动重新锁定。")
        provider = DeepSeekProvider() if args.deepseek else LocalFallbackProvider()
        metrics = MetricsObserver()
        observer = CompositeObserver(metrics, SafeJsonlObserver(args.event_log)) if args.event_log else metrics
        sink = WavAudioSink(args.output_wav) if args.output_wav else SoundDeviceSpeaker(args.output_device)
        recognizer = DashScopeFunAsr(workspace_id=args.workspace)
        session = VoiceSession(TarsBrain(client, provider=provider, memory=memory),
                               recognizer,
                               create_dashscope_synthesizer(args.voice, workspace_id=args.workspace,
                                                            model=args.tts_model),
                               sink, UtteranceCollector(threshold=threshold,
                                                        silence_ms=args.silence_ms),
                               wake_word=args.wake_word, observer=observer,
                               dialogue_publisher=client.display_dialogue,
                               interaction_publisher=client.interaction)
        print(f"TARS Voice ready. Ctrl+C 退出；说完后保持约 {args.silence_ms / 1000:.1f} 秒安静。")
        completed_turns = 0
        while True:
            try:
                flush = getattr(microphone, "flush", None)
                if flush:
                    flush()
                turn = await session.run_turn(microphone)
                print(f"你：{turn.transcript}\nTARS：{turn.response.text}")
                completed_turns += 1
                if turn.speech_error:
                    print(f"[语音输出异常：{turn.speech_error}；已执行的动作不会重放]")
                if args.input_wav or (args.max_turns and completed_turns >= args.max_turns):
                    break
            except VoiceStageError as exc:
                if exc.stage == "asr" and recognizer.last_error_code:
                    print(f"[Fun-ASR provider code: {recognizer.last_error_code}]")
                suffix = "未发送新命令" if exc.stage == "asr" else "处理已安全终止"
                print(f"[语音链路异常：{exc.stage}/{exc.code}；{suffix}]")
                if args.input_wav:
                    break
            except (OSError, EOFError, ValueError, RuntimeError) as exc:
                print(f"[本地音频异常：{type(exc).__name__}]")
                if args.input_wav:
                    break
    finally:
        if microphone:
            try: microphone.close()
            except Exception: pass
        if memory:
            try: memory.close()
            except Exception: pass
        if client:
            if args.arm_confirm:
                try: await client.disarm()
                except Exception: pass
            try: await client.close()
            except Exception: pass
        if device:
            try: await device.close()
            except Exception: pass


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--legacy-cloud", action="store_true", help="explicitly opt into the existing cloud speech pipeline; may incur charges")
    parser.add_argument("--mock", action="store_true", help="fully offline ASR/Brain/TTS/body audio; no devices or credentials")
    parser.add_argument("--mock-text", default="介绍一下你的任务", help="fixed Mock ASR transcript")
    parser.add_argument("--simulate", action="store_true", help="simulate robot control, not cloud speech")
    parser.add_argument("--port")
    parser.add_argument("--usb-serial")
    parser.add_argument("--baud", type=int, default=115200)
    parser.add_argument("--voice", help="Bailian voice ID; defaults to DASHSCOPE_VOICE_ID")
    parser.add_argument("--tts-model", help="defaults to DASHSCOPE_TTS_MODEL or voice-ID routing")
    parser.add_argument("--workspace", help="Bailian workspace ID; defaults to DASHSCOPE_WORKSPACE_ID")
    parser.add_argument("--deepseek", action="store_true")
    parser.add_argument("--memory", default="tars_memory.sqlite3")
    parser.add_argument("--vad-threshold", type=float, default=500.0)
    parser.add_argument("--silence-ms", type=int, default=500,
                        help="end an utterance after this much silence (300..1500 ms)")
    parser.add_argument("--calibrate-seconds", type=float, default=0.0,
                        help="capture 1..10 seconds of room silence and derive VAD threshold")
    parser.add_argument("--wake-word", default="TARS|TAS|塔斯",
                        help="required transcript prefix; | separates aliases; empty is bench-only")
    parser.add_argument("--input-device", type=_audio_device,
                        help="PortAudio input device index or unique name")
    parser.add_argument("--output-device", type=_audio_device,
                        help="PortAudio output device index or unique name")
    parser.add_argument("--input-wav", help="single-turn PCM16 mono 16 kHz input instead of microphone")
    parser.add_argument("--output-wav", help="write TTS PCM to WAV instead of speakers")
    parser.add_argument("--event-log", help="safe JSONL metrics only; never stores transcript or response")
    parser.add_argument("--max-turns", type=int, default=0,
                        help="exit after N recognized turns; 0 keeps listening")
    parser.add_argument("--arm-confirm",
                        help="explicit local physical-safety confirmation required before motion")
    args = parser.parse_args()
    if args.max_turns < 0:
        parser.error("--max-turns must be >= 0")
    if not 300 <= args.silence_ms <= 1500:
        parser.error("--silence-ms must be within 300..1500")
    try:
        asyncio.run(run(args))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
