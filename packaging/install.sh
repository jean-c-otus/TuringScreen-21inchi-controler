#!/bin/sh
set -eu

package_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
install_dir=${TURING_INSTALL_DIR:-"$HOME/.local/opt/turing-studio"}
bin_dir=${TURING_BIN_DIR:-"$HOME/.local/bin"}
udev_rules_dir=${TURING_UDEV_RULES_DIR:-/etc/udev/rules.d}
import_dir=
if [ "$#" -gt 0 ]; then
    if [ "$#" -ne 2 ] || [ "$1" != "--import-data" ]; then
        echo "Usage: $0 [--import-data OLD_WORKSPACE]" >&2
        exit 2
    fi
    import_dir=$2
fi

echo "Turing Studio 安装管理"
echo "  1) 安装 / 更新"
echo "  2) 卸载（保留配置、素材、字体和日志）"
echo "  0) 退出"
while :; do
    printf '%s' "请选择 [1/2/0]："
    if ! IFS= read -r action; then
        printf '\n%s\n' "未收到选择，未执行任何操作。" >&2
        exit 1
    fi
    case "$action" in
        1) break ;;
        2)
            if [ ! -x "$install_dir/app/turing-studio" ] || [ ! -d "$install_dir/app/_internal" ]; then
                echo "尚未安装 Turing Studio：$install_dir"
                exit 0
            fi
            if "$install_dir/app/turing-studio" --status | grep -q '"ok": true'; then
                echo "请先在托盘右键退出 Turing Studio，再执行卸载。" >&2
                exit 1
            fi
            config_dir=${XDG_CONFIG_HOME:-"$HOME/.config"}
            data_dir=${XDG_DATA_HOME:-"$HOME/.local/share"}
            rm -f -- "$config_dir/autostart/turing-studio.desktop" "$data_dir/applications/turing-studio.desktop"
            if [ -L "$bin_dir/turing-studio" ] && [ "$(readlink -- "$bin_dir/turing-studio")" = "$install_dir/app/turing-studio" ]; then
                rm -f -- "$bin_dir/turing-studio"
            fi
            rm -rf -- "$install_dir"
            echo "Turing Studio 已卸载，配置、素材、字体和日志已保留。"
            echo "独立 USB 权限规则已保留：$udev_rules_dir/99-turing-studio.rules"
            exit 0
            ;;
        0) echo "已退出，未执行任何操作。"; exit 0 ;;
        *) echo "请输入 1、2 或 0。" ;;
    esac
done

if [ -n "$import_dir" ] && [ ! -f "$import_dir/config/state.json" ]; then
    echo "Cannot find config/state.json in the old workspace: $import_dir" >&2
    exit 1
fi

"$package_dir/app/turing-studio" --check
if "$package_dir/app/turing-studio" --status | grep -q '"ok": true'; then
    echo "Please exit Turing Studio from the tray before installing or upgrading." >&2
    exit 1
fi
mkdir -p -- "$(dirname -- "$install_dir")" "$bin_dir"
stage=$(mktemp -d "${install_dir}.install.XXXXXX")
replaced=0
previous="${install_dir}.previous"
cleanup() {
    result=$?
    if [ "$result" -ne 0 ] && [ "$replaced" -eq 1 ]; then
        rm -rf -- "$install_dir"
        mv -- "$previous" "$install_dir"
    fi
    if [ -n "$stage" ]; then rm -rf -- "$stage"; fi
}
trap cleanup EXIT
trap 'exit 1' HUP INT TERM
cp -a -- "$package_dir/app" "$stage/app"
cp -- "$package_dir/install.sh" "$stage/install.sh"
cp -- "$package_dir/99-turing-studio.rules" "$stage/99-turing-studio.rules"
cp -- "$package_dir/DESKTOP.md" "$stage/DESKTOP.md"
if [ -e "$previous" ]; then
    echo "Previous installation backup already exists: $previous" >&2
    exit 1
fi

# Configure only the system device permissions with elevated privileges. Keep
# the application, imported data and desktop entry owned by the invoking user.
rule_source="$package_dir/99-turing-studio.rules"
rule_target="$udev_rules_dir/99-turing-studio.rules"
if ! udevadm verify "$rule_source"; then
    echo "USB 权限规则校验失败，安装已中止。" >&2
    exit 1
fi
echo "正在安装 USB 权限规则：$rule_target"
if [ "$(id -u)" -eq 0 ] || [ -w "$udev_rules_dir" ]; then
    if ! install -D -m 0644 -- "$rule_source" "$rule_target" || ! udevadm control --reload-rules; then
        echo "USB 权限规则安装或重载失败，安装已中止。" >&2
        exit 1
    fi
else
    echo "以下操作需要管理员权限，仅用于安装规则和重载 udev。"
    if ! sudo install -D -m 0644 -- "$rule_source" "$rule_target" || ! sudo udevadm control --reload-rules; then
        echo "未能取得管理员权限，或 USB 规则安装/重载失败；安装已中止。" >&2
        exit 1
    fi
fi
echo "USB 权限规则已安装并重载。"

if [ -d "$install_dir" ]; then
    mv -- "$install_dir" "$previous"
    replaced=1
fi
if ! mv -- "$stage" "$install_dir"; then
    exit 1
fi
stage=
if [ -n "$import_dir" ]; then
    "$install_dir/app/turing-studio" --import-data "$import_dir"
fi
"$install_dir/app/turing-studio" --register-desktop
ln -sfn -- "$install_dir/app/turing-studio" "$bin_dir/turing-studio"
rm -rf -- "$previous"
replaced=0
echo "Installed: $install_dir"
echo "Open Turing Studio from your application launcher. Your data is kept across upgrades."
