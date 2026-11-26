import hashlib
import json
import os
import plistlib
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

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

class DesktopIconTests(unittest.TestCase):
    def test_macos_bundle_contains_declared_icon(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            stage = root / "stage"
            stage.mkdir()
            launcher = root / "launcher"
            launcher.write_bytes(b"launcher")
            update = root / "update.tar.gz"
            update.write_bytes(b"update")
            options = SimpleNamespace(launcher=launcher, update=update, channel="dev",
                                      mac_identity="-", mac_notary_profile=None)
            with patch.object(package_desktop.subprocess, "run"):
                package_desktop.macos(stage, root / "installer.dmg", options, "2026-11-25-1")
            contents = stage / "MinecraftTools.app" / "Contents"
            with (contents / "Info.plist").open("rb") as info:
                icon = plistlib.load(info)["CFBundleIconFile"]
            self.assertEqual((contents / "Resources" / icon).read_bytes(),
                             (package_desktop.ASSETS / "logo-macos.icns").read_bytes())

    def test_linux_installer_registers_icon(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            stage = root / "stage"
            stage.mkdir()
            for name in ("MinecraftToolsLauncher", "bootstrap.json", "bootstrap.tar.gz"):
                (stage / name).write_bytes(b"fixture")
            shutil.copyfile(package_desktop.ASSETS / "logo-macos.png", stage / "logo.png")
            shutil.copyfile(package_desktop.NATIVE / "install.sh", stage / "install.sh")
            shutil.copyfile(package_desktop.NATIVE / "uninstall.sh", stage / "uninstall.sh")
            env = {**os.environ, "XDG_DATA_HOME": str(root / "data"), "XDG_BIN_HOME": str(root / "bin")}
            subprocess.run(["sh", str(stage / "install.sh")], env=env, check=True, capture_output=True)
            desktop = (root / "data/applications/minecrafttools.desktop").read_text()
            icon = root / "data/minecrafttools/logo.png"
            self.assertIn(f"Icon={icon}", desktop)
            self.assertEqual(icon.read_bytes(), (package_desktop.ASSETS / "logo-macos.png").read_bytes())


if __name__ == "__main__":
    unittest.main()
