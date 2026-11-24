import argparse
import hashlib
import json
import os
import re
import shutil
import tarfile
import zipfile
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.request import Request, urlopen


RELEASES = "https://api.github.com/repos/fatedier/frp/releases"
TAG_PATTERN = re.compile(r"v\d+\.\d+\.\d+")


def release_info(tag: str | None = None) -> dict:
    if tag is not None and not TAG_PATTERN.fullmatch(tag):
        raise ValueError(f"Invalid FRP tag: {tag}")
    endpoint = f"tags/{tag}" if tag else "latest"
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "MinecraftFRP-build"}
    if token := os.getenv("GH_TOKEN"):
        headers["Authorization"] = f"Bearer {token}"
    with urlopen(Request(f"{RELEASES}/{endpoint}", headers=headers), timeout=30) as response:
        release = json.load(response)
    if not TAG_PATTERN.fullmatch(release["tag_name"]) or release["prerelease"] or release["draft"]:
        raise ValueError("Expected a stable FRP release")
    return release


def download_frpc(release: dict, system: str, arch: str, output: Path) -> Path:
    version = release["tag_name"][1:]
    stem = f"frp_{version}_{system}_{arch}"
    windows = system == "windows"
    asset_name = stem + (".zip" if windows else ".tar.gz")
    asset = next((item for item in release["assets"] if item["name"] == asset_name), None)
    if asset is None:
        raise ValueError(f"Release does not contain {asset_name}")
    digest = asset.get("digest") or ""
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
        raise ValueError(f"Release does not provide a SHA-256 digest for {asset_name}")

    binary = "frpc.exe" if windows else "frpc"
    output.mkdir(parents=True, exist_ok=True)
    destination = output / binary
    with TemporaryDirectory(dir=output) as tmp:
        archive = Path(tmp) / asset_name
        with urlopen(asset["browser_download_url"], timeout=90) as response, archive.open("wb") as file:
            shutil.copyfileobj(response, file)
        with archive.open("rb") as file:
            actual = hashlib.file_digest(file, "sha256").hexdigest()
        if digest != f"sha256:{actual}":
            raise ValueError(f"SHA-256 mismatch for {asset_name}")

        staging = Path(tmp) / binary
        license_name = "frp-LICENSE"
        if windows:
            with zipfile.ZipFile(archive) as package:
                with package.open(f"{stem}/{binary}") as source, staging.open("wb") as file:
                    shutil.copyfileobj(source, file)
                license_bytes = package.read(f"{stem}/LICENSE")
        else:
            with tarfile.open(archive, "r:gz") as package:
                info = package.getmember(f"{stem}/{binary}")
                if not info.isfile():
                    raise ValueError(f"{info.name} is not a regular file")
                with package.extractfile(info) as source, staging.open("wb") as file:
                    shutil.copyfileobj(source, file)
                with package.extractfile(f"{stem}/LICENSE") as license_file:
                    license_bytes = license_file.read()
        staging.chmod(0o755)
        staging.replace(destination)
        (Path(tmp) / license_name).write_bytes(license_bytes)
        (Path(tmp) / license_name).replace(output / license_name)

    print(f"{asset_name} sha256:{actual}")
    return destination


def main():
    parser = argparse.ArgumentParser(description="Fetch the official frpc release asset.")
    parser.add_argument("--latest-tag", action="store_true")
    parser.add_argument("--tag")
    parser.add_argument("--os", choices=("windows", "darwin", "linux"))
    parser.add_argument("--arch", choices=("amd64", "arm64"))
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.latest_tag:
        print(release_info()["tag_name"])
        return
    if not args.os or not args.arch or not args.output:
        parser.error("--os, --arch and --output are required when downloading")
    download_frpc(release_info(args.tag), args.os, args.arch, args.output)


if __name__ == "__main__":
    main()
