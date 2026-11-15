"""Rev C device-side storage and media player commands.

This module intentionally sits above the USB transport. It does not own or
reopen the USB device; every operation reuses the active Display session.
"""
from __future__ import annotations

import os
import logging
import re
import time
from pathlib import Path
from typing import Callable

import usb.core

from device import IN_BUFFER_SIZE, IN_EP
from device_protocol import pad_command, path_command, upload_command

VIDEO_DIR_INTERNAL = "/mnt/SDCARD/video/"
VIDEO_DIR_COMPAT = "/root/video/"
IMAGE_DIR_INTERNAL = "/mnt/SDCARD/img/"
IMAGE_DIR_COMPAT = "/root/img/"
log = logging.getLogger(__name__)
FILE_SIZE_REPLY = re.compile(
    rb"(?:^|[\x00\r\n])[ \t]*(?P<value>[0-9]+|file_not_found|not found|no such file|not exist|none)"
    rb"[ \t]*(?=[\x00\r\n]|$)", re.IGNORECASE
)


class DeviceMedia:
    def __init__(self, display):
        self.display = display

    @staticmethod
    def _validate_path(path: str) -> str:
        if not isinstance(path, str) or not path.startswith((
            "/root/", "/mnt/SDCARD/", "/tmp/video/"
        )) or ".." in Path(path).parts:
            raise ValueError("不允许访问该设备路径")
        path.encode("ascii")
        if len(path.encode("ascii")) > 255:
            raise ValueError("设备路径超过 255 字节")
        return path

    def _read_reply(self, expected: tuple[bytes, ...] = (), timeout: float = 2.5,
                    max_bytes: int = 32768, *, pattern=None,
                    context: str = "device media", quiet_ms: int = 0) -> bytes:
        if pattern is None and expected:
            pattern = re.compile(b"|".join(re.escape(token) for token in expected), re.IGNORECASE)
        if pattern is not None:
            # A STOP/overlay acknowledgement may arrive before this command's
            # actual reply. Keep reading to the deadline instead of returning
            # the first packet, or stopping on a short timeout after that packet.
            return self.display._read_matching_reply(
                context, pattern, timeout_ms=max(1,int(timeout*1000)),
                size=1024, max_bytes=max_bytes, quiet_ms=quiet_ms,
            ) or b""
        deadline = time.monotonic() + timeout
        result = bytearray()
        while time.monotonic() < deadline and len(result) < max_bytes:
            remaining = max(1, int((deadline - time.monotonic()) * 1000))
            try:
                chunk = bytes(self.display.dev.read(
                    IN_EP, IN_BUFFER_SIZE, timeout=min(300, remaining)
                ))
            except usb.core.USBTimeoutError:
                if result:
                    break
                continue
            except usb.core.USBError:
                self.display.needs_reset = True
                raise
            if not chunk:
                break
            result.extend(chunk)
            # Untyped optional/list replies retain the bounded legacy reader.
            if len(chunk) < IN_BUFFER_SIZE:
                break
        return bytes(result)

    @staticmethod
    def _text(reply: bytes) -> str:
        return reply.replace(b"\x00", b"").decode("ascii", "replace").strip()

    def storage_info(self) -> dict:
        with self.display.io_lock:
            self.display._command(bytes.fromhex("64 EF 69 00 00 00 01"))
            reply = self._read_reply(timeout=2.0)
        return {"raw": self._text(reply)}

    def list_files(self, directory: str) -> dict:
        directory = self._validate_path(directory)
        with self.display.io_lock:
            self.display._command(path_command(0x65, directory))
            reply = self._read_reply(timeout=2.5, max_bytes=65536)
        text = self._text(reply)
        dirs_match = re.search(r"dir:(.*?)file:", text, re.IGNORECASE | re.DOTALL)
        files_match = re.search(r"file:(.*)$", text, re.IGNORECASE | re.DOTALL)
        directories = [x for x in (dirs_match.group(1).split("/") if dirs_match else []) if x]
        files = [x for x in (files_match.group(1).split("/") if files_match else []) if x]
        return {"path": directory, "directories": directories, "files": files,
                "raw": text}

    def get_file_size(self, path: str) -> int:
        path = self._validate_path(path)
        with self.display.io_lock:
            self.display._command(path_command(0x6E, path))
            reply = self._read_reply(timeout=2.0, pattern=FILE_SIZE_REPLY,
                                     context=f"file size {path}", quiet_ms=30)
        match = FILE_SIZE_REPLY.search(reply)
        if match is None:
            raise RuntimeError("设备在 2 秒内未返回有效文件大小；其他回复已记录日志")
        value = match.group("value")
        log.info("Device file size %s: %s", path, self._text(value))
        # Accept a complete decimal response only: never interpret digits in
        # needReSend/renderCnt, a path, or another command's acknowledgement.
        return int(value) if value.isdigit() else 0

    def delete_file(self, path: str) -> None:
        path = self._validate_path(path)
        with self.display.io_lock:
            self.display._command(path_command(0x66, path))
            # Delete replies vary by firmware; drain a bounded optional reply.
            self._read_reply(timeout=0.35)

    def upload_file(self, local_path: Path, remote_path: str,
                    progress: Callable[[int, int], None] | None = None) -> dict:
        """Upload a file as 249-byte payload blocks with a 0x00 delimiter.

        Uploads are deliberately not cancellable mid-stream: the device treats
        the declared file size as authoritative and would parse a following
        command as the remainder of the file if the host stopped early.
        """
        local_path = Path(local_path)
        remote_path = self._validate_path(remote_path)
        if not local_path.is_file() or local_path.stat().st_size <= 0:
            raise ValueError("待上传文件不存在或为空")
        total = local_path.stat().st_size
        if total > 0xFFFFFFFF:
            raise ValueError("文件超过设备协议支持的 4 GiB 上限")

        sent = 0
        log.info("Uploading device media %s -> %s (%d bytes)", local_path.name, remote_path, total)
        last_report = 0
        batch = bytearray()
        with self.display.io_lock:
            self.display._drain_pending()
            self.display._command(upload_command(remote_path, total))
            # Any interruption after CREATE leaves the device consuming file
            # bytes; recovery must reset that parser before issuing commands.
            self.display.needs_reset = True
            created = self._read_reply((b"create_success", b"create success"), timeout=3.0,
                                       context="create upload file")
            if b"create_success" not in created.lower() and b"create success" not in created.lower():
                raise RuntimeError(
                    "设备没有确认创建上传文件；为避免协议错位，已停止发送。"
                    f"设备响应：{self._text(created)[:120] or '无响应'}"
                )

            with local_path.open("rb") as source:
                while True:
                    chunk = source.read(249)
                    if not chunk:
                        break
                    batch.extend(chunk)
                    batch.extend(b"\x00" * (250 - len(chunk)))
                    sent += len(chunk)
                    if len(batch) >= 16000:
                        self.display._write(bytes(batch))
                        batch.clear()
                    if progress and (sent - last_report >= 256 * 1024 or sent == total):
                        progress(sent, total)
                        last_report = sent
            if batch:
                self.display._write(bytes(batch))

            completed = self._read_reply(
                (b"file_rev_done", b"file rev done"), timeout=4.0, max_bytes=65536,
                context="upload completion"
            )
            # Some ROMs omit or truncate the textual completion message. Drain
            # any late acknowledgement before issuing the authoritative size query.
            self.display._drain_pending()
            actual = self.get_file_size(remote_path)
            if actual != total:
                raise RuntimeError(
                    f"设备文件校验失败：本地 {total} 字节，设备 {actual} 字节。"
                    "如果刚才中断过上传，请删除残留文件后重试。"
                )
            self.display.needs_reset = False
            log.info("Device media upload verified: %s (%d bytes)", remote_path, actual)
            if progress:
                progress(total, total)
        return {
            "path": remote_path, "size": total,
            "ack": self._text(completed)[:160], "verified": True,
        }

    def play_video(self, path: str) -> dict:
        path = self._validate_path(path)
        size = self.get_file_size(path)
        if size <= 0:
            raise FileNotFoundError(f"屏幕内部找不到视频：{path}")
        with self.display.io_lock:
            self.display._command(path_command(0x78, path, sub=1))
            reply = self._read_reply((b"play_video_success", b"play video success"), timeout=4.0,
                                     context="video playback")
        text = self._text(reply)
        log.info("Device loop playback %s: %s", path, text[:160] or "no response")
        if b"play_video_success" not in reply.lower() and b"play video success" not in reply.lower():
            raise RuntimeError(f"设备未确认开始播放：{text[:160] or '无响应'}")
        return {"path": path, "size": size, "response": text[:160]}

    def stop_video(self) -> None:
        with self.display.io_lock:
            self.display._command(bytes.fromhex("79 EF 69 00 00 00 01"))
            self.display._command(bytes.fromhex("96 EF 69 00 00 00 01"))
            reply = self._read_reply((b"media_stop", b"media stop"), timeout=2.0,
                                     context="media stop")
            if b"media_stop" not in reply.lower() and b"media stop" not in reply.lower():
                raise RuntimeError("设备在 2 秒内未确认停止播放；其他回复已记录日志")
