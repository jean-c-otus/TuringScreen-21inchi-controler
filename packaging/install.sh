#!/bin/sh
set -eu

package_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
install_dir=${TURING_INSTALL_DIR:-"$HOME/.local/opt/turing-studio"}
bin_dir=${TURING_BIN_DIR:-"$HOME/.local/bin"}
config_dir=${XDG_CONFIG_HOME:-"$HOME/.config"}
data_dir=${XDG_DATA_HOME:-"$HOME/.local/share"}
app_data_dir=${TURING_DATA_DIR:-"$data_dir/turing-studio"}
app_log_dir=${TURING_LOG_DIR:-"${XDG_STATE_HOME:-"$HOME/.local/state"}/turing-studio"}
udev_rules_dir=${TURING_UDEV_RULES_DIR:-/etc/udev/rules.d}
import_dir=
if [ "$#" -gt 0 ]; then
    if [ "$#" -ne 2 ] || [ "$1" != "--import-data" ]; then
        echo "用法：$0 [--import-data 旧工作区完整路径]" >&2
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
        1|2) break ;;
        0) echo "已退出，未执行任何操作。"; exit 0 ;;
        *) echo "请输入 1、2 或 0。" ;;
    esac
done

if [ "$(id -u)" -eq 0 ]; then
    echo "请以桌面用户运行 ./install.sh，不要给整个脚本加 sudo。" >&2
    exit 1
