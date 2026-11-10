# 0.16.3-motionpack current motion contract

See ../../VOICE_INTEGRATION.txt (package root) and actions.json / motion-settings.json.
`motion.execute` accepts gait names and all 23 built-in emotion names.
Emotion payload: `{"name":"hello","steps":1,"speed_pct":100,"support_confirm":true}`.
Support confirmation is required only for entries marked requires_support.
Arm and the live session lease remain mandatory. `motion.catalog` returns hash/count.
Normal forward/left/right gaits smooth ALL SIX phases, 300 ms per phase, no extra hold.
Emotion motions finish with outputs disabled; normal gait completion retains Arm until disarm/lease expiry.
The historical no-interpolation notes below describe older releases, not this version.
Audio and motion still use mutual exclusion; concurrent speaking/moving is not implemented.

---

# TARS JSON protocol v2 — USB and paired LAN

0.15.2-diag adds read-only `diagnostic.get`, payload `{"view":"boot"}`,
`{"view":"current"}` or `{"view":"previous"}` (boot is default). Standard
session/boot checks, ACK/terminal, dedup and512-byte limits remain. Boot returns
SDK reset code/name, uptime, RTC previous-valid flag. Trace views return a
best-effort commanded-stage breadcrumb, NOT servo feedback/voltage telemetry.
No reset, movement or flash write is performed by this query.

0.15.1-gaitrestore restores the user's original direct-target, fixed300ms/phase
gait. `speed_pct` remains a validated10..100 compatibility field but does NOT
retime these gaits. No interpolation. See `docs/GAIT_RESTORE_STATUS_2026-11-07.md`.

Integrated candidate 0.15.0 adds TWA1 CONTROL_JSON=40 / CONTROL_RESULT=41,
each carrying one unchanged JSON v2 document (max512 bytes). No USB prefix is
used inside TWA1. The paired connection remains nonce/HMAC protected, not encrypted.
USB and LAN share one controller owner; a new session disarms before takeover.
Only the owner can renew its lease/execute commands. Expiry closes a LAN owner
connection; reconnect never restores Arm. Calibration, Wi-Fi provisioning and
audio self-test require physical USB. See `docs/INTEGRATED_RUNTIME_2026-11-07.md`.

Stage 4 retains the framing/session/ACK/dedup contract below. Additive commands:

- `interaction.set`, payload `{"state":"IDLE|LISTENING|THINKING|SPEAKING"}`
  (choose one literal), updates the existing dual-screen snapshot path.
- `audio.wifi.configure`, physical-USB-only profile provisioning; see
  `docs/WIFI_AUDIO_PROTOCOL.md`. Requires idle/disarmed state, no active audio
  job/session; saves to NVS for the next manual reboot. No credential echo.
- Existing `audio.self_test` now executes asynchronously in an audio worker;
  its ACK means accepted, terminal means software I/O completed, NOT physical
  sound verification. RMS/peak/DC statistics are now PCM16 units. USB estop,
  disarm and session loss abort/mute audio; Arm is refused during audio use.

Wireless PCM is NOT sent in these JSON frames and does not accept motion
commands. Its independent TWA1 wire/security contract is in the linked document.

PCB V1 uses the USB Serial/JTAG driver on GPIO19/20, not UART1 or TinyUSB.
One TX task owns all application USB writes. Application console VFS output is
disabled; ESP_LOG output uses the mux. Physical lines are explicitly separated:

```text
@TARS2 {JSON v2 body}\n
@LOG2 sanitized diagnostic text\n
```

The JSON body limit remains 512 UTF-8 bytes (prefix excluded). TX emits an extra
newline before each frame to resynchronize after partial output. PC demux ignores
raw ROM/startup text and accepts no unprefixed JSON. Logs are bounded/droppable;
protocol TX congestion/partial writes fail the control link and disarm. Logs do
not wait on USB. Normal stdout/printf is not an application protocol/log channel.
USB's 115200 line coding is not its physical bus speed.

Handshake JSON body:

```json
{"v":2,"type":"session.open","command_id":"open-123","payload":{"session_id":"pc-random-123"}}
```

Session IDs follow the command-ID syntax/length. A NEW session always disarms,
including USB reconnects with the same boot ID; repeated opens of the SAME active
session only repeat hello. Hello echoes session ID in `payload.session_id`.
All later requests include this ID at the root; responses echo it at the root.
PC ignores hello/responses from older sessions. This is a freshness boundary,
not cryptographic authentication. `session.open` alone omits target_boot_id.

