import argparse
import os
import platform
import re
import shutil
import subprocess
import sys
from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory

from minecraftfrp.config import load_nodes, write_frpc_config


CLIENT = Path(__file__).resolve().parents[1]
NAME = "MinecraftFRP"


def main():
    parser = argparse.ArgumentParser(description="Build and validate a native MinecraftFRP archive.")
    parser.add_argument("--frpc", type=Path, required=True)
    parser.add_argument("--version", required=True)
    args = parser.parse_args()
    release_version = re.fullmatch(r"([0-9]{4}-[0-9]{2}-[0-9]{2})-[1-9][0-9]*", args.version)
    if release_version:
        try:
            date.fromisoformat(release_version[1])
        except ValueError:
            parser.error("release version must contain a valid calendar date")
    elif not re.fullmatch(r"ci-[0-9a-f]+|local", args.version):
        parser.error("version must be yyyy-mm-dd-n (n starts at 1), ci-<sha> or local")
    system = platform.system().lower()
    arch = {"x86_64": "amd64", "amd64": "amd64", "aarch64": "arm64", "arm64": "arm64"}[platform.machine().lower()]
    if system not in {"windows", "darwin", "linux"}:
        parser.error(f"system is unsupported: {system}")
    frpc = args.frpc.resolve()
    dist = CLIENT / "dist"
    work = CLIENT / "build" / "pyinstaller"
    source = CLIENT / "src" / "minecraftfrp"
    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm", "--clean", "--onedir", "--name", NAME,
        "--distpath", str(dist), "--workpath", str(work), "--specpath", str(work),
        "--add-binary", f"{frpc}:minecraftfrp/bin",
        "--add-data", f"{frpc.with_name('frp-LICENSE')}:minecraftfrp",
        "--add-data", f"{source / 'logo.ico'}:minecraftfrp",
        "--add-data", f"{source / 'MinecraftToolsStartupLogo.png'}:minecraftfrp",
    ]
    if system in {"windows", "darwin"}:
        cmd.append("--windowed")
    if system == "windows":
        cmd.extend(["--icon", str(source / "logo.ico")])
    if system == "darwin":
        cmd.extend(["--osx-bundle-identifier", "com.mitahill.minecraftfrp"])
    cmd.append(str(source / "__main__.py"))
    subprocess.run(cmd, check=True, cwd=CLIENT)

    bundle = dist / NAME
    executable = bundle / (NAME + ".exe" if system == "windows" else NAME)
    if system == "darwin":
        bundle = dist / f"{NAME}.app"
        executable = bundle / "Contents" / "MacOS" / NAME
    env = {**os.environ, "QT_QPA_PLATFORM": "offscreen"}
    with TemporaryDirectory() as directory:
        config = Path(directory) / "frpc.toml"
        node = load_nodes(CLIENT / "nodes.example.toml")[0]
        write_frpc_config(config, node, 25565, 25566)
        subprocess.run([str(frpc), "verify", "-c", str(config)], check=True, timeout=15)
        subprocess.run([str(executable), "--smoke-test"], env=env, check=True, timeout=30)

    archives = dist / "artifacts"
    archives.mkdir(parents=True, exist_ok=True)
    base = archives / f"{NAME}-{args.version}-{system}-{arch}"
    if system == "darwin":
        subprocess.run(
            ["ditto", "-c", "-k", "--sequesterRsrc", "--keepParent", str(bundle), f"{base}.zip"],
            check=True,
        )
    else:
        shutil.make_archive(str(base), "zip" if system == "windows" else "gztar", root_dir=dist, base_dir=bundle.name)


if __name__ == "__main__":
    main()
