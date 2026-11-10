# Project TARS — Stage 3 OpenAI text brain handoff

Date: 2026-11-06. Scope: Python Companion text interaction only.

## Status and source of truth

- Active source/run directory: `<repository>\firmware\tars_companion`; module origin was checked and resolves here, not C-drive or an archived release.
- PC baseline: commit `d941215` (Stage 2 USB transport). Firmware remains at `89eed78`; no ESP32 source/config/build/flash was changed during Stage 3. The already-dirty firmware status document was left untouched.
- Read historical `<repository>\Project-TARS-Software-Status-Handoff.md` and current Stage 1/2 records. Historical C-drive paths/versions in the audit are superseded by current G-drive sources.
- Stage 2 reality: real native USB handshake/state/ACK/replay/conflict checks passed 7/7 after the zero-tick starvation fix, user confirmed dual-screen Chinese output. Full hotplug/COM reenumeration and physical motion acceptance remain incomplete; the later reconnect check found no target USB device. Stage 3 does not claim to complete those tests.
- **Stage 3 implemented and Mock-verified. Real OpenAI API/account/model verification has NOT been done.** No paid request, hardware connection or real motion occurred.

## Files and behavior

| File | Necessary change |
| --- | --- |
| `tars_companion/llm.py` | Official async OpenAI SDK Responses adapter, explicit model/key configuration, bounded timeout/output, sanitized errors and client cleanup; explicit provider factory. Existing DeepSeek request adapter unchanged. |
| `tars_companion/brain.py` | Bounded in-process history when no existing MemoryStore is supplied; reuse existing history quoting/redaction. Safe disconnection responses, unsupported-backward handling and English live-telemetry guard. |
| `tars_companion/canon.py` | English query aliases retrieve the same existing movie sections; no new fictional history invented. |
| `tars_companion/intent.py` | Add exact stop/停止 and backward/后退 recognition; backward is rejected, not mapped to a nonexistent gait. Retain strict motion whitelist and negation/quotation behavior. |
| `tars_companion/chat.py` | Reuse text CLI: provider/model options, safe simulated default, visible parsed intents, optional real-USB mode, explicit Arm and real-motion opt-in, cleanup and sanitized failures. |
| `pyproject.toml` | Optional `gpt` extra: official `openai>=2.43.0,<3`. Tested local SDK version: 2.43.0. |
| `tests/test_openai.py`, `README.md`, this report | Targeted offline coverage and runnable setup/handoff instructions. |

`client.py`, `tools.py`, `personality.py`, simulator, JSON v2, USB framing/session semantics, ASR/TTS/voice entrypoints and memory DB schema were not modified. Firmware GPIO/UI/fonts/gait are out of scope and unchanged.

## OpenAI interface and model configuration

Responses API was chosen because its text interface fits the existing `ChatProvider.complete(messages)` contract: trusted system messages become `instructions`, normal messages become `input`, and `output_text` is returned. No function/tools surface, GPIO, PWM or serial capability is passed to the model. Model output is never reparsed as a motion request.

- Uses `OPENAI_API_KEY` from the launching process; keys are never printed, copied into files or committed.
- Model must come from `OPENAI_MODEL` or `--model`. No hardcoded model or guessed account availability. The development process had neither variable configured; user confirmed these can be supplied later. An economic/low-latency model must still be selected from the user's actual account before live validation.
- Explicit official endpoint `https://api.openai.com/v1` avoids accidentally routing the OpenAI credential to an inherited third-party base URL.
- 15-second SDK + outer deadline; zero automatic SDK retries; 256 output tokens; `store=False`. No tools or automatic second-provider request.
- Fixed public error codes distinguish timeout, authentication, bad request/model/not-found, access denied, rate limit, connection, API failure, empty and incomplete output. SDK error bodies/headers are not displayed.
- DeepSeek remains selectable with `--deepseek` / `--provider deepseek`; failure does not silently invoke it or OpenAI.

