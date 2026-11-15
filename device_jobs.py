"""Device-side image/video backgrounds with independently updated overlays."""
from __future__ import annotations

import hashlib
import logging
import os
import time
import uuid
from pathlib import Path

import workspace
from device_media import DeviceMedia, VIDEO_DIR_INTERNAL, VIDEO_DIR_COMPAT
from media import render_device_video, device_video_signature, STATIC_BACKGROUND_FPS
from scene import compose_overlay, sensor_snapshot


ALLOWED_VIDEO_DIRS = {VIDEO_DIR_INTERNAL, VIDEO_DIR_COMPAT}
log = logging.getLogger(__name__)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def run_device_video_job(item, lcd, stop_event, closing_event, update,
                         mount_ccw: int, device_dir: str = VIDEO_DIR_INTERNAL,
                         state_lock=None, on_ready=None):
    """Transcode -> upload/verify -> play locally -> maintain transparent UI.

    The background remains on the panel and loops independently (a still image
    uses two identical frames). The host sends only
    transparent overlay diffs after one CA/D0 initialization, through video-mode
    CC + POSLEN. A user stop stops the panel video; process shutdown leaves
    the already-started local video running.
    """
    if item["kind"] not in ("image", "video"):
        raise ValueError("MCU 刷新支持图片、GIF 或视频方案")
    if device_dir not in ALLOWED_VIDEO_DIRS:
        raise ValueError("设备视频目录无效")

    device_file = Path(item.get("device_video", Path(item["source"]).parent / "device.mp4"))
    item["device_video"] = device_file
    is_image = item["kind"] == "image"
    fps = STATIC_BACKGROUND_FPS if is_image else int(item["settings"].get("fps", 2))
    background_label = "静态图片背景" if is_image else f"本地视频 {fps} FPS"
    signature = device_video_signature(item["source"], item["settings"], mount_ccw)
    update(state="preparing", progress=0, total=0,
           message=("正在准备屏幕静态图片背景：480×480" if is_image else
                    f"正在准备屏幕原生视频：480×480 · H.264 · {fps} FPS"))
    if (not device_file.is_file() or device_file.stat().st_size == 0 or
            item["info"].get("device_video_signature") != signature):
        def transcode_progress(done, total):
            update(state="preparing", progress=done, total=total,
                   message=("正在生成屏幕静态图片背景…" if is_image else
                            f"正在生成设备视频：{done}/{total} 帧 · {fps} FPS"))
        temporary = device_file.with_name(f"device-{uuid.uuid4().hex}.mp4")
        try:
            info = render_device_video(item["source"], temporary, item["settings"],
                                       progress=transcode_progress, mount_ccw=mount_ccw)
            os.replace(temporary, device_file)
        finally:
            temporary.unlink(missing_ok=True)
        item["info"]["device_video_info"] = info
        item["info"]["device_video_signature"] = signature
        item["info"].pop("device_upload_hash", None)
        workspace.write_draft(item)
    local_size = device_file.stat().st_size
    digest = _sha256(device_file)
    if stop_event.is_set():
        update(state="stopped", message="转码已完成；停止请求生效，未触碰设备存储")
        return
    # Reuse the verified video previously uploaded by this draft or the
    # saved snapshot it came from.  A fresh draft has a different ID, but
    # the device content and remote path are still the same.
    cached_path = item["info"].get("device_path")
    cached_hash = item["info"].get("device_upload_hash")
    if (cached_hash == digest and isinstance(cached_path, str)
            and cached_path.startswith(device_dir)
            and cached_path.endswith(".mp4")
            and "/" not in cached_path[len(device_dir):]):
        remote_path = cached_path
    else:
        remote_path = device_dir + f"tws_{digest[:20]}.mp4"
    lcd.connect()
    media = DeviceMedia(lcd)
    media.stop_video()

    update(state="uploading", progress=0, total=local_size,
           message="正在检查屏幕内部存储…")
    remote_size = media.get_file_size(remote_path)
    if stop_event.is_set():
        update(state="stopped", message="已停止；未删除或上传设备文件")
        return
    uploaded_hash = item["info"].get("device_upload_hash")
    if remote_size != local_size or uploaded_hash != digest:
        if remote_size > 0:
            # Immutable saved snapshots may still point to this media file.
            # Never delete a remote video to replace a draft's content.
            update(state="uploading", progress=0, total=local_size,
                   message="发现同名旧背景；保留旧文件，改用新文件名上传…")
            remote_path = device_dir + (
                f"tws_{digest[:16]}_{time.time_ns() % 100000000:08d}.mp4"
            )
        if stop_event.is_set():
            update(state="stopped", message="已停止；没有开始新的文件上传")
            return

        def on_progress(sent, total):
            percent = min(100, int(sent * 100 / max(1, total)))
            update(state="uploading", progress=sent, total=total,
                   bytes=sent,
                   message=f"正在上传到屏幕内部存储：{percent}% · "
                           f"{sent / 1024 / 1024:.2f}/{total / 1024 / 1024:.2f} MiB")

        uploaded = media.upload_file(device_file, remote_path, progress=on_progress)
        if not uploaded.get("verified"):
            raise RuntimeError("设备端文件校验没有通过")
        item["info"]["device_upload_hash"] = digest
        item["info"]["device_path"] = remote_path
        item["info"].pop("previous_device_path", None)
        workspace.write_draft(item)
    else:
        update(state="uploading", progress=local_size, total=local_size,
               message="屏幕中已有相同版本背景，跳过重复上传")

    if stop_event.is_set():
        update(state="stopped", message="背景已上传并校验；按下停止后未启动显示")
        return

    update(state="running", progress=local_size, total=local_size,
           message=("文件校验完成，正在显示屏幕静态图片背景…" if is_image else
                    "文件校验完成，正在启动屏幕内部 H.264 播放器…"))
    media.play_video(remote_path)

    def finish_playback():
        if not closing_event.is_set():
            media.stop_video()
        update(state="stopped", message=(
            "已停止本地背景与透明图层 · 屏幕文件仍保留，可再次显示"
            if not closing_event.is_set() else
            "程序退出；背景文件保留在屏幕内部，当前画面由设备自行维持"
        ))

    if stop_event.is_set():
        finish_playback()
        return

    guard = state_lock or workspace.lock
    initial_tick = time.monotonic()
    with guard:
        layers = [dict(layer) for layer in item["layers"]]
        last_revision = item.get("revision", 1)
    # Clear stale overlays even when this scheme has no layers.
    lcd.initialize_video_overlay(mount_ccw)
    completion_times = []
    last_timing_log = float("-inf")
    overlay_snapshot = None

    def refresh_overlay(current_layers, reuse=False):
        nonlocal last_timing_log, overlay_snapshot
        sampled = time.monotonic()
        if reuse and overlay_snapshot is not None:
            rendered = sampled
            overlay = overlay_snapshot
        else:
            readings = sensor_snapshot(force=True)
            rendered = time.monotonic()
            overlay = compose_overlay(current_layers, readings)
            overlay_snapshot = overlay
        sending = time.monotonic()
        result = lcd.update_video_overlay(overlay, mount_ccw)
        completed = time.monotonic()
        interval = None
        if result.get("method") != "skip":
            completion_times.append(completed)
            del completion_times[:-16]
            if len(completion_times)>1:
                interval = round(1000*(completion_times[-1]-completion_times[0])/(len(completion_times)-1))
            if completed-last_timing_log>=5:
                log.info("MCU overlay: method=%s sensors=%.0fms render=%.0fms overlay=%.0fms "
                         "write=%.0fms wait=%.0fms encode=%.0fms interval=%sms bytes=%d pixels=%d poslen=%d",
                         result.get("method"),
                         1000*(rendered-sampled),1000*(sending-rendered),
                         1000*(completed-sending),1000*result.get("transfer_seconds",0),
                         1000*result.get("wait_seconds",0),1000*result.get("encode_seconds",0),
                         interval,result.get("bytes",0),result.get("pixel_bytes",0),result.get("poslen_bytes",0))
                last_timing_log = completed
        update(overlay_sample_ms=round(1000*(rendered-sampled)),
               overlay_render_ms=round(1000*(sending-rendered)),
               overlay_send_ms=round(1000*(completed-sending)),
               overlay_transfer_ms=round(1000*result.get("transfer_seconds",0)),
               overlay_wait_ms=round(1000*result.get("wait_seconds",0)),
               overlay_encode_ms=round(1000*result.get("encode_seconds",0)),
               overlay_last_bytes=result.get("bytes",0),
               overlay_pixel_bytes=result.get("pixel_bytes",0),
               overlay_poslen_bytes=result.get("poslen_bytes",0),
               **({"overlay_interval_ms":interval} if result.get("method")!="skip" else {}))
        return result

    # Establish even an empty transparent layer, so a previous theme cannot
    # leave stale pixels and the first later edit can use a known CC baseline.
    result = refresh_overlay(layers)

    dynamic = any(layer.get("type") == "sensor" for layer in layers)
    total_overlay_bytes = int(result.get("bytes", 0))
    update(state="live", progress=1, total=1, method=result.get("method", "local-video"),
           bytes=total_overlay_bytes, changed_pixels=result.get("changed_pixels", 0),
           regions=result.get("regions", 0), fps=fps,
           message=(f"屏幕{background_label}；透明图层已初始化，传感器按 1 秒目标周期更新"
                    if dynamic else f"屏幕{background_label}；等待图层变化"))
    if on_ready:
        on_ready(last_revision)

    next_tick = initial_tick + 1.0
    next_health_check = initial_tick + 5.0
    while not stop_event.wait(max(0.0, next_tick-time.monotonic())):
        tick_started = time.monotonic()
        # Work is part of the one-second budget, not followed by another
        # full-second sleep. Slow USB sends stay serial and use fresh readings.
        next_tick = tick_started + 1.0
        if tick_started >= next_health_check:
            lcd.check_alive()
            next_health_check = time.monotonic() + 5.0
        with guard:
            layers = [dict(layer) for layer in item["layers"]]
            revision = item.get("revision", 1)
        dynamic = any(layer.get("type") == "sensor" for layer in layers)
        # Static layers still query status each tick, so needReSend can restore
        # a lost device buffer without re-rendering or re-sending unchanged data.
        result = refresh_overlay(layers,reuse=not dynamic and revision==last_revision)
        last_revision = revision
        total_overlay_bytes += int(result.get("bytes", 0))
        update(state="live", progress=1, total=1,
               method=result.get("method", "skip"), bytes=total_overlay_bytes,
               changed_pixels=result.get("changed_pixels", 0),
               regions=result.get("regions", 0), scene_revision=revision, fps=fps,
               message=(f"{background_label} · 图层变化 {result.get('changed_pixels', 0)} 像素 · "
                        f"{result.get('method', 'skip')}"))
        if on_ready:
            on_ready(revision, result.get("method") != "skip")

    finish_playback()
