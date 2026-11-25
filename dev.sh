#!/bin/sh
# 项目树里不留构建产物：target/、.venv/、build/、dist/、__pycache__ 全部写到项目目录以外。
# 用法：./dev.sh uv sync --project client --locked --extra build
#       ./dev.sh cargo test --manifest-path launcher/Cargo.toml
# 目录默认 /tmp/minecrafttools-build，可用 MINECRAFTTOOLS_BUILD_ROOT 覆盖。
# 想复用全局缓存就自己设 UV_CACHE_DIR（例如 ~/.cache/uv），默认也放在构建根目录里。
# 临时目录被系统清理后，重新运行 uv sync 即可，依赖本来就只该是缓存。
set -eu

root=${MINECRAFTTOOLS_BUILD_ROOT:-/tmp/minecrafttools-build}
mkdir -p "$root/cargo-target" "$root/pycache" "$root/venv"

# client 要 Python 3.15，control 要 3.12，共用一个环境时每次 sync 都会卸掉对方的依赖。
# 按 --project 分环境：--project client → $root/venv/client，没带 --project 时用 $root/venv/shared。
project=
prev=
for arg in "$@"; do
    case $arg in
        --project=*) project=${arg#--project=} ;;
        *)
            if [ "$prev" = "--project" ]; then
                project=$arg
            fi
            ;;
    esac
    prev=$arg
done

venv=$root/venv/shared
case ${project##*/} in
    .|..|"") ;;
    *) venv=$root/venv/${project##*/} ;;
esac

MINECRAFTTOOLS_BUILD_ROOT=$root
CARGO_TARGET_DIR=$root/cargo-target
UV_PROJECT_ENVIRONMENT=$venv
UV_CACHE_DIR=${UV_CACHE_DIR:-$root/uv-cache}
PYTHONPYCACHEPREFIX=$root/pycache
MINECRAFTTOOLS_DIST_DIR=$root/dist
MINECRAFTTOOLS_WORK_DIR=$root/pyinstaller
export MINECRAFTTOOLS_BUILD_ROOT CARGO_TARGET_DIR UV_PROJECT_ENVIRONMENT \
    UV_CACHE_DIR PYTHONPYCACHEPREFIX MINECRAFTTOOLS_DIST_DIR MINECRAFTTOOLS_WORK_DIR

if [ $# -eq 0 ]; then
    echo "usage: ./dev.sh <command> [args...]" >&2
    exit 2
fi

cd "$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"

# setuptools 安装本项目时会把 egg-info 写进 client/src，用完就删。
trap 'rm -rf client/src/*.egg-info; find . -path ./.git -prune -o -type d -name __pycache__ -prune -exec rm -rf {} + 2>/dev/null' EXIT
"$@"
