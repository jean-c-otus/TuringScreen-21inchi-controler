# Turing Studio V3

**本地 Web 编辑器 + PyUSB/libusb + Turing 2.1" (480×480, Rev C)**

这是为当前已成功使用 `libusb` 显示首张图片的 2.1" 屏幕设计的实验性版本。

设备：
- `1d6b:0121` / `20080411`，CDC 数据 Interface 1，Bulk OUT `0x01`、IN `0x81`
- `1a86:ca21` / `CT21INCH` 用于唤醒
- Arch Linux / fish / niri Wayland / Nvidia 4080 SUPER / i7-14700K
- 水冷头**逆时针旋转 90° 安装**（UI 默认开启反向画面补偿）

> 已有实机验证：V1 全屏图片发送、独立 libusb HELLO。
> V3 新增 **Rev C 0xCC 局部更新** 和长时间视频/传感器刷新，其 USB 稳定性仍需在你这块 2.1" 屏幕上实机验证。所有离线测试均使用模拟 USB 设备，不代表实际传输速度。

## 运行

下载打包版请先看[安装教程](INSTALL.md)。登录启动、静默启动、托盘及自动恢复的详细说明见 [DESKTOP.md](DESKTOP.md)。

```bash
sudo pacman -S --needed python python-pip libusb ffmpeg
python -m venv .venv
.venv/bin/python -m pip install -r requirements-desktop.txt
.venv/bin/python desktop.py
```

程序自动打开系统 Chrome 应用窗口，后台服务仅监听本机地址。

**先关闭旧版 Turing Web Studio 服务和其他屏幕控制软件。**
请通过 udev 授权当前用户访问 USB，以当前用户运行程序。

依赖：Flask、Pillow、psutil、PyUSB、pyserial、dbus-next、Chrome、libusb、FFmpeg。打包版自带 libusb。GPU 传感器需要 `nvidia-smi` 命令可用。

### 授予 USB 权限

安装包的 `install.sh` 在“安装 / 更新”时会自动安装独立规则并重载 udev，按需请求 sudo 权限。
从源码运行或只需配置设备权限时，可以手动安装随项目提供的规则：

```bash
sudo install -m 0644 packaging/99-turing-studio.rules /etc/udev/rules.d/99-turing-studio.rules
sudo udevadm control --reload-rules
```

规则由系统 udev 读取，不依赖 InfoPanel。上述操作只重载规则，不主动触发设备重新枚举。

## 功能

### 1. 自由图片/视频裁切
- 上传 PNG / JPG / WebP / BMP，以及 MP4 / MOV / WebM / MKV / AVI / **GIF**。
- 铺满 / 完整 / 自由裁切；自由模式下鼠标拖动、滚轮缩放、触摸移动及双指缩放。
- 水冷头物理旋转补偿与素材旋转分别设置；素材角度可自由输入，支持小数、负数和多圈角度。
- Python 根据浏览器提交的裁切参数生成**权威预览**；编辑中的 Canvas 是快速草稿预览。
- 最多 80 MB，视频/GIF 处理前 20 秒；内部 MCU 播放可选 1–30 FPS，电脑逐帧发送可选 1–8 FPS。

### 2. 文字 / 符号 / 传感器图层
- 添加文字、Unicode 符号（实际字符支持依赖系统字体）或传感器数值。
- 单击图层后改文字、颜色、字号、X/Y；直接在圆形画面拖动图层定位。
- 支持最多 12 个叠加图层，Python Pillow 生成合成后的预览。
- 传感器：CPU 封装温度、CPU 使用率、GPU 温度、GPU 使用率、GPU 显存使用率、内存使用率。
- 数据优先从 `coretemp` 的 `/sys/class/hwmon/*/temp1_input` 读取 CPU 温度，NVIDIA GPU 使用 `nvidia-smi`。
- 没有温度数据时显示 `--`，不是编造数据。
- 静态背景搭配传感器图层时，会每 2 秒采集一次并更新发生变化的区域；按「停止发送」可退出实时模式。

