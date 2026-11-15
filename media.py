"""Common pixel-perfect crop engine for images and video frames."""
from __future__ import annotations

import re
import hashlib
import json
import math
import shutil
import subprocess
import tempfile
from pathlib import Path

from runtime_paths import system_environment

from PIL import Image, ImageOps

SIZE = 480
IMAGE_EXT = {".png", ".jpg", ".jpeg", ".webp", ".bmp"}
VIDEO_EXT = {".mp4", ".mov", ".mkv", ".webm", ".avi", ".gif"}
MAX_SECONDS = 20
STATIC_BACKGROUND_FPS = 2
STATIC_BACKGROUND_FRAMES = 2


def defaults(values):
    values = values or {}
    fit = str(values.get("fit", "cover"))
    if fit not in ("cover", "contain", "free"):
        raise ValueError("无效的画面裁切模式")

    try:
        rotation = float(values.get("rotation", 0))
    except (TypeError, ValueError):
        raise ValueError("素材旋转角度必须是有效数字") from None
    if not math.isfinite(rotation):
        raise ValueError("素材旋转角度必须是有限数字")
    rotation %= 360

    fps = int(values.get("fps", 2))
    if not 1 <= fps <= 30:
        raise ValueError("视频帧率应为 1–30 FPS")

    bg = str(values.get("background", "#101827"))
    if not re.fullmatch(r"#[a-fA-F0-9]{6}", bg):
        raise ValueError("无效背景色")

    zoom = float(values.get("zoom", 1.0))
    if not 0.35 <= zoom <= 4.0:
        raise ValueError("缩放必须在 0.35–4.0 之间")

    x = float(values.get("offset_x", 0))
    y = float(values.get("offset_y", 0))
    if not (-960 <= x <= 960 and -960 <= y <= 960):
        raise ValueError("拖动位置超出范围")

    return {
        "fit": fit, "rotation": rotation, "fps": fps,
        "background": bg, "zoom": zoom,
        "offset_x": x, "offset_y": y,
    }


def image_frame(image: Image.Image, settings: dict) -> Image.Image:
    settings = defaults(settings)
    image = ImageOps.exif_transpose(image).convert("RGBA")
    if settings["rotation"]:
        image = image.rotate(-settings["rotation"], expand=True,
                             resample=Image.Resampling.BICUBIC)
    result = Image.new("RGBA", (SIZE, SIZE), settings["background"])

    if settings["fit"] == "cover":
        output = ImageOps.fit(image, (SIZE, SIZE), method=Image.Resampling.LANCZOS)
        result.alpha_composite(output)
    elif settings["fit"] == "contain":
        output = ImageOps.contain(image, (SIZE, SIZE), method=Image.Resampling.LANCZOS)
        result.alpha_composite(
            output, ((SIZE - output.width)//2, (SIZE - output.height)//2)
        )
    else:
        # Zoom=1 means just covering the 480x480 crop. Positive offsets move
        # the picture to the right/down; pixels outside the canvas are clipped.
        factor = max(SIZE/image.width, SIZE/image.height) * settings["zoom"]
        dims = (max(1, round(image.width * factor)),
                max(1, round(image.height * factor)))
        output = image.resize(dims, Image.Resampling.LANCZOS)
        position = (
            round((SIZE - output.width)/2 + settings["offset_x"]),
            round((SIZE - output.height)/2 + settings["offset_y"]),
        )
        result.alpha_composite(output, position)

    return result.convert("RGB")


def run_ffmpeg(args, timeout=150):
    if shutil.which("ffmpeg") is None:
        raise RuntimeError("缺少 ffmpeg，请安装：sudo pacman -S ffmpeg")
    try:
        done = subprocess.run(
            ["ffmpeg", "-nostdin", "-y", "-hide_banner", "-loglevel", "error", *args],
            capture_output=True, text=True, timeout=timeout, check=False,
            env=system_environment(),
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("FFmpeg 视频处理超时") from exc
    if done.returncode:
        raise RuntimeError("FFmpeg 错误：" + done.stderr[-600:])


def duration_of(video: Path):
    if shutil.which("ffprobe") is None:
        raise RuntimeError("缺少 ffprobe")
    p = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", str(video)],
        capture_output=True, text=True, timeout=15,
        env=system_environment(),
    )
    if p.returncode or not p.stdout.strip():
        raise RuntimeError("视频无法识别时长")
    return min(MAX_SECONDS, float(p.stdout.strip()))


def render(src: Path, kind: str, dest: Path, settings: dict):
    options = defaults(settings)

    if kind == "image":
        with Image.open(src) as original:
            image_frame(original, options).save(dest, format="PNG", optimize=True)
        return {"duration": 0, "frames": 1, "fps": 0}

    # Extract low-rate decoded frames and process each with *the same Pillow
    # crop engine* used for images. This guarantees drag/zoom coordinates match.
    with tempfile.TemporaryDirectory(prefix="turing-frames-") as folder:
        tmp = Path(folder)
        extracted = tmp / "raw_%04d.png"
        run_ffmpeg([
            "-i", str(src), "-t", str(MAX_SECONDS),
            "-vf", f"fps={options['fps']},"
                   "scale=960:960:force_original_aspect_ratio=decrease:flags=lanczos",
            "-fps_mode", "vfr",
            str(extracted)
        ], timeout=180)
        frames = sorted(tmp.glob("raw_*.png"))
        if not frames:
            raise RuntimeError("视频没有可用帧")
        for idx, path in enumerate(frames, start=1):
            with Image.open(path) as frame:
                output = image_frame(frame, options)
                output.save(tmp / f"screen_{idx:04d}.png")

        run_ffmpeg([
            "-framerate", str(options["fps"]), "-i", str(tmp / "screen_%04d.png"),
            "-an", "-sn", "-c:v", "libx264", "-preset", "fast",
            "-crf", "18", "-pix_fmt", "yuv420p",
            "-movflags", "+faststart", str(dest),
        ], timeout=180)

    if not dest.is_file() or dest.stat().st_size == 0:
        raise RuntimeError("未能生成视频预览")
    return {"duration": len(frames)/options["fps"], "frames": len(frames), "fps": options["fps"]}


def iter_video_frames(video: Path, stop_event, loop=False):
    frame_bytes = SIZE * SIZE * 3
    proc = subprocess.Popen(
        ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error",
         *(["-stream_loop", "-1"] if loop else []),
         "-i", str(video), "-an", "-sn", "-f", "rawvideo",
         "-pix_fmt", "rgb24", "pipe:1"],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=system_environment(),
    )
    try:
        while not stop_event.is_set():
            data = bytearray()
            while len(data) < frame_bytes:
                part = proc.stdout.read(frame_bytes - len(data))
                if not part:
                    break
                data.extend(part)
            if not data:
                if loop and not stop_event.is_set():
                    raise RuntimeError("循环视频解码意外结束，正在重新打开 FFmpeg")
                break
            if len(data) != frame_bytes:
                raise RuntimeError("收到不完整视频帧")
            yield Image.frombytes("RGB", (SIZE, SIZE), bytes(data))
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
        if proc.stdout is not None:
            proc.stdout.close()


