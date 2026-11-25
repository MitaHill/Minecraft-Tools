#!/bin/sh
# Installs MinecraftTools into a per-user directory. Run by the makeself .run payload.
set -eu

source_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
install_dir=${MINECRAFTTOOLS_INSTALL_DIR:-${XDG_DATA_HOME:-$HOME/.local/share}/minecrafttools}
bin_dir=${XDG_BIN_HOME:-$HOME/.local/bin}
applications_dir=${XDG_DATA_HOME:-$HOME/.local/share}/applications

mkdir -p "$install_dir" "$bin_dir" "$applications_dir"

install -m 0755 "$source_dir/MinecraftToolsLauncher" "$install_dir/MinecraftToolsLauncher"
install -m 0644 "$source_dir/bootstrap.json" "$install_dir/bootstrap.json"
install -m 0644 "$source_dir/bootstrap.tar.gz" "$install_dir/bootstrap.tar.gz"
install -m 0755 "$source_dir/uninstall.sh" "$install_dir/uninstall.sh"

ln -sf "$install_dir/MinecraftToolsLauncher" "$bin_dir/minecrafttools"

cat > "$applications_dir/minecrafttools.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=MinecraftTools
Exec="$install_dir/MinecraftToolsLauncher"
Terminal=false
Categories=Game;
EOF
chmod 0644 "$applications_dir/minecrafttools.desktop"

if command -v update-desktop-database >/dev/null 2>&1; then
    update-desktop-database "$applications_dir" >/dev/null 2>&1 || true
fi

printf '已安装到 %s\n' "$install_dir"
printf '启动命令：%s\n' "$bin_dir/minecrafttools"