Implementation references: [official text/Responses guide](https://developers.openai.com/api/docs/guides/text), [official error types](https://developers.openai.com/api/docs/guides/error-codes). The fetched text guide informed Responses/instructions/output_text mapping; local SDK interfaces were also inspected.

## Persona, history and actions

Existing TARS film identity, sections, trusted mission retrieval, response cleanup, personality renderer and short history are reused. English is the prompt default; Chinese input requests Chinese answers. Calm/reliable/dry humor/light sarcasm remain; no stage-direction performance. Prompt/telemetry guards prohibit fabricated sensor data or execution claims. Actual stylistic quality cannot be proven by canned Mock replies and remains a live-evaluation item.

Honesty 95/Humor 60 remain existing **film-role facts**, not real PCB settings. No claim is made that the screen's fixed values are synchronized or that runtime parameter controls have been added. No long-term memory feature or database migration was introduced; existing MemoryStore remains an explicit `--memory PATH` option, otherwise history is bounded in RAM.

- Forward/left/right: existing strict parser → `validate_motion` → BrainToolExecutor state guard → TarsClient → existing JSON v2.
- Stop: existing software estop (communication required; not a physical power cut).
- Backward: recognized but explicitly unsupported; no command is sent.
- Arm is not exposed to the model or ordinary natural-language parser. Simulated success testing requires the exact local `/arm ...` command. Real motion additionally requires `--robot --allow-motion` and explicit Arm; reconnect never autoarms.
- Tool failures/disconnects never claim success or automatically replay a command. Negated/quoted requests are not executed. Ordinary model/API errors cannot turn into motion.

## Verification (all offline)

| Suite | Result | Log under `evidence/` |
| --- | --- | --- |
| New OpenAI SDK + text/action tests | 20 passed | `stage3-openai-mock.log` |
| Existing Brain / safety / DeepSeek config | 18 passed | `stage3-brain-regression.log` |
| Existing movie canon | 4 passed | `stage3-canon-regression.log` |
| Existing protocol/client/personality | 21 passed | `stage3-protocol-regression.log` |
| Existing native USB mock | 11 passed | `stage3-usb-regression.log` |
| **Total scoped tests** | **74 passed, 0 failed** | No cloud/hardware traffic |

The OpenAI tests instantiate the **real official SDK** with `httpx.MockTransport`: English, Chinese, follow-up context, persona/movie instructions, responses mapping, missing config, invalid model syntax, 400/401/403/404/429/500, network/overall timeouts, empty/incomplete replies, no retry, DeepSeek adapter preservation, all requested action categories, no-Arm/disconnect guards, non-executable model output, and safe CLI defaults/explicit simulation Arm. They validate request wiring, not actual GPT response quality or account model access.

`python -m tars_companion.chat --help` and a piped local-provider CLI smoke test passed. `git diff --check` passed. No unrelated full voice/audio test suite or device tests were run. Generated evidence is local and Git-ignored.

## Start testing later (Windows CMD)

```cmd
cd /d <repository>\firmware\tars_companion
python -m pip install -e ".[gpt]"
python -m tars_companion.chat --simulate --provider local
```

This is fully offline, not GPT. Once the existing key is available in that same terminal, choose an account-available model ID:

```cmd
set "OPENAI_MODEL=your-account-available-model-id"
python -m tars_companion.chat --simulate --openai
```

**The second command makes paid API calls even with `--simulate`.** The developer did not run it in this stage. Never send a real key in chat, source, arguments or diagnostic logs. Continuous questions keep short context; `intent=` shows parsing. Use `/state`, `/disarm`, `quit`. For simulated action success only:

```text
/arm I_HAVE_LIFTED_TARS_AND_CAN_CUT_SERVO_POWER
前进
turn left
右转
stop
backward
quit
```

The mock-only tests can be reproduced with `python -m unittest discover -s tests -p test_openai.py -v` after installing the GPT extra. No speech setup is required.

## Remaining acceptance / next-stage boundary

1. Supply/confirm local key and a real account-available low-cost model. No actual model is claimed selected.
2. With explicit paid-call permission, at most 3 short live requests: English, Chinese, follow-up; evaluate persona/canon/context and output budget. No retrying alternate models blindly.
3. Preserve Stage 2 physical reconnect/motion acceptance as separate pending work. No automatic real actions during GPT testing.
4. Audio integration is not implemented in Stage 3. ASR/TTS/clone work and Stage 4 were not started.

Software is ready for configuration and an authorized short live text test, not a claim of real OpenAI or full robot-system acceptance.
