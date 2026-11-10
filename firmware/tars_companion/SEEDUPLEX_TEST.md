# Seeduplex robot test (2026-11-08)

Uses the current web experience's protocol: model `1.2.6.1`, JSON events at
`wss://openspeech.bytedance.com/api/v3/duplex/realtime/dialogue`, `X-Api-Key`
authentication. This is separate from the older `doubao_runtime` SC2.0 binary
protocol. Existing launchers and firmware are unchanged.

Speaker is explicitly `S_vXjewQyk2`; no preset fallback. Persona is in
`tars-seeduplex.json`. Input 16kHz mono PCM16, cloud output 24kHz PCM16,
resampled to the robot's 16kHz PCM16. Current user-accepted playback path buffers
and converts the complete reply before playing, with no extra gain, cloud
loudness 0 and firmware volume 20%. Four PCM commands (512 bytes each) can be
in flight; all must ACK before PLAY_END. Eight-second segmentation retains
the firmware limits; longer replies can still have gaps at those boundaries.
Reply buffer is limited to 30 seconds. This adds first-sound latency compared
with streaming, prioritizing clean speech for the current hardware.

Real diagnostic: the same 4.60975-second Tars sentence took 6.045 seconds using
stop-and-wait, and 4.754 seconds with four packets in flight. User explicitly
confirmed the latter was very clear, requested a repeat and confirmed again.
This supports a transport/playback issue; it does not prove hardware is free
of electrical noise. Do not increase gain again as a substitute for diagnosis.

Cloud supports full duplex but this robot evaluation is half duplex: stop body
capture and send the cloud mute event before playback. There is no AEC or
voice interruption claim. No motion tools; a control session disarms the robot.
Default one turn/60 seconds; `-Turns 3 -Seconds 180` tests three turns in the
same cloud session. Limits are five turns/300 seconds, with a 25-second
microphone upload cap per turn. Each round restarts capture after playback.
Requires an operator-verified remaining free allowance >=100000 tokens.
This guard is not a billing API balance monitor or a monetary spending cap.

Run from this directory:

```powershell
.\Start-Tars-Seeduplex.ps1 -FreeTokens 955544
```

Enter the Speech API Key locally at the hidden prompt once (or set
`DOUBAO_API_KEY` in the launching process). The launcher saves a Windows DPAPI
encrypted SecureString to `doubao-key.credential.local.xml`, ignored by Git.
The file is decryptable by this Windows user on this computer; do not copy it
to another account expecting it to work. Use `-ResetKey` to replace it.
The launcher restores the previous process environment afterward. Do not paste keys
in chat or command arguments. Sanitized output goes to
`evidence/seeduplex-live-last.log`, without audio or transcription storage.

Offline validation completed: 11 tests in `test_seeduplex.py` pass, including
streaming event sequence, capture mute/release, 24k->16k conversion, bounded
playback segments and remote error redaction. Configuration check passed.
API authentication and robot conversation have since succeeded. User reported
quiet/intermittent speech before the transport change. The fixed diagnostic
sentence was accepted; live conversation using the buffered pipeline still
needs user verification. Encrypted-key launcher now waits for Enter before
starting, and shows an explicit TARS test window title.

Later verification: two real-cloud turns in the same session completed using
the saved synthesized diagnostic sentence as ASR input, and the physical robot
acknowledged playback of 138284 and 146344 PCM bytes respectively. User heard
the output and confirmed no problem. This verifies the cloud conversation and
speaker path, but does not replace a live robot-microphone three-turn test.
See `evidence/seeduplex-loopback.log`. Diagnostics identified the live timeout:
cloud had sent `response.output_audio.done` and 533772 PCM bytes, but playback
waited for `response.done` (usage). The conversion worker timed out while
355828 converted bytes remained buffered. Audio completion now starts playback
without waiting for usage. Late usage from the preceding round is ignored;
the regression covers missing and late usage across two rounds.
Capture and cloud upload are decoupled by a bounded queue. Upload pacing uses
absolute deadlines instead of accumulating Windows sleep/write delays.
Local real-device capture/uplink tests do not establish cloud dialogue success.
Cross-day memory is not implemented in this adapter.

Third-turn live failure: TCP EOF (`IncompleteReadError`) interrupted capture.
Board diagnostics showed POWERON with >6700s uptime, so that failure was not a
recent reboot. A local three-cycle microphone/playback test passed (51,51,50
100ms frames and the buffered probe sentence each cycle). The original link
loss cause remains unresolved. Runtime permits one bounded robot reconnect,
opens a fresh disarmed control session, and resumes cloud history via session
ID. It does not replay unfinished speech/audio or repeat completed turns.
If no history ID exists or a second disconnect happens, it stops. A mocked
regression verifies resume ID, fresh disarm and no completed-turn replay.

Official protocol:
https://docs.volcengine.com/docs/doubao-voice/end-to-end-real-time-voice-full-duplex-version?lang=zh
