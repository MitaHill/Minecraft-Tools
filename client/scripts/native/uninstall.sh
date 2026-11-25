#!/bin/sh
# Removes a per-user MinecraftTools installation created by install.sh.
set -eu

install_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
bin_dir=${XDG_BIN_HOME:-$HOME/.local/bin}
applications_dir=${XDG_DATA_HOME:-$HOME/.local/share}/applications

case "$install_dir" in
    / | "") echo "拒绝卸载：安装目录无效" >&2; exit 1 ;;
esac

rm -f "$bin_dir/minecrafttools"
rm -f "$applications_dir/minecrafttools.desktop"

if command -v update-desktop-database >/dev/null 2>&1; then
    update-desktop-database "$applications_dir" >/dev/null 2>&1 || true
fi

cd /
rm -rf "$install_dir"
printf '已卸载 %s\n' "$install_dir"