### 3. 增量矩形更新（Rev C）
源码中存在 `UPDATE_BITMAP (0xCC)` 的区域传输格式：
- **首次完整画面**：普通 BGRA 全帧发送；
- **后续**：对比 Python 缓存的上一帧与当前帧，按 40×40 的区域检测变化；
- 合并相邻的矩形区域，发送 BGR 格式局部数据（含行地址 / 像素长度）；
- 超过 12 区域或改动面积大于 60% 时改用全屏传输；
- 完全一样的画面不发送任何 USB 数据。

该策略复用了**屏幕当前显示的背景**。这不是写入设备 Flash，也不是让固件独立解码素材。
**若 MCU 复位，必须重新发全图**，因此连接失败后会清掉本地增量缓存。

### 4. 真实 USB 测速与预检
「测量 USB 速度」不是本地 RAM 测试：
- 确保 libusb 已握手；
- **实际发送一帧背景**；
- 实际发送 160×160、240×240 两次**相同像素**的局部更新；
- 使用真实 Bulk OUT + 查询响应的耗时，计算全屏/局部 MiB/s；
- 测速会操作真实屏幕，用户需要先点击确认。

使用全屏或局部策略逐帧发送 GIF / 视频前，如果 10 分钟内无有效测速，会**自动运行预检**；内部 MCU 播放不执行此预检：
- 基于前最多 10 帧的变化区域估计最坏传输量；
- 加入至少 30% 的余量和命令切换开销；
- `自动降帧`：选择跳帧后的较低实际帧率；
- `阻止播放`：若需求超过估计上限，不开始连续播放；
- 运行中若部分帧实测比预计慢，还会临时额外跳帧避免累积 USB 队列。

**性能预测不是 MCU 硬件保证**：视频后半段出现全屏变化时仍可能更慢。
测速预检本身必须发送画面才能测到真实设备速度。屏幕 USB 块传输不会等于 USB 480 Mbps 理论链路带宽。

