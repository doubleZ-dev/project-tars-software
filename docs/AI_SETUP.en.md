# 0.18.3-public: PC AI, API and Doubao Voice Setup

[中文](AI_SETUP.zh-CN.md) · [Project home](../README.en.md)

## Package and validation status

Download the [complete software ZIP](https://github.com/doubleZ-dev/project-tars-software/releases/download/v0.18.3-public/TARS-0.18.3-public.zip). It replaces the separate firmware/debug archives and includes matching source, daily voice Companion and USB tools, without the filming director. The bundled `AI_INSTALL.txt` and `DOUBAO_VOICE.txt` provide detailed Chinese instructions.

The provider reports successful compilation and local checks. Full 0.18.3 hardware and user-account cloud acceptance remain pending. A successful `--check` or `--mock` run is not hardware validation.

## 1. Installation and flashing

Use Windows 10/11 x64, Python 3.13 x64 and TARS PCB V1 / ESP32-S3 N16R8. Extract into a writable directory and run root `Install.cmd`. It creates `companion/.venv`, installs pinned dependencies and downloads/verifies the official ASR model (about155 MB download,239 MB extracted). The PC and robot need a trusted LAN and2.4 GHz WPA2 Wi-Fi. Reserve a stable PC IPv4 address.

Disconnect servo power during flashing/provisioning and power the controller over USB. From the package root, discover and confirm your own port and MAC:

```powershell
companion\.venv\Scripts\python.exe -m serial.tools.list_ports -v
companion\.venv\Scripts\python.exe -m esptool --chip esp32s3 --port COMx read_mac
companion\.venv\Scripts\python.exe tools\flash.py --port COMx --device-mac YOUR_DEVICE_MAC
```

The last command only shows the plan. Add `--apply` after confirming the target and flashing operation. The tool checks hashes, chip, MAC and16 MB flash and verifies writes. Addresses: bootloader `0x0`, partition table `0x8000`, application `0x10000`; DIO/80 MHz/16 MB, without full-chip erasure.

## 2. First USB pairing and certificates

Stop competing services and recheck the USB port after flashing. From `companion`:

```powershell
.venv\Scripts\python.exe -X utf8 -m tars_companion.setup_public --port COMx
```

Enter your SSID, hidden Wi-Fi password and PC IPv4 locally. The tool reads the robot identity and generates a random token and local TLS key/certificate. USB provisions the public certificate and network profile; the private key stays in `data/tls/`. Restart the robot manually afterward. **No original-author credentials or custom firmware rebuild are needed.** Allow only private-LAN TCP8765/8766 through the firewall; no public port forwarding is required.

After a PC address change, stop services, privately back up and move the previous `wifi-audio.local.json` and `data/tls`, and provision again. Windows DPAPI credentials are not transferable to another PC. Keep TLS verification enabled.

## 3. API and voice cloning

1. Create your own Doubao Voice API key in the Volcengine console and enable realtime voice dialogue.
2. For a cloned voice, enable voice cloning2.0, train with your own or authorized samples, audition and keep your Speaker ID.
3. Confirm access to that voice in both realtime dialogue and synthesis; synthesis success alone is insufficient.
4. Run `companion/Configure-Voice.cmd`. Enter the key in its local secure prompt, then your Speaker ID and matching TTS resource. Cloning2.0 uses `seed-icl-2.0`; preset voices need their own documented resource.
5. The key is encrypted with the current Windows user's DPAPI and is not written into firmware. Voice settings go into `voice.local.json`; persona instructions can be edited in `tars-seeduplex.local.json`.

`token_budget` is a local conversation ceiling (minimum100000), not free cloud credit. Cached prompt synthesis is billed separately according to the account. Enable voice motion only after calibration and checking movement clearance; chat remains available without it.

The package uses the Realtime JSON event protocol, `X-Api-Key` and session model `1.2.6.1`. Confirm account/API compatibility in the [official realtime guide](https://docs.volcengine.com/docs/doubao-voice/end-to-end-real-time-voice-full-duplex-version?lang=zh), [console guide](https://docs.volcengine.com/docs/doubao-voice/console-guidance?lang=zh) and [voice-cloning guide](https://docs.volcengine.com/docs/doubao-voice/sound-replica-api-v3?lang=zh).

## 4. Check and start

From `companion`, run in order:

```powershell
.\Start-Voice.cmd --check
.\Start-Voice.cmd --prepare
.\Start-Voice.cmd
```

`--check` validates local configuration, certificates, models and Opus without cloud calls or motion. `--prepare` uses your cloud account to generate/cache prompts and may incur charges. On normal startup wait for the listening message, test chat, then supervise one short motion command. Keep the PC running and awake.

Forward/turn commands and counts are recognized offline on the PC. Other utterances are sent to Doubao. Completion audio plays after the motion finishes. **Microphone input pauses during motion/playback: speaking while moving and voice emergency-stop are not supported.** Use `Stop-Voice.cmd` or Ctrl+C to stop the service; this is not a validated mechanical emergency stop. Device lease behavior still needs hardware acceptance.

## 5. USB tools, builds and troubleshooting

Stop voice services before running `Start-Servo-Debug.cmd` or `Start-Gait-Debug.cmd`, one at a time. `--mock` is simulation only. To build, enter the package's `firmware-source` in ESP-IDF5.5.3 and run `idf.py build`; first build downloads locked managed components.

| Symptom | Check |
| --- | --- |
| Busy USB or failed provisioning | Actual port, post-flash re-enumeration, competing programs |
| TLS/connection failure | PC address, paired certificate/device identity, private-network firewall; USB reprovisioning |
| Dialogue/synthesis failure | API project, enabled services, Speaker ID and resource permissions |
| Missing model | Rerun Install.cmd; retain hash verification |
| Unexpected motion | Stop and inspect zero calibration, wiring and mechanism; do not move hardware to identify it |

## Privacy and license

Redistribute only the original release ZIP. Do not re-upload generated `*.local.*`, `data/`, `models/` or `.venv/`, especially credentials, Wi-Fi passwords, tokens, private keys, memory databases, recordings or logs. Original work uses [CC BY-NC-SA4.0](../LICENSE.txt), with separate permission required for commercial use. Third-party software and cloud services retain their own terms.