fi
for path in "$install_dir" "$bin_dir" "$config_dir" "$data_dir" "$app_data_dir" "$app_log_dir" "$udev_rules_dir"; do
    case "$path" in
        /*) ;;
        *) echo "安装及 XDG 目录必须使用绝对路径：$path" >&2; exit 1 ;;
    esac
done
if [ -L "$install_dir" ]; then
    echo "安装目录不能是符号链接：$install_dir" >&2
    exit 1
fi
install_dir=$(realpath -m -- "$install_dir")
bin_dir=$(realpath -m -- "$bin_dir")
# Never allow recursive application removal to encompass home or shared data.
for protected in "$HOME" "$bin_dir" "$config_dir" "$data_dir" "$app_data_dir" "$app_log_dir"; do
    protected=$(realpath -m -- "$protected")
    case "$protected/" in
        "$install_dir/"*) echo "安装目录不能包含用户主目录或共享目录：$install_dir" >&2; exit 1 ;;
    esac
done
for protected in "$app_data_dir" "$app_log_dir"; do
    protected=$(realpath -m -- "$protected")
    case "$install_dir/" in
        "$protected/"*) echo "程序不能安装在用户数据或日志目录中。" >&2; exit 1 ;;
    esac
done
if [ -e "$install_dir" ] && { [ ! -x "$install_dir/app/turing-studio" ] || [ ! -d "$install_dir/app/_internal" ]; }; then
    echo "目标已存在但不是有效的 Turing Studio 安装，未改动：$install_dir" >&2
    exit 1
fi
if [ "$action" = 2 ] && [ ! -d "$install_dir" ]; then
    echo "尚未安装 Turing Studio：$install_dir"
    exit 0
fi
mkdir -p -- "$(dirname -- "$install_dir")"
# Keep the lock inode after exit so simultaneous installers share the same lock.
exec 9>"${install_dir}.install.lock"
if ! flock -n 9; then
    echo "另一个安装管理进程正在运行，请稍后再试。" >&2
    exit 1
fi
previous="${install_dir}.previous"
if [ -e "$previous" ] || [ -L "$previous" ]; then
    echo "发现尚未处理的旧版本备份，请先检查：$previous" >&2
    exit 1
fi

if [ "$action" = 1 ]; then
    case "$package_dir/" in
        "$install_dir/"*) echo "请使用解压后的新安装包执行安装 / 更新。" >&2; exit 1 ;;
    esac
    case "$install_dir/" in
        "$package_dir/"*) echo "安装目录不能位于当前安装包内。" >&2; exit 1 ;;
    esac
    for file in app/turing-studio install.sh 99-turing-studio.rules DESKTOP.md INSTALL.md SHA256SUMS; do
        if [ ! -f "$package_dir/$file" ]; then
            echo "安装包不完整，缺少：$file" >&2; exit 1
        fi
    done
    if ! (cd -- "$package_dir" && sha256sum --check --quiet SHA256SUMS); then
        echo "安装包校验失败，请重新下载并解压。" >&2; exit 1
    fi
    if [ -n "$import_dir" ] && [ ! -f "$import_dir/config/state.json" ]; then
        echo "旧工作区中未找到 config/state.json：$import_dir" >&2; exit 1
    fi
    if ! command -v udevadm >/dev/null 2>&1; then
        echo "缺少 udevadm，请先安装 systemd。" >&2; exit 1
    fi
    if ! command -v sudo >/dev/null 2>&1; then
        echo "安装系统 USB 规则需要 sudo，请先安装并配置 sudo。" >&2; exit 1
    fi
    "$package_dir/app/turing-studio" --check
    status_app="$package_dir/app/turing-studio"
else
    status_app="$install_dir/app/turing-studio"
fi
# A status failure is also unsafe to ignore: an existing backend may be stuck.
if ! status=$("$status_app" --status); then
    echo "无法确认后台状态，请先从托盘退出程序，再重新运行安装管理。" >&2; exit 1
fi
if printf '%s\n' "$status" | grep -q '"ok": true'; then
    echo "请先在托盘右键退出 Turing Studio，再执行安装、更新或卸载。" >&2; exit 1
fi

entry="$data_dir/applications/turing-studio.desktop"
autostart="$config_dir/autostart/turing-studio.desktop"
command_path="$bin_dir/turing-studio"
for path in "$entry" "$autostart" "$command_path"; do
    if [ -d "$path" ]; then
        echo "入口路径被目录占用，未改动：$path" >&2; exit 1
    fi
done
if [ "$action" = 1 ] && { [ -e "$command_path" ] || [ -L "$command_path" ]; }; then
    if [ ! -L "$command_path" ] || [ "$(readlink -- "$command_path")" != "$install_dir/app/turing-studio" ]; then
        echo "命令入口已被其他文件占用，未改动：$command_path" >&2; exit 1
    fi
fi
transaction=$(mktemp -d "${install_dir}.install.XXXXXX")
committed=0
promoted=0
entries_touched=0
restore_entry() {
    target=$1
    snapshot=$2
    rm -f -- "$target" || return 1
    if [ -e "$snapshot" ] || [ -L "$snapshot" ]; then
        cp -a -- "$snapshot" "$target" || return 1
    fi
}
cleanup() {
    result=$?
    trap - EXIT HUP INT TERM
    set +e
    rollback_failed=0
    if [ "$committed" -eq 0 ] && [ "$result" -ne 0 ]; then
        if [ "$promoted" -eq 1 ]; then
            rm -rf -- "$install_dir" || rollback_failed=1
        fi
        if [ -d "$previous" ]; then
            mv -T -- "$previous" "$install_dir" || rollback_failed=1
        fi
        if [ "$entries_touched" -eq 1 ]; then
            restore_entry "$entry" "$transaction/entry" || rollback_failed=1
            restore_entry "$autostart" "$transaction/autostart" || rollback_failed=1
            restore_entry "$command_path" "$transaction/command" || rollback_failed=1
        fi
        echo "操作失败，已尝试恢复原程序及入口；用户数据未删除。" >&2
    fi
    if [ "$rollback_failed" -eq 0 ]; then
        rm -rf -- "$transaction"
    else
        echo "恢复未完全成功，备份保留在：$previous 和 $transaction" >&2
    fi
    exit "$result"
}
trap cleanup EXIT
trap 'exit 1' HUP INT TERM
for name in entry autostart command; do
    case "$name" in
        entry) path=$entry ;;
        autostart) path=$autostart ;;
        command) path=$command_path ;;
    esac
    if [ -e "$path" ] || [ -L "$path" ]; then
        cp -a -- "$path" "$transaction/$name"
    fi
done

if [ "$action" = 2 ]; then
    mv -T -- "$install_dir" "$previous"
    entries_touched=1
    rm -f -- "$entry" "$autostart"
    if [ -L "$command_path" ] && [ "$(readlink -- "$command_path")" = "$install_dir/app/turing-studio" ]; then
        rm -f -- "$command_path"
    fi
else
    mkdir -- "$transaction/new"
    cp -a -- "$package_dir/app" "$transaction/new/app"
    for file in install.sh 99-turing-studio.rules DESKTOP.md INSTALL.md; do
        cp -p -- "$package_dir/$file" "$transaction/new/$file"
    done
    # Elevation is limited to installing and reloading the independent rule.
    rule_source="$transaction/new/99-turing-studio.rules"
    rule_target="$udev_rules_dir/99-turing-studio.rules"
    udevadm verify "$rule_source"
    echo "正在安装 USB 权限规则：$rule_target"
    echo "以下两步需要管理员权限，仅用于安装规则和重载 udev。"
    sudo install -D -m 0644 -- "$rule_source" "$rule_target"
    sudo udevadm control --reload-rules
    if [ -d "$install_dir" ]; then
        mv -T -- "$install_dir" "$previous"
    fi
    promoted=1
    mv -T -- "$transaction/new" "$install_dir"
    if [ -n "$import_dir" ]; then
        "$install_dir/app/turing-studio" --import-data "$import_dir"
    fi
    entries_touched=1
    "$install_dir/app/turing-studio" --register-desktop
    mkdir -p -- "$bin_dir"
    ln -s -- "$install_dir/app/turing-studio" "$transaction/new-command"
    mv -Tf -- "$transaction/new-command" "$command_path"
fi
# Commit before disposing of backups; a partial backup deletion must never
# replace a working installation with a partially deleted old version.
committed=1
if ! rm -rf -- "$previous"; then
    echo "操作已完成，但旧文件未完全清理，请稍后处理：$previous" >&2
fi
if [ "$action" = 2 ]; then
    echo "Turing Studio 已卸载，配置、素材、字体和日志已保留。"
    echo "独立 USB 权限规则已保留：$udev_rules_dir/99-turing-studio.rules"
else
    echo "安装完成：$install_dir"
    echo "请从应用菜单打开 Turing Studio；更新会保留用户数据和启动设置。"
    echo "USB 规则用于后续设备事件；首次配置权限后请重新插拔屏幕。"
fi
