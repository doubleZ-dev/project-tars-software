from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any


FLASH_CONFIRMATION = "SERVOS_5V_OFF_ROBOT_LIFTED"


class ManifestError(ValueError):
    pass


@dataclass(frozen=True)
class FlashArtifact:
    name: str
    offset: int
    path: Path
    size: int
    sha256: str


@dataclass(frozen=True)
class ReleaseManifest:
    path: Path
    release_id: str
    firmware_version: str
    artifacts: tuple[FlashArtifact, ...]


def _require(condition: bool, code: str) -> None:
    if not condition:
        raise ManifestError(code)


def load_manifest(path: str | Path) -> ReleaseManifest:
    manifest_path = Path(path).resolve()
    try:
        data: dict[str, Any] = json.loads(manifest_path.read_text(encoding="utf-8"))
    except Exception:
        raise ManifestError("manifest_unreadable") from None
    _require(data.get("schema_version") == 1, "schema_mismatch")
    _require(data.get("target_chip") == "esp32s3", "target_mismatch")
    _require(data.get("board") == "esp32-s3-n16r8", "board_mismatch")
    _require(data.get("flash_size_bytes") == 16 * 1024 * 1024, "flash_size_mismatch")
    _require(data.get("protocol_version") == 2, "protocol_mismatch")
    _require(data.get("flash_mode") == "dio" and data.get("flash_freq") == "80m", "flash_settings_invalid")
    safe = data.get("safe_start")
    _require(isinstance(safe, dict) and safe.get("default_motion") is False and
             safe.get("boot_disarmed") is True, "unsafe_start")
    release_id = data.get("release_id")
    firmware_version = data.get("firmware_version")
    _require(release_id == "tars-phase10" and firmware_version == "0.10.2", "release_mismatch")
    raw_artifacts = data.get("artifacts")
    _require(isinstance(raw_artifacts, list) and len(raw_artifacts) == 3, "artifact_set_invalid")
    root = manifest_path.parent
    artifacts: list[FlashArtifact] = []
    expected = {"bootloader": 0x0, "partition_table": 0x8000, "app": 0x10000}
    for item in raw_artifacts:
        _require(isinstance(item, dict) and item.get("name") in expected, "artifact_invalid")
        relative = Path(str(item.get("path", "")))
        _require(not relative.is_absolute() and ".." not in relative.parts, "artifact_path_invalid")
        resolved = (root / relative).resolve()
        _require(resolved.is_relative_to(root), "artifact_path_invalid")
        try:
            offset = int(str(item.get("offset")), 0)
            size = int(item.get("size"))
        except (TypeError, ValueError):
            raise ManifestError("artifact_metadata_invalid") from None
        digest = str(item.get("sha256", ""))
        _require(offset == expected[item["name"]] and size > 0 and len(digest) == 64,
                 "artifact_metadata_invalid")
        _require(resolved.is_file() and resolved.stat().st_size == size, "artifact_size_mismatch")
        actual = hashlib.sha256(resolved.read_bytes()).hexdigest()
        _require(actual == digest and digest == digest.lower(), "artifact_hash_mismatch")
        artifacts.append(FlashArtifact(item["name"], offset, resolved, size, digest))
    artifacts.sort(key=lambda artifact: artifact.offset)
    _require(len({a.offset for a in artifacts}) == 3, "artifact_offset_duplicate")
    for current, following in zip(artifacts, artifacts[1:]):
        _require(current.offset + current.size <= following.offset, "artifact_overlap")
    _require(artifacts[-1].offset + artifacts[-1].size <= 0x110000, "app_partition_overflow")
    return ReleaseManifest(manifest_path, release_id, firmware_version, tuple(artifacts))


def build_flash_argv(manifest: ReleaseManifest, port: str, *, python: str = sys.executable,
                     baud: int = 460800) -> list[str]:
    if not port or any(char in port for char in "\r\n\0"):
        raise ValueError("invalid_port")
    argv = [python, "-m", "esptool", "--chip", "esp32s3", "--port", port,
            "--baud", str(baud), "--before", "default_reset", "--after", "hard_reset",
            "write_flash", "--flash_mode", "dio", "--flash_freq", "80m",
            "--flash_size", "16MB"]
    for artifact in manifest.artifacts:
        argv.extend([hex(artifact.offset), str(artifact.path)])
    return argv


def main() -> None:
    parser = argparse.ArgumentParser(description="Validate or flash the TARS phase10 release")
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--port", required=True,
                        help="board CH340 USB-to-UART flash/control port, for example COM7")
    parser.add_argument("--baud", type=int, default=460800)
    parser.add_argument("--execute", action="store_true", help="actually invoke esptool; default is plan only")
    parser.add_argument("--confirm-servos-off")
    args = parser.parse_args()
    manifest = load_manifest(args.manifest)
    argv = build_flash_argv(manifest, args.port, baud=args.baud)
    print(json.dumps({"release_id": manifest.release_id, "validated": True,
                      "will_execute": args.execute, "argv": argv}, ensure_ascii=False, indent=2))
    if not args.execute:
        return
    if args.confirm_servos_off != FLASH_CONFIRMATION:
        raise SystemExit(f"flashing requires --confirm-servos-off {FLASH_CONFIRMATION}")
    subprocess.run(argv, check=True)


if __name__ == "__main__":
    main()
