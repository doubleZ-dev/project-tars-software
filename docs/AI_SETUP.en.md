# PC Companion, API Keys and Doubao Voice Cloning

[中文](AI_SETUP.zh-CN.md) · [Project home](../README.en.md)

## What runs where

| Component | Location | Cloud account needed? |
| --- | --- | --- |
| Board firmware | ESP32-S3 | Not for USB debugging |
| Servo debug tools | Local Python/web service on the PC, connected by USB | No |
| Voice Companion | Persistent PC service connected to the robot over LAN | Yes for cloud conversation, synthesis and custom voices |

The firmware and servo-debug ZIPs do not install a voice Companion. The author is preparing the final public voice delivery. This guide documents configuration principles and existing repository entry points; historical source has not been verified as the exact source of `0.18.2-final65`.

## 1. PC and dependencies

The PC and robot must communicate over a LAN. The robot uses 2.4 GHz Wi-Fi, and cloud conversation needs internet access. Reserve a stable LAN IPv4 address for the PC to avoid address/certificate mismatches after reboot.

Existing PC code is in `firmware/tars_companion`. From that directory:

```powershell
py -3.13 -m venv .venv
.\.venv\Scripts\python.exe -m pip install ".[daily]"
```

The USB debug package requires Python 3.11 or later. Follow the final voice package's dependency requirements when available. Wake-word/recognition models are downloaded separately and must match the service version; an old wake-word model is not interchangeable with a newer recognition model. The historical [voice guide](../firmware/tars_companion/TARS_VOICE_SERVICE.md) is reference, not a claim of final-package compatibility.

## 2. LAN pairing

Copy `wifi-audio.example.json` to `wifi-audio.local.json` and enter your own values:

```json
{
  "ssid": "YOUR_2_4_GHZ_WIFI",
  "password": "YOUR_WIFI_PASSWORD",
  "host": "192.168.1.100",
  "port": 8765,
  "token": "REPLACE_WITH_YOUR_RANDOM_PAIRING_TOKEN"
}
```

The IP above is only an example. Generate your own token:

```powershell
.\.venv\Scripts\python.exe -c "import secrets; print(secrets.token_hex(32))"
```

Provision your own device over USB:

```powershell
.\.venv\Scripts\python.exe -m tars_companion.wifi_config --config wifi-audio.local.json --usb-serial YOUR_USB_SERIAL --apply
```

Restart the robot afterward and allow the Companion on your Windows private network. Entry points requiring `TARS_DEVICE_ID` need your robot's MAC address, not the author's identifier.

## 3. Match the PC TLS certificate and firmware

The supplied final application binary trusts the original PC Companion's public certificate. **Changing Wi-Fi credentials, a pairing token and an API key alone does not make that binary work with a fresh PC.** The original private key is not distributed.

The existing repository provides `firmware/prepare_xiaozhi_tls.py`. It reads your local pairing profile, generates credentials under the PC's `data/tls/`, and places only the public certificate into firmware source. From the repository root:

```powershell
firmware\tars_companion\.venv\Scripts\python.exe firmware\prepare_xiaozhi_tls.py
```

Then use an ESP-IDF 5.5.3 terminal to build the historical project in `firmware/tars_director_firmware`. **This describes the current source's certificate mechanism; it is not the final standalone voice release.** Its exact replacement build/pairing entry point will be updated when the final package is supplied.

Keep your certificate and private key together. An address change, lost key or regenerated certificate requires updating the corresponding firmware trust. Do not disable certificate verification to work around pairing.

## 4. Create your Doubao API key

In the Doubao Voice / Volcengine console, select your project, create a key under API management and enable the required voice services. Keys, services and voices must belong to a project you can access. [Official console guide](https://docs.volcengine.com/docs/doubao-voice/console-guidance?lang=en)

The voice entry point reads `DOUBAO_API_KEY`. Existing `Start-Tars.ps1 -Setup` supports hidden entry and local Windows-account encryption. For a temporary PowerShell session:

```powershell
$tarsSecret = Read-Host 'Doubao API Key' -AsSecureString
$env:DOUBAO_API_KEY = [System.Net.NetworkCredential]::new('', $tarsSecret).Password
$tarsSecret.Dispose()
```

Start the service from that same terminal. After stopping it, use `Remove-Item Env:DOUBAO_API_KEY` to clear the session variable. Never publish the real key in configuration examples, screenshots, Issues or ZIPs. Credentials for other model services must be configured separately.

## 5. Clone an authorized voice

1. Select the intended console project, enable the relevant voice-cloning service and obtain your own voice slot/ID.
2. Supply your own voice or authorized recordings, following the console/API sample and transcript requirements.
3. Train and audition using the [official voice registration API](https://docs.volcengine.com/docs/doubao-voice/tone-training-http?lang=zh). The current endpoint is `POST /api/v3/tts/voice_clone`; the new console uses `X-Api-Key`. Follow the official documentation for current fields and input limits.
4. Keep your own Speaker ID. An author-specific ID in a historical example does not grant your account access to that voice.
5. Test the voice with the intended API before connecting the robot. Successful TTS cloning does not establish compatibility with every realtime-conversation interface.

Cloning, synthesis and realtime dialogue can use different resources and billing. For the voice-cloning 2.0 TTS interface, the resource identifier is `seed-icl-2.0`; follow the [official TTS documentation](https://docs.volcengine.com/docs/doubao-voice/unidirectional-streaming-text-to-speech-http?lang=zh), rather than inserting that identifier into unrelated dialogue requests.

## 6. Configure the voice and persona

The repository's `seeduplex.py` reads the voice from the environment:

```powershell
$env:TARS_TTS_SPEAKER = 'YOUR_SPEAKER_ID'
```

Copy `tars-seeduplex.example.json` to `tars-seeduplex.json` and edit `instructions` for persona and response preferences. Do not put private conversation history into a public example.

After pairing, model installation and certificate provisioning, the existing continuous-voice entry point can be configured in chat-only mode:

```powershell
.\Start-Tars.ps1 -Setup -ChatOnly
```

Verify conversation and playback before enabling motion through that version's confirmation flow. Its remaining-free-token prompt is a local accounting policy, not a promise of free account credit. The final standalone daily-voice startup procedure awaits the author's new package. The filming director is not part of the new public installation.

## 7. Validation and troubleshooting

Check USB debugging first, then LAN pairing/device ID/certificates, then cloud authentication and voice access, followed by microphone/playback and finally voice-triggered motion. Keep the PC service running.

| Failure | First checks |
| --- | --- |
| Authentication | Correct API project and enabled service |
| Voice access | Speaker ID, account project and API compatibility |
| TLS or missing voice connection | PC address, matching certificate/key and firmware trust |
| USB port busy | Other calibration, gait or voice processes using the device |
| No audio | Required models, audio wiring and redacted error codes |

## Privacy and license

Keep Wi-Fi profiles, pairing tokens, API keys, private keys, credential XML, conversation databases, recordings and runtime logs private. Share redacted error codes and version numbers for support.

Original project materials use [CC BY-NC-SA 4.0](../LICENSE.txt); commercial use requires separate permission. Cloud services, voices, models and third-party software retain their respective terms and licenses.
