import argparse
import hashlib
import json
import plistlib
import re
import shutil
import subprocess
from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory

NATIVE = Path(__file__).with_name("native")
ASSET = re.compile(r"MinecraftTools-update-([0-9]{4}-[0-9]{2}-[0-9]{2}-[1-9][0-9]*)-(darwin|linux|windows)-(amd64|arm64)\.tar\.gz")


def dated_version(version: str) -> date:
    return date.fromisoformat(version.rsplit("-", 1)[0])


def msi_version(version: str) -> str:
    day = dated_version(version)
    release = int(version.rsplit("-", 1)[1])
    if not 2000 <= day.year <= 2255 or not 1 <= release <= 999:
        raise ValueError("MSI versions require a year from 2000 to 2255 and a sequence from 1 to 999")
    return f"{day.year - 2000}.{day.month}.{day.day * 1000 + release}"


def bootstrap(archive: Path, resources: Path, version: str, channel: str) -> None:
    resources.mkdir(parents=True, exist_ok=True)
    with archive.open("rb") as source:
        digest = hashlib.file_digest(source, "sha256").hexdigest()
    shutil.copyfile(archive, resources / "bootstrap.tar.gz")
    (resources / "bootstrap.json").write_text(json.dumps({
        "version": version, "channel": channel, "size": archive.stat().st_size, "sha256": digest,
    }) + "\n", encoding="utf-8")


def macos(stage: Path, output: Path, options, version: str) -> None:
    app = stage / "MinecraftTools.app"
    contents = app / "Contents"
    binaries = contents / "MacOS"
    binaries.mkdir(parents=True)
    launcher = binaries / "MinecraftToolsLauncher"
    shutil.copyfile(options.launcher, launcher)
    launcher.chmod(0o755)
    bootstrap(options.update, contents / "Resources", version, options.channel)
    with (contents / "Info.plist").open("wb") as info:
        plistlib.dump({
            "CFBundleIdentifier": "com.mitahill.minecrafttools.launcher",
            "CFBundleName": "MinecraftTools",
            "CFBundleDisplayName": "MinecraftTools",
            "CFBundlePackageType": "APPL",
            "CFBundleExecutable": "MinecraftToolsLauncher",
            "CFBundleVersion": msi_version(version),
            "CFBundleShortVersionString": msi_version(version),
            "NSHighResolutionCapable": True,
        }, info)
    flags = [] if options.mac_identity == "-" else ["--options", "runtime", "--timestamp"]
    subprocess.run(["codesign", "--force", "--sign", options.mac_identity, *flags, app], check=True)
    subprocess.run(["codesign", "--verify", "--strict", app], check=True)
    (stage / "Applications").symlink_to("/Applications", target_is_directory=True)
    subprocess.run(["hdiutil", "create", "-volname", "MinecraftTools", "-format", "UDZO", "-srcfolder", stage, output], check=True)
    if options.mac_notary_profile:
        subprocess.run(["codesign", "--sign", options.mac_identity, "--timestamp", output], check=True)
        subprocess.run(["xcrun", "notarytool", "submit", output, "--keychain-profile", options.mac_notary_profile, "--wait"], check=True)
        subprocess.run(["xcrun", "stapler", "staple", output], check=True)
        subprocess.run(["xcrun", "stapler", "validate", output], check=True)


def windows(stage: Path, output: Path, options, version: str, arch: str) -> None:
    shutil.copyfile(options.launcher, stage / "MinecraftToolsLauncher.exe")
    bootstrap(options.update, stage, version, options.channel)
    subprocess.run([
        options.wix, "build", str(NATIVE / "MinecraftTools.wxs"), "-arch", "x64" if arch == "amd64" else "arm64",
        "-d", f"SourceDir={stage}", "-d", f"Version={msi_version(version)}", "-o", str(output),
    ], check=True)


def linux(stage: Path, output: Path, options, version: str) -> None:
    shutil.copyfile(options.launcher, stage / "MinecraftToolsLauncher")
    bootstrap(options.update, stage, version, options.channel)
    for name in ("MinecraftToolsLauncher", "install.sh", "uninstall.sh"):
        executable = stage / name
        if name.endswith(".sh"):
            shutil.copyfile(NATIVE / name, executable)
        executable.chmod(0o755)
    subprocess.run([options.makeself, "--nox11", "--nochown", "--sha256", "--nocomp", stage, output,
                    "MinecraftTools", "./install.sh"], check=True)


def main() -> None:
    parser = argparse.ArgumentParser(description="Package the native Launcher and desktop seed bundle")
    parser.add_argument("--launcher", type=Path, required=True)
    parser.add_argument("--update", type=Path, required=True)
    parser.add_argument("--channel", choices=("stable", "dev"), required=True)
    parser.add_argument("--output-directory", type=Path, required=True)
    parser.add_argument("--mac-identity", default="-")
    parser.add_argument("--mac-notary-profile")
    parser.add_argument("--wix", default="wix")
    parser.add_argument("--makeself", default="makeself")
    options = parser.parse_args()
    match = ASSET.fullmatch(options.update.name)
    if match is None:
        parser.error("expected a date-versioned desktop update package")
    version, system, arch = match.groups()
    dated_version(version)
    if options.mac_notary_profile and options.mac_identity == "-":
        parser.error("notarization requires a Developer ID signing identity")
    extension = {"darwin": "dmg", "windows": "msi", "linux": "run"}[system]
    options.output_directory.mkdir(parents=True, exist_ok=True)
    destination = options.output_directory.resolve() / f"MinecraftTools-{version}-{system}-{arch}.{extension}"
    with TemporaryDirectory(prefix="minecrafttools-native-", dir=destination.parent) as work:
        stage = Path(work) / "content"
        stage.mkdir()
        output = Path(work) / destination.name
        if system == "darwin":
            macos(stage, output, options, version)
        elif system == "windows":
            windows(stage, output, options, version, arch)
        else:
            linux(stage, output, options, version)
        output.replace(destination)
    print(f"{destination.name} {destination.stat().st_size} bytes")


if __name__ == "__main__":
    main()
