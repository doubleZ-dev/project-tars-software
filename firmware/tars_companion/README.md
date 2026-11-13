# TARS PC Companion — PCB V1 integrated candidate

2026-11-08：新增豆包日常语音候选入口 `Start-Tars-Daily.cmd`，复用已确认清晰的
Seeduplex 音色和播放链路，加入本地文字记忆、动作工具路由（默认不使能运动）、
空闲暂停与退出、用量检查。使用与验收边界见 [TARS_DAILY.md](TARS_DAILY.md)。
未改动或烧写固件；三轮真人测试及旧启动入口保留。

2026-11-07: unified paired-Wi-Fi voice and guarded JSON v2 control is implemented
and tested offline, **not flashed or hardware-accepted**. Existing DashScope
ASR/TTS remain explicit opt-in; no paid service was called during development.
Current integration guide and test evidence:
[整机候选版状态与配置](../tars_firmware/docs/INTEGRATED_RUNTIME_2026-11-07.md).
Offline entry: `python -m tars_companion.robot_runtime --mock --once`.
The Stage 4 material below describes the earlier baseline, not the latest runtime.

Active source: `<repository>\firmware\tars_companion`.
Native USB communication and Stage 3 text Brain are retained. Stage 4 adds
vendor-neutral speech adapters, offline body-audio mocks and an independent
same-LAN audio channel. No speech model or paid API is selected/called.
Current ledger: [Stage 4 status](../tars_firmware/docs/STAGE4_STATUS.md).
Current installation: [0.18.3 public setup guide](../../docs/AI_SETUP.en.md).

## Stage 4: no cloud credentials required

```cmd
cd /d <repository>\firmware\tars_companion
python -m tars_companion.voice_cli --mock --output-wav evidence\stage4-mock-tone.wav
```

This is fully offline: simulated body microphone/channel, fixed Mock ASR text,
Scripted Brain, generated test tone (NOT speech), simulated speaker and state
sequence. No physical USB/microphone/speaker is opened. `--simulate` alone is
not equivalent to `--mock`; the historical cloud voice path now additionally
requires explicit `--legacy-cloud` and may incur charges.

For future hardware LAN tests, copy `wifi-audio.example.json` to the ignored
`wifi-audio.local.json` and fill Wi-Fi SSID/password, PC LAN IPv4, port (8765),
and a random 32..64-character hex pairing secret. Never commit/share that file.
Generate the pairing secret locally using `secrets.token_hex(16)`; it is not an
API key. Validate without opening any device:

```cmd
python -m tars_companion.wifi_config --config wifi-audio.local.json
```

**Only after separately authorized Stage 4 flashing**, `wifi_config ... --apply`
stores the profile through USB; reboot manually to apply. Then:

```cmd
python -m tars_companion.wifi_voice --config wifi-audio.local.json --mock --once
```

The PC listens on the configured LAN IPv4/port; board connects over the same
trusted private LAN. Enter starts one capture turn. Providers remain MOCK:
actual microphone PCM is collected but not recognized, and speaker output is
a low-volume tone, not a cloned or synthesized voice. No physical motion client.
Audio/state/terminal are wireless; general motion control is not migrated.
No auto-record/playback on connect or reconnect. See the ledger for pairing,
private-network firewall setup, timeout bounds and pending real RF/I2S tests.
TWA1 is authenticated pairing but **not encrypted TLS**; no public exposure.

## Text testing (no microphone or speaker required)

From this directory, the module resolves to this G-drive source, not the archived
release. Default robot mode is simulated; provider `local` makes no API calls:

```cmd
cd /d <repository>\firmware\tars_companion
python -m pip install -e ".[gpt]"
python -m tars_companion.chat --simulate --provider local
```

OpenAI mode uses the official SDK Responses API. It requires your existing
`OPENAI_API_KEY` in the launching terminal and an explicitly selected account-
available `OPENAI_MODEL` (no hardcoded model, no automatic model selection).
Do not paste keys into the chat, command arguments or source files.

```cmd
set "OPENAI_MODEL=your-account-available-model-id"
python -m tars_companion.chat --simulate --openai
```

**`--simulate` replaces the robot only; `--openai` still makes paid network
requests.** Only `--provider local` and the Mock tests are fully offline.
You can also use `--provider openai --model MODEL_ID`. Provider selection can
be supplied via `TARS_LLM_PROVIDER`; without it or a flag, default is `local`.
DeepSeek remains explicit `--deepseek`; no automatic paid fallback is made.

Type English/Chinese questions, then follow up without restarting. Session
history is bounded in RAM by default; `--memory PATH` opts into the unchanged
SQLite store. `intent=` shows deterministic parsing. Available motion: 前进 /
move forward, 左转 / turn left, 右转 / turn right. 停止 / stop uses software
estop. 后退 / backward is explicitly unsupported, not implemented by GPT.

Simulation starts disarmed. To test successful **simulated** motion, enter:
`/arm I_HAVE_LIFTED_TARS_AND_CAN_CUT_SERVO_POWER`, then `前进`, `stop`,
`/state`, `/disarm`, or `quit`. No natural-language request or model output
can Arm. Quoted/negated commands do not execute.

Real hardware is opt-in using `--robot --usb-serial SERIAL`. This opens a
disarmed native session; Arm/motion remain blocked unless `--allow-motion`
is also supplied and the explicit `/arm ...` is entered. Do not enable those
options until separately authorized mechanical testing. No real motion was
performed during Stage 3.

