import argparse
import base64
import hashlib
import json
import re
import subprocess
from datetime import date
from pathlib import Path
from tempfile import NamedTemporaryFile, TemporaryDirectory
from time import time
from urllib.parse import urlsplit

ASSET = re.compile(
    r"MinecraftTools-update-(\d{4}-\d{2}-\d{2})-([1-9]\d*)-(windows|darwin|linux)-(amd64|arm64)\.tar\.gz"
)


def encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def release(path: Path, channel: str, base: str) -> dict:
    match = ASSET.fullmatch(path.name)
    if match is None:
        raise ValueError(f"Invalid update package name: {path.name}")
    day, sequence, os_name, arch = match.groups()
    date.fromisoformat(day)
    if int(sequence) > (1 << 32) - 1:
        raise ValueError(f"Version sequence is too large: {path.name}")
    size = path.stat().st_size
    if not 0 < size <= 1024**3:
        raise ValueError(f"Invalid update package size: {path.name}")
    with path.open("rb") as file:
        digest = hashlib.file_digest(file, "sha256").hexdigest()
    return {
        "channel": channel,
        "version": f"{day}-{sequence}",
        "os": os_name,
        "arch": arch,
        "url": f"{base}/{path.name}",
        "size": size,
        "sha256": digest,
    }


def write_manifest(options) -> None:
    base = options.base_url.rstrip("/")
    parsed = urlsplit(base)
    loopback = parsed.hostname in ("127.0.0.1", "::1", "localhost")
    if (
        not parsed.hostname
        or parsed.username is not None
        or parsed.query
        or parsed.fragment
        or not (parsed.scheme == "https" or (loopback and parsed.scheme == "http"))
    ):
        raise ValueError("The artifact base URL must use HTTPS or loopback HTTP")
    if options.hours <= 0 or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", options.key_id):
        raise ValueError("Invalid manifest lifetime or key identifier")
    releases = [
        release(path, options.channel, base) for path in sorted(options.artifacts)
    ]
    keys = {(item["version"], item["os"], item["arch"]) for item in releases}
    if len(keys) != len(releases):
        raise ValueError("Duplicate update package for the same version and platform")
    if options.matrix_version and keys != {
        (options.matrix_version, system, arch)
        for system in ("windows", "darwin", "linux")
        for arch in ("amd64", "arm64")
    }:
        raise ValueError("The release must contain all six packages at the same version")
    now = int(time())
    data = json.dumps(
        {
            "channel": options.channel,
            "issued_at": now,
            "expires_at": now + options.hours * 3600,
            "releases": releases,
        },
        separators=(",", ":"),
    ).encode("utf-8")
    with TemporaryDirectory() as directory:
        payload = Path(directory) / "payload.json"
        payload.write_bytes(data)
        signature = subprocess.run(
            [
                options.openssl,
                "pkeyutl",
                "-sign",
                "-rawin",
                "-inkey",
                str(options.private_key),
                "-in",
                str(payload),
            ],
            check=True,
            stdout=subprocess.PIPE,
        ).stdout
    if len(signature) != 64:
        raise ValueError("Signing key must be Ed25519")
    envelope = (
        json.dumps(
            {
                "schema": 1,
                "key_id": options.key_id,
                "payload": encode(data),
                "signature": encode(signature),
            },
            separators=(",", ":"),
        )
        + "\n"
    )
    if len(envelope) > 512 * 1024:
        raise ValueError("Signed manifest exceeds its size limit")
    options.output.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile(
        dir=options.output, delete=False, mode="w", encoding="utf-8"
    ) as file:
        temporary = Path(file.name)
        try:
            file.write(envelope)
            file.close()
            temporary.chmod(0o644)
            temporary.replace(options.output / f"{options.channel}.json")
        finally:
            temporary.unlink(missing_ok=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Sign a MinecraftTools release manifest with Ed25519"
    )
    parser.add_argument("--channel", choices=["stable", "dev"], required=True)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--key-id", required=True)
    parser.add_argument("--private-key", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--hours", type=int, default=24 * 30)
    parser.add_argument("--matrix-version")
    parser.add_argument("--openssl", default="openssl")
    parser.add_argument("artifacts", type=Path, nargs="*")
    write_manifest(parser.parse_args())
