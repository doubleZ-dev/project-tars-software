import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tars_companion.deployment import (ManifestError, build_flash_argv, load_manifest,
                                       main)


BASE = Path(__file__).resolve().parents[2]
RELEASE = BASE / "tars_release_phase10" if (BASE / "tars_release_phase10").exists() else BASE
MANIFEST = RELEASE / "manifest.json"


class DeploymentTests(unittest.TestCase):
    def test_release_manifest_and_hashes_validate(self):
        manifest = load_manifest(MANIFEST)
        self.assertEqual(manifest.release_id, "tars-phase10")
        self.assertEqual([a.offset for a in manifest.artifacts], [0, 0x8000, 0x10000])

    def test_flash_argv_is_fixed_and_paths_remain_single_arguments(self):
        manifest = load_manifest(MANIFEST)
        argv = build_flash_argv(manifest, "COM_TEST", python="python test.exe")
        self.assertEqual(argv[:7], ["python test.exe", "-m", "esptool", "--chip", "esp32s3", "--port", "COM_TEST"])
        self.assertIn("16MB", argv)
        self.assertNotIn("erase_flash", argv)
        self.assertEqual([argv[argv.index(hex(a.offset)) + 1] for a in manifest.artifacts],
                         [str(a.path) for a in manifest.artifacts])

    def test_tampered_firmware_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            copy = Path(directory) / "release"
            shutil.copytree(RELEASE, copy)
            app = copy / "firmware" / "tars_firmware.bin"
            data = bytearray(app.read_bytes()); data[-1] ^= 1; app.write_bytes(data)
            with self.assertRaisesRegex(ManifestError, "hash"):
                load_manifest(copy / "manifest.json")

    def test_2mb_or_unsafe_manifest_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            copy = Path(directory) / "release"
            shutil.copytree(RELEASE, copy)
            path = copy / "manifest.json"
            data = json.loads(path.read_text(encoding="utf-8"))
            data["flash_size_bytes"] = 2 * 1024 * 1024
            data["safe_start"]["default_motion"] = True
            path.write_text(json.dumps(data), encoding="utf-8")
            with self.assertRaises(ManifestError):
                load_manifest(path)

    def test_plan_mode_never_invokes_esptool(self):
        argv = ["tars-deploy", "--manifest", str(MANIFEST), "--port", "COM_TEST"]
        with patch("sys.argv", argv), patch("subprocess.run") as runner:
            main()
        runner.assert_not_called()


if __name__ == "__main__":
    unittest.main()