### 5. 图片、GIF 和视频的实际工作方式
- 默认 USB 刷新策略为 `内部MCU为主刷新`；全屏和局部策略保留在选择框中作为调试选项。已有方案仍按保存的策略恢复。
- MCU 策略同时支持图片、GIF 和视频。图片保持 PNG 编辑预览，发送时将裁切和旋转后的背景编码成两帧相同画面的 1 秒 H.264 文件，由屏幕循环显示静态内容，复用视频背景与透明图层差分流程；电脑不持续传输图片背景。图片不使用 GIF / 视频播放帧率设置。
- `固定全屏` / `固定局部`：电脑使用 FFmpeg 解码、Pillow 合成图层，再逐帧经 USB 发送。退出后停止刷新。
- `内部MCU为主刷新`：电脑将原始 GIF / 视频按裁切、素材旋转和所选帧率转成 480×480 H.264 文件，上传到屏幕存储并校验大小，再请求循环播放。视频背景按 MCU 已经偏转 90° 处理，额外顺时针补偿为 `(安装角度 - 90) % 360`，在素材旋转与裁切完成后应用。额外图层仍独立补偿完整安装角度。背景文件不包含额外图层；播放期间电脑只更新透明图层。
- 同一背景通过内容、配置、剩余安装补偿和转码版本签名缓存；保存方案、克隆和启动恢复保留缓存。修改图层不会重新转码背景；修改背景需先停止并重新发送。旧版全量补偿及完全不补偿的视频缓存在下次发送时自动重新生成。
- 透明图层首次通过 `0xCA` 建立完整 BGRA 基准，非空可见区域表通过 `0xD0` 发送；后续使用 `0xCC` 发送差分像素记录和当前 POSLEN。差分比较全部 RGBA 字节，包含旧文字变透明的像素；小屏像素使用压缩 BGRA（B6/G6/R8/A4），不推送视频背景帧。完全没有像素变化时仅查询状态，不发送像素数据。
- 全层基准仅在首次、重连、安装角度变化或设备状态要求重建时发送；增量序号在全层重建后从 0 开始。电脑仅在完整写入后保存基准，写入异常会丢弃基准并进入原有恢复流程。
- 每次图层处理前通过 `0xCF` 查询设备状态，限时等待内容包含 `needReSend` 的回复；不强制逐条等待 `0xCA` / `0xD0` / `0xCC` 的确认。实机 ROM 1.89 会在叠加层初始化后返回 `seq_png_init_sucess`，因此接收循环会记录其他回复并继续寻找本次状态，支持分片或同一次读取中混有多条消息。连续四次缺失有效状态回复才进入恢复；每 5 秒另通过标准 USB 状态请求检测断线。HELLO 同样按设备 ID 内容匹配，避免把迟到的状态回复当成握手结果。
- 停止播放必须匹配到 `media_stop` 确认后才继续切换素材；文件大小、创建文件、上传完成和开始播放也按各自内容匹配。文件大小仅接受独立的十进制回复或明确的不存在标记，不从状态、路径或停止确认中提取数字。无终止符的数字回复会等待短暂静默以接收完整分片；其他迟到回复记录到日志后继续读取。
- 图层按 1 秒目标周期调度，采集、合成和 USB 传输计入周期，不在发送后另等 1 秒。处理超过 1 秒时按实际耗时串行更新，不积压旧帧。界面分别显示本次发送量、像素记录、可见表、累计发送量，以及写入、等待和编码耗时；发送量统计 USB OUT，包含头部、分块填充和状态查询命令，不计读取的回复。日志另记采集与合成耗时。
- 点击停止会停止设备播放；正在上传时先完成当前文件，避免后续命令被当成文件内容。托盘退出会释放 USB，已开始的设备本地背景播放继续，电脑传感器图层停止更新。
- 本地文件播放参考 [Lumi 的 Rev C 实现](https://github.com/waldokilian2/lumi/blob/master/src/lumi/device.py)；透明叠加与差分格式按用户提供的 [Bezel Rev C 逆向协议](https://github.com/jripper77/bezel-evo/blob/windows-improvements/docs/reverse-engineering/protocol-turing-rev-c.md)及其 [像素格式文档](https://github.com/jripper77/bezel-evo/blob/windows-improvements/docs/reverse-engineering/pixel-formats.md)对齐。此次增量接入仅进行静态检查，尚未进行实机验证；此策略出错会记录并重试，不会自动改为电脑推帧。
- 预览由 Python 合成，传感器数字是生成预览时的快照；设备运行时使用新的实时读数。

## 故障与恢复
- 运行时需要持续占有 USB 数据接口，**不要**同时打开旧版 turing-smart-screen-python。
- 如果发生 `[Errno 75] Overflow` 或 Bulk 超时：程序会记录日志、释放 USB 会话，再自动重连并重新初始化画面，**不会接着重发错误的半帧**。
- 可以先停止发送，再使用页面的「USB 手动重连」；如果屏幕固件失去响应，可从托盘退出程序后使用 `resetusb.sh` 重新授权设备。主程序连接时会自动尝试唤醒屏幕。
- FPS 估算有余量并不会阻止所有设备固件问题，建议先测试 1 FPS GIF、单个 CPU 温度图层，再逐渐增加帧率。

## 项目结构

```text
turing-web-studio-v3/
  desktop.py             桌面程序入口、Chrome 窗口与后台生命周期
  desktop_settings.py    登录启动与静默启动设置
  desktop_tray.py        系统托盘及右键菜单
  app.py                 本地 Web API、测速/发送任务控制和预检
  device.py              libusb HELLO / 全屏帧 / 0xCC 局部更新 / USB 实测速
  media.py               图片、视频、GIF 的裁切与解码
  scene.py               文字/符号/Pillow 绘制与 CPU/GPU 传感器
  static/index.html      HTML/CSS/JS 页面，无 Node 构建工具
  workspace.py           草稿、保存方案与发送恢复快照
  runtime_paths.py       用户数据、资源路径与 libusb 查找
  requirements*.txt      运行、桌面与构建依赖
  packaging/             打包与交互式安装/卸载脚本
  tests/                 自动化验证
  resetusb.sh            仅目标屏幕的 USB 重新授权
```

数据位于 `~/.local/share/turing-studio/`，项目目录不存放用户素材；旧工作区可通过安装脚本导入。页面只监听 `127.0.0.1`，不会将媒体发送到公网。
浏览器编辑预览使用 Canvas，USB 真正发送由 Python 处理。修改图层后需要「应用修改并预览」。

协议参考：[mathoudebine/turing-smart-screen-python](https://github.com/mathoudebine/turing-smart-screen-python) 的
`lcd_comm_rev_c.py`（GPL-3.0-or-later）及相关 libusb Issue。发布衍生代码时请遵守上游许可证与署名要求。