OpenAI requests have a 15-second deadline, zero automatic SDK retries, a
256-token output cap and `store=False`; incomplete/empty replies are failures.
Errors are fixed sanitized codes; no SDK error bodies or keys are printed.
No OpenAI tools/function execution surface is provided. Model output is text
only and never fed back into the action parser. Different models may consume
the output budget differently; a real account/model smoke test is still needed.

Existing film Honesty/Humor values are persona background, not synchronized
PCB settings. The old screen's fixed parameter/battery labels are not telemetry.

## Tests and simulation

```powershell
python -m unittest discover -s tests -v
python -m tars_companion.cli --simulate
python -m tars_companion.chat --simulate --memory tars_memory.sqlite3
```

## Native USB and safe HIL (requires separately authorized installation)

Connect native USB on PCB GPIO19/20. The protected battery must be connected
and the mechanical switch ON; USB VBUS feeds only the charger. No CH340 is used.
Select the device's stable USB serial identity; COM number changes are handled
automatically. VID:PID is 303A:1001. With one device the serial can be discovered
automatically; multiple matches require explicit selection. `--port COMx` is an
optional first-open hint, not a persistent identity. No port opens on import.

```powershell
python -m tars_companion.hil --list-ports
python -m tars_companion.hil --usb-serial 84:C7:BB:77:B5:F0 --output hil-report.json
python -m tars_companion.cli --usb-serial 84:C7:BB:77:B5:F0
```

Default HIL sends session.open/state.get and checks ACK/replay/conflicts. Opening
a NEW control session DISARMS, so this is a no-motion probe, not a state-neutral
operation. Motion probing remains explicitly gated (not run during Stage 2):

```powershell
python -m tars_companion.hil --usb-serial 84:C7:BB:77:B5:F0 --motion `
  --confirm-lifted I_HAVE_LIFTED_TARS_AND_CAN_CUT_SERVO_POWER
```

The transport separates `@TARS2` JSON from `@LOG2` diagnostics; raw boot output is
discarded. Logs are available in `transport.codec.logs` (bounded to 100 entries).
Within-session retry is exact and deduplicated; disconnect/reboot aborts pending
commands as unknown, rediscovers the SAME serial, and performs a fresh disarmed
handshake. Arm is never restored automatically. CLI requires explicit
`arm I_HAVE_LIFTED_TARS_AND_CAN_CUT_SERVO_POWER` after preparation.

Historical raw `SerialTransport` remains for archived-board callers/tests, but
current CLI/HIL/chat/voice entry points route via `UsbSerialTransport`. Changing
those entry points only migrates transport/version checks; no ASR/LLM/TTS is
invoked by transport tests. The old deployment command remains a legacy release
installer and must not be used with this PCB's candidate image.

## Preserved DeepSeek functionality (outside Stage 2)

Set the key only in the terminal that launches Companion:

```powershell
$env:DEEPSEEK_API_KEY="..."
python -m tars_companion.chat --simulate --deepseek
```

DeepSeek handles ordinary conversation. Motion intent is parsed and validated locally; raw actuator instructions are rejected.

## Qwen voice loop

Fun-ASR converts speech to text, the deterministic Brain validates any requested motion, and TTS reads only the verified response. This release supports the cloned-voice snapshot `qwen3-tts-vc-2026-01-22` and keeps the legacy CosyVoice adapter for compatible older IDs.

```powershell
pip install -e ".[voice]"
$env:DASHSCOPE_API_KEY="..."
$env:DASHSCOPE_WORKSPACE_ID="..."
$env:DASHSCOPE_VOICE_ID="qwen-tts-vc-..."
# Optional; qwen-tts-vc-* IDs select it automatically:
$env:DASHSCOPE_TTS_MODEL="qwen3-tts-vc-2026-01-22"
python -m tars_companion.voice_cli --legacy-cloud --simulate --calibrate-seconds 2
```

`--simulate` replaces only the robot/UART. The microphone, Fun-ASR, Qwen-TTS and speaker remain real. Qwen uses its official HTTP stream and returns Base64 PCM16 mono 24 kHz chunks. Input is PCM16 mono 16 kHz with energy VAD, 700 ms trailing silence and a 10 second cap. The default wake aliases are `TARS` and `塔斯`.

The offline doctor checks dependencies, credentials and the voice/model profile without calling the cloud or printing secrets:

```powershell
python -m tars_companion.voice_doctor --audio --deepseek
```

Voice turns are serialized. ASR failure sends no command. TTS/playback failure preserves an already verified robot result, and speech-only retry cannot replay physical motion. Provider error details are replaced with fixed codes; putting credentials in diagnostic strings would be an impressively avoidable design decision.

## Repeatable WAV input

Input WAV must be uncompressed PCM16 mono 16 kHz and is restricted to simulated, single-turn use. Output WAV uses the TTS format.

```powershell
python -m tars_companion.voice_cli --legacy-cloud --simulate `
  --input-wav command.wav --output-wav reply.wav
```

## Servo mechanical-zero calibration (local only)

See [中文启动、操作、安全与验收说明](SERVO_CALIBRATION.md).
Run `python -m tars_companion.servo_calibration` in this directory; use `--mock`
for offline operation. The service listens only on 127.0.0.1:8766, prints a
per-run capability URL, and never opens a serial device before a page request.
Calibration uses new firmware commands, never ordinary `safety.arm`.
