from __future__ import annotations

import argparse
import asyncio
import importlib.util
import os
import socket
import sys
import urllib.error
from dataclasses import dataclass

from .llm import DeepSeekProvider
from .speech import COSYVOICE_MODEL, QWEN_VC_MODEL, QWEN_VOICE_PREFIX, workspace_id_is_valid


@dataclass(frozen=True)
class Check:
    name: str
    ok: bool
    message: str


def _probe_deepseek() -> Check:
    try:
        asyncio.run(DeepSeekProvider().complete([{"role": "user", "content": "Reply with OK."}]))
    except urllib.error.HTTPError as exc:
        return Check("deepseek_live", False, f"http_{exc.code}")
    except (urllib.error.URLError, ConnectionError):
        return Check("deepseek_live", False, "network_error")
    except (TimeoutError, socket.timeout):
        return Check("deepseek_live", False, "timeout")
    except (KeyError, IndexError, TypeError):
        return Check("deepseek_live", False, "response_invalid")
    except Exception:
        return Check("deepseek_live", False, "provider_error")
    return Check("deepseek_live", True, "request succeeded")


def run_checks(*, audio: bool = False, deepseek: bool = False,
               probe_deepseek: bool = False) -> list[Check]:
    api_key_present = bool(os.environ.get("DASHSCOPE_API_KEY"))
    voice_present = bool(os.environ.get("DASHSCOPE_VOICE_ID"))
    checks = [
        Check("python", sys.version_info >= (3, 11), f"{sys.version_info.major}.{sys.version_info.minor}"),
        Check("dashscope", importlib.util.find_spec("dashscope") is not None, "optional voice SDK"),
        Check("sounddevice", importlib.util.find_spec("sounddevice") is not None, "optional audio backend"),
        Check("DASHSCOPE_API_KEY", api_key_present, "configured" if api_key_present else "not configured"),
        Check("DASHSCOPE_VOICE_ID", voice_present, "configured" if voice_present else "not configured"),
        Check("DASHSCOPE_WORKSPACE_ID", workspace_id_is_valid(os.environ.get("DASHSCOPE_WORKSPACE_ID")),
              "configured" if workspace_id_is_valid(os.environ.get("DASHSCOPE_WORKSPACE_ID")) else
              "invalid; copy the ws-* prefix from API Host"),
    ]
    if voice_present:
        configured_model = os.environ.get("DASHSCOPE_TTS_MODEL")
        qwen_voice = os.environ["DASHSCOPE_VOICE_ID"].startswith(QWEN_VOICE_PREFIX)
        inferred_model = QWEN_VC_MODEL if qwen_voice else COSYVOICE_MODEL
        compatible = not configured_model or configured_model == inferred_model
        checks.append(Check("TTS_PROFILE", compatible,
                            (f"qwen-vc/{QWEN_VC_MODEL}" if qwen_voice else
                             f"cosyvoice/{COSYVOICE_MODEL}") if compatible else
                            "voice and DASHSCOPE_TTS_MODEL do not match"))
    if deepseek:
        present = bool(os.environ.get("DEEPSEEK_API_KEY"))
        checks.append(Check("DEEPSEEK_API_KEY", present, "configured" if present else "not configured"))
        model = os.environ.get("DEEPSEEK_MODEL", "deepseek-v4-flash")
        supported = model in {"deepseek-v4-flash", "deepseek-v4-pro"}
        checks.append(Check("DEEPSEEK_MODEL", supported, model if supported else
                            "invalid; use deepseek-v4-flash or deepseek-v4-pro"))
        if probe_deepseek:
            checks.append(_probe_deepseek() if present and supported else
                          Check("deepseek_live", False, "configuration_invalid"))
    if audio and checks[2].ok:
        try:
            import sounddevice as sd
            count = len(sd.query_devices())
            checks.append(Check("audio_devices", count > 0, f"{count} device(s)"))
        except Exception:
            checks.append(Check("audio_devices", False, "query failed"))
    return checks


def main() -> None:
    parser = argparse.ArgumentParser(description="Offline TARS voice configuration check")
    parser.add_argument("--audio", action="store_true", help="also query local audio devices")
    parser.add_argument("--deepseek", action="store_true", help="also require DEEPSEEK_API_KEY")
    parser.add_argument("--probe-deepseek", action="store_true",
                        help="make one minimal DeepSeek request without printing content or credentials")
    args = parser.parse_args()
    checks = run_checks(audio=args.audio, deepseek=args.deepseek,
                        probe_deepseek=args.probe_deepseek)
    for check in checks:
        print(f"{'OK' if check.ok else 'MISSING'}  {check.name}: {check.message}")
    raise SystemExit(0 if all(check.ok for check in checks) else 2)


if __name__ == "__main__":
    main()
