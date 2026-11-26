import argparse
import os
import platform
import re
import shutil
import subprocess
import sys
import tarfile
from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory

from minecrafttools.config import Node, TunnelClaim, write_frpc_config

CLIENT = Path(__file__).resolve().parents[1]
NAME = "MinecraftTools"


def output_dirs() -> tuple[Path, Path]:
    """产物和工作目录默认在 client/ 下；设置环境变量后写到项目目录以外。"""
    dist = Path(os.environ.get("MINECRAFTTOOLS_DIST_DIR", CLIENT / "dist"))
    work = Path(os.environ.get("MINECRAFTTOOLS_WORK_DIR", CLIENT / "build" / "pyinstaller"))
    return dist, work


def main():
    parser = argparse.ArgumentParser(description="Build and validate a native MinecraftTools archive.")
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
    dist, work = output_dirs()
    work.mkdir(parents=True, exist_ok=True)
    source = CLIENT / "src" / "minecrafttools"
    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm", "--clean", "--onedir", "--name", NAME,
        "--distpath", str(dist), "--workpath", str(work), "--specpath", str(work),
        "--add-binary", f"{frpc}:minecrafttools/bin",
        "--add-data", f"{frpc.with_name('frp-LICENSE')}:minecrafttools",
        "--add-data", f"{source / 'logo.ico'}:minecrafttools",
        "--add-data", f"{source / 'MinecraftToolsStartupLogo.png'}:minecrafttools",
    ]
    if system in {"windows", "darwin"}:
        cmd.append("--windowed")
    if system == "windows":
        cmd.extend(["--icon", str(source / "logo.ico")])
    if system == "darwin":
        cmd.extend(["--osx-bundle-identifier", "com.mitahill.minecrafttools"])
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
        claim_id = "0" * 32
        claim = TunnelClaim(claim_id, f"minecraft-{claim_id}", "A" * 43)
        write_frpc_config(config, Node("127.0.0.1", 7000), 25565, 25566, claim)
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
    update_name = archives / f"{NAME}-update-{args.version}-{system}-{arch}.tar.gz"
    with tarfile.open(update_name, "w:gz", format=tarfile.GNU_FORMAT, compresslevel=4) as archive:
        archive.add(bundle, arcname=bundle.name)


if __name__ == "__main__":
    main()
