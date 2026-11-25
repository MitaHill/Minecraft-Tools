import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import package_desktop  # noqa: E402


class VersionTests(unittest.TestCase):
    def test_msi_version_maps_date_and_sequence(self):
        self.assertEqual(package_desktop.msi_version("2026-11-25-1"), "26.11.25001")
        self.assertEqual(package_desktop.msi_version("2000-01-01-999"), "0.1.1999")
        self.assertEqual(package_desktop.msi_version("2255-12-31-1"), "255.12.31001")

    def test_msi_version_rejects_out_of_range_values(self):
        for version in ("1999-12-31-1", "2026-11-25-1000", "2026-02-30-1"):
            with self.subTest(version=version), self.assertRaises(ValueError):
                package_desktop.msi_version(version)


class AssetNameTests(unittest.TestCase):
    def test_accepts_update_package_names(self):
        for name in (
            "MinecraftTools-update-2026-11-25-1-darwin-arm64.tar.gz",
            "MinecraftTools-update-2026-11-25-12-windows-amd64.tar.gz",
            "MinecraftTools-update-2026-11-25-1-linux-arm64.tar.gz",
        ):
            with self.subTest(name=name):
                self.assertIsNotNone(package_desktop.ASSET.fullmatch(name))

    def test_rejects_other_names(self):
        for name in (
            "MinecraftTools-update-ci-abc123-darwin-arm64.tar.gz",
            "MinecraftTools-update-2026-11-25-1-darwin-x86_64.tar.gz",
            "MinecraftTools-update-2026-11-25-0-linux-amd64.tar.gz",
            "MinecraftTools-2026-11-25-1-darwin-arm64.tar.gz",
        ):
            with self.subTest(name=name):
                self.assertIsNone(package_desktop.ASSET.fullmatch(name))


class BootstrapTests(unittest.TestCase):
    def test_writes_metadata_and_copies_archive(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive = root / "MinecraftTools-update-2026-11-25-1-linux-amd64.tar.gz"
            archive.write_bytes(b"payload")
            resources = root / "Resources"
            package_desktop.bootstrap(archive, resources, "2026-11-25-1", "dev")
            metadata = json.loads((resources / "bootstrap.json").read_text(encoding="utf-8"))
            self.assertEqual(metadata["version"], "2026-11-25-1")
            self.assertEqual(metadata["channel"], "dev")
            self.assertEqual(metadata["size"], len(b"payload"))
            self.assertEqual(metadata["sha256"], hashlib.sha256(b"payload").hexdigest())
            self.assertEqual((resources / "bootstrap.tar.gz").read_bytes(), b"payload")


if __name__ == "__main__":
    unittest.main()
