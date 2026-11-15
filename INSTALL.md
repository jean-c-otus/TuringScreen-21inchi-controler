# Turing Studio 安装教程

当前安装包面向 **Arch Linux x86_64**，用于图灵 2.1 英寸、480×480、Rev C 屏幕。其他发行版或较旧系统库的兼容性尚未验证。

程序包含 Python 运行环境、Python 依赖和 libusb；复用系统 Chrome/Chromium、FFmpeg。桌面环境需要支持 StatusNotifierItem 的系统托盘，例如 Waybar 的 `tray` 模块。

## 1. 准备依赖

在普通桌面用户的终端中执行：

```sh
sudo pacman -Syu --needed chromium ffmpeg sudo
```

已有 Google Chrome 时可以不安装 Chromium。安装脚本会检查浏览器、FFmpeg 和包内 libusb 是否可用；GPU 传感器额外需要 NVIDIA 驱动提供的 `nvidia-smi`。

## 2. 下载与校验

在仓库的 [Releases](https://github.com/jean-c-otus/TuringScreen-21inchi-controler/releases) 页面下载附件 **`installer.tar.gz`** 和 **`installer.tar.gz.sha256`**，放入同一个目录。`Source code` 是源码压缩包，直接安装请使用 `installer.tar.gz`。

在下载目录执行：

```sh
sha256sum -c installer.tar.gz.sha256
tar -xzf installer.tar.gz
cd installer
./install.sh
```

校验应显示 `installer.tar.gz: OK`。若校验失败，请重新下载，不要继续安装。

## 3. 安装与首次启动

1. 在安装管理菜单输入 `1`，选择“安装 / 更新”。**直接运行 `./install.sh`，不要使用 `sudo ./install.sh`。**
2. 脚本检查包内文件校验和与运行依赖，复制程序并创建应用菜单入口和命令链接。只有安装 USB 规则和重载 udev 两步会请求 sudo 权限。
3. 首次配置 USB 权限后重新插拔屏幕，让新规则在设备事件中生效。独立规则不依赖 InfoPanel，可以在安装后卸载 InfoPanel。
4. 关闭其他占用屏幕的程序，从应用菜单打开 **Turing Studio**。也可以执行 `~/.local/bin/turing-studio`。
5. 上传素材、设置裁切和图层，点击“应用修改并预览”，然后发送。默认使用“内部MCU为主刷新”，背景由屏幕播放，电脑仅更新额外图层。

首次没有发送记录时不会自动发送。成功发送后会保存恢复快照，之后启动自动恢复上次实际发送的配置。已有本软件用户数据时会直接沿用，无需再次导入。

## 4. 登录启动、静默启动与退出

- `Start at login`：勾选后随当前用户登录启动，默认关闭。
- “静默启动”：独立选项，勾选后启动仅驻留托盘，同时恢复发送。
- 关闭编辑窗口会收起到托盘，后台继续工作。再次点击应用菜单入口可以打开界面。
- **真正退出请在托盘图标上右键，选择“退出”。**更新或卸载前也必须这样退出。退出后电脑停止刷新图层并释放 USB；屏幕已开始的本地背景播放可能继续。

## 5. 更新与卸载

更新：先从托盘退出，下载并校验新安装包，在空目录解压，再运行其中的 `./install.sh`，选择 `1`。不要从已安装目录中执行“安装 / 更新”。更新会保留配置、素材、字体、发送恢复快照及启动设置。

卸载：运行安装包的 `./install.sh`，或执行下面的命令，再选择 `2`：

```sh
~/.local/opt/turing-studio/install.sh
```

卸载移除程序、桌面入口、登录启动入口和命令链接，保留用户数据、日志与独立 USB 权限规则。

## 6. 目录与排查

| 内容 | 默认位置 |
| --- | --- |
| 程序 | `~/.local/opt/turing-studio/` |
| 命令入口 | `~/.local/bin/turing-studio` |
| 配置、素材、字体及快照 | `~/.local/share/turing-studio/` |
| 日志 | `~/.local/state/turing-studio/studio.log` |
| 桌面入口 | `~/.local/share/applications/turing-studio.desktop` |
| 登录启动入口 | `~/.config/autostart/turing-studio.desktop` |
| 独立 USB 规则 | `/etc/udev/rules.d/99-turing-studio.rules` |

数据、日志及桌面入口支持相应的 XDG 环境变量。命令不在 PATH 中时可以直接使用上表中的完整路径。

依赖诊断（不访问 USB）：

```sh
~/.local/bin/turing-studio --check
~/.local/bin/turing-studio --status
```

USB 异常会记录日志并持续尝试恢复。若程序已经在运行，先通过托盘退出，再更新或卸载；单纯关闭窗口仍会保留后台进程。安装失败会尝试恢复原程序和各入口；已导入的用户数据、已安装的独立 USB 规则不会被删除。若磁盘或权限问题导致回滚也失败，脚本会打印保留的备份路径，请保留这些备份后查看报错。

仅在迁移尚未导入的旧工作区时使用：

```sh
./install.sh --import-data /完整路径/旧工作区
```

选择 `1` 后只会初始化空数据目录，不覆盖已有数据，也不删除旧文件。程序功能、源码构建及协议细节见 [DESKTOP.md](DESKTOP.md)；发布包中的 `source-code/` 包含对应源码，`build-info.json` 记录版本和提交号。

本次发布仅进行静态检查与打包文件校验，未运行回归、smoke、安装/卸载或实机 USB 测试。