def device_video_compensation(mount_ccw):
    """Clockwise compensation remaining after the MCU player's fixed 90°."""
    if mount_ccw not in (0, 90, 180, 270):
        raise ValueError("安装角度无效")
    return (mount_ccw - 90) % 360


def device_video_signature(source, settings, mount_ccw):
    """Version 3 invalidates both full-compensation and uncompensated clips."""
    compensation = device_video_compensation(mount_ccw)
    digest = hashlib.sha256()
    with Path(source).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    cache = {"version": 3, "settings": defaults(settings), "mount_compensation": compensation}
    if Path(source).suffix.lower() in IMAGE_EXT:
        cache["static_image_clip"] = {"fps": STATIC_BACKGROUND_FPS, "frames": STATIC_BACKGROUND_FRAMES}
    digest.update(json.dumps(cache, sort_keys=True).encode())
    return digest.hexdigest()


def render_device_video(source, dest, settings, progress=None, mount_ccw=90):
    """Create a native background clip, excluding the separately updated layers.

    Static images become two identical frames, reusing the established device
    video/transparent-overlay path. Apply material rotation before cropping,
    then mount compensation minus the MCU player's 90 degrees.
    """
    compensation = device_video_compensation(mount_ccw)
    options = defaults(settings)
    with tempfile.TemporaryDirectory(prefix="turing-device-") as folder:
        if Path(source).suffix.lower() in IMAGE_EXT:
            base = Path(folder) / "background.png"
            with Image.open(source) as original:
                image_frame(original, options).save(base, "PNG")
            inputs = ["-loop", "1", "-framerate", str(STATIC_BACKGROUND_FPS), "-i", str(base)]
            output_limit = ["-frames:v", str(STATIC_BACKGROUND_FRAMES)]
            info = {"duration": STATIC_BACKGROUND_FRAMES / STATIC_BACKGROUND_FPS,
                    "frames": STATIC_BACKGROUND_FRAMES, "fps": STATIC_BACKGROUND_FPS, "static": True}
        else:
            base = Path(folder) / "background.mp4"
            info = render(Path(source), "video", base, options)
            inputs, output_limit = ["-i", str(base)], []
        filters = {0: [], 90: ["-vf", "transpose=clock"],
                   180: ["-vf", "hflip,vflip"], 270: ["-vf", "transpose=cclock"]}
        run_ffmpeg([*inputs, *filters[compensation], *output_limit, "-an", "-sn",
                    "-c:v", "libx264", "-profile:v", "baseline", "-level:v", "3.0",
                    "-pix_fmt", "yuv420p", "-preset", "fast", "-crf", "20",
                    "-maxrate", "2200k", "-bufsize", "4400k",
                    "-movflags", "+faststart", str(dest)], timeout=180)
    if progress:
        progress(info["frames"], info["frames"])
    return info