```json
{"v":2,"type":"state.get","target_boot_id":"BOOT_ID_FROM_HELLO","session_id":"pc-random-123","command_id":"pc-0005","payload":{}}
{"v":2,"type":"dialogue.append","target_boot_id":"BOOT_ID_FROM_HELLO","session_id":"pc-random-123","command_id":"pc-0006","payload":{"role":"user","text":"我们要执行什么任务？"}}
```

Motion/Arm/cancel/estop/clear retain their v2 payloads and require the same root
boot/session fields. New connection = new session ID + new command IDs. ACK retry
within a session reuses exactly the original wire body. Commands pending at link
loss/reboot have unknown outcomes and are never resent automatically. Explicit
Arm is required again; a 2-second heartbeat lease still disables outputs.

`dialogue.append` is display-only and never arms or moves the robot. `role` is exactly `user` or `tars`.
`text` is 1..300 UTF-8 bytes after JSON decoding. C0/C1 control characters, malformed UTF-8, and
overlong text are rejected. The complete NDJSON line must still fit the 512-byte protocol limit.
The right ST7735 keeps a bounded recent history and wraps text; it does not animate UTF-8 byte-by-byte.

Responses use `command.ack` followed by exactly one terminal `command.completed`, `command.failed`, or `command.cancelled` for accepted commands. Rejected ACK is already terminal.

The client must wait for `hello`, copy its random `boot_id` into `target_boot_id`, and never reuse an old request after the boot ID changes. A mismatch is rejected before any side effect. The device caches 32 command IDs. An exact wire-level retry replays the latest response without re-executing motion. Reusing an ID with different bytes is rejected as `command_id_conflict`. JSON key ordering and whitespace therefore need to remain identical on retry.

`safety.clear` clears only the software e-stop and never resumes an interrupted action. This personal prototype has no physical e-stop; keep servo power manually disconnectable during testing.

## Manual servo calibration extension (2026-11-07)

Additive JSON v2 commands, same boot/session/command ID, ACK, terminal and bounded
32-entry dedup semantics. `hello.payload.servo_calibration_v1=true` advertises
this candidate. Full operator guide: `../tars_companion/SERVO_CALIBRATION.md`.

| Type | Payload | Effect |
| --- | --- | --- |
| `calibration.begin` | `servo`: integer1..4, `confirm`: true, `calibration_id`: fresh valid ID | Reject armed/busy/estop/audio/calibration modes; output reference0 on only this axis |
| `calibration.set` | `calibration_id`, `angle_deg`: integer−20..20 | Direct physical reference coordinate, hard limits retained; no gait offsets/directions |
| `calibration.renew` | `calibration_id` | Renew independent1200ms calibration lease, only while active and not expired |
| `calibration.end` | `{}` | Cancel calibration and force outputs low; does not operate ordinary gait mode |
| `calibration.status` | `{}` | Query active, timed_out, estop, servo, angle_deg, pulse_us |

Success yields accepted ACK plus command.completed with payload.result containing
those fields. Failure yields rejected ACK (`calibration_denied` or unknown_type).
angle_deg/pulse_us describe commanded target, never measured position. The previous
target fields may remain after stopping; active=false/servo=0 means no enabled axis.
The webpage clears its current target on disable or session loss. Software emergency
stop and global disable retain safety.estop/clear/disarm semantics; clearing does
not enable any output. calibration.end is intentionally not a general disarm.

Only calibration.renew extends this lease. Session keepalive and other requests
cannot do so. All new handlers run after session/boot checks and dedup lookup;
replaying a cached begin/set/renew response never re-enables, moves or extends a
lease. Audio admission shares the calibration gate to prevent cross-task races.
Reference mapping:500+(angle_deg+90)*2000/180 with integer truncation,50Hz,
reference0=1500us. Calibration bypasses gait zeros. On 2026-11-07 the user explicitly approved updating the shared gait zeros to +10/0/−10/+2; see docs/GAIT_CALIBRATION_HANDOFF_2026-11-07.md.

No NVS calibration write, LLM tool registration, Wi-Fi angle surface, or automatic
mechanical-zero search. No physical angle-feedback sensor exists.
