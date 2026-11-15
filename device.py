"""Turing 2.1" Revision C / 1d6b:0121 direct libusb transport.

The first session performs a single HELLO. Subsequent drawings reuse the claimed
interface and never send another HELLO unless explicitly disconnected.
"""
from __future__ import annotations

import logging
import time
import hashlib
import re
import ctypes.util
import threading
from runtime_paths import usb_library
from PIL import ImageChops, ImageDraw
from device_protocol import frame_stream
from video_overlay import build_overlay_packet, visibility_list, _device_image

import usb.backend.libusb1
import usb.core
import usb.util

log = logging.getLogger(__name__)

VID, PID, SERIAL = 0x1D6B, 0x0121, "20080411"
OUT_EP, IN_EP, DATA_IF = 0x01, 0x81, 1
FRAME = 250
CHUNK_SIZE = 250 * 64
# Unlike 64/1024-byte reads, this is comfortably larger than a full USB
# high-speed packet and typical device status responses.
IN_BUFFER_SIZE = 16384

HELLO = bytes.fromhex("01 EF 69 00 00 00 01 00 00 00 C5 D3")
PRE_UPDATE = bytes.fromhex("86 EF 69 00 00 00 01")
BITMAP_2INCH = bytes.fromhex("C8 EF 69 00 0E 10 0E 10")
QUERY = bytes.fromhex("CF EF 69 00 00 00 01")
UPDATE = bytes.fromhex("CC EF 69 00")
STOP_VIDEO = bytes.fromhex("79 EF 69 00 00 00 01")
STOP_MEDIA = bytes.fromhex("96 EF 69 00 00 00 01")
OPTIONS = bytes.fromhex("7D EF 69 00 00 00 05 00 00 00 2D")
HELLO_REPLY = re.compile(rb"chs_[A-Za-z0-9]+\.dev[0-9]+_rom[0-9]+\.[0-9]{2,}")
OVERLAY_STATUS_REPLY = re.compile(rb"needReSend:([01])(?=\||[\x00\r\n]|$)", re.IGNORECASE)


def available() -> bool:
    try:
        return usb.core.find(idVendor=VID, idProduct=PID,
                             backend=libusb_backend()) is not None
    except (usb.core.USBError, ValueError):
        return False


def libusb_backend():
    backend = usb.backend.libusb1.get_backend(find_library=usb_library)
    if backend is None:
        backend = usb.backend.libusb1.get_backend(find_library=ctypes.util.find_library)
    return backend


def probe_device():
    """Read USB identity without claiming the interface or writing commands."""
    result = {"present": False, "matched": False, "usb_id": "1d6b:0121",
              "serial": None, "bus": None, "address": None, "error": ""}
    dev = None
    try:
        backend = libusb_backend()
        if backend is None:
            raise RuntimeError("libusb 初始化失败")
        dev = usb.core.find(idVendor=VID, idProduct=PID, backend=backend)
        if dev is None:
            result["error"] = "未发现屏幕 USB 设备"
            return result
        result.update(present=True, bus=dev.bus, address=dev.address)
        result["serial"] = dev.serial_number
        result["matched"] = result["serial"] == SERIAL
        if not result["matched"]:
            result["error"] = "USB 序列号不符，拒绝操作"
    except Exception as exc:
        result["error"] = str(exc)
    finally:
        if dev is not None:
            usb.util.dispose_resources(dev)
    return result


def wake():
    if available():
        return
    try:
        import serial
        path = "/dev/serial/by-id/usb-Turing_UsbMonitor_CT21INCH-if00"
        with serial.Serial(path, 115200, timeout=1, rtscts=True) as port:
            port.dtr = True
            port.rts = True
            time.sleep(0.5)
    except Exception as exc:
        raise RuntimeError(f"屏幕唤醒失败：{exc}") from exc

    for _ in range(20):
        if available():
            return
        time.sleep(0.25)
    raise RuntimeError("唤醒后未检测到屏幕 1d6b:0121")


def frame_bytes(image) -> bytes:
    """480x480 RGB -> Revision C full-frame BGRA with 249-byte delimiters."""
    if image.size != (480, 480):
        raise ValueError("必须使用 480×480 的图像")
    native = image.convert("RGBA").rotate(90, expand=True).tobytes("raw", "BGRA")
    packed = b"\x00".join(native[i:i + 249] for i in range(0, len(native), 249))
    return packed + b"\x00" * (-len(packed) % FRAME)


class Display:
    def __init__(self):
        self.dev = None
        self.claimed = False
        self.detached = []
        self.display_id = None
        self.last_frame = None
        self.mount_ccw = None
        self.update_count = 0
        self.bytes_sent = 0
        self.last_packet_hash = ""
        self.last_mode = None
        self.needs_reset = False
        self.io_lock = threading.RLock()
        self.video_overlay = None
        self.video_overlay_mount = None
        self.video_overlay_sequence = 0
        self.video_overlay_empty_replies = 0
        self.video_overlay_visible = 0
        self.video_overlay_regions = 0

    @property
    def connected(self):
        return self.dev is not None and self.claimed and self.display_id is not None

    def connect(self, preserve_media=False):
        if self.connected:
            return self
        wake()
        backend = libusb_backend()
        if backend is None:
            raise RuntimeError("libusb 未安装或初始化失败")
        dev = usb.core.find(idVendor=VID, idProduct=PID, backend=backend)
        if dev is None:
            raise RuntimeError("未检测到图灵智显 USB 显示设备")
        self.dev = dev
        try:
            if dev.serial_number != SERIAL:
                raise RuntimeError("USB 序列号不符，已拒绝操作该设备")
            for index in (0, DATA_IF):
                if dev.is_kernel_driver_active(index):
                    dev.detach_kernel_driver(index)
                    self.detached.append(index)
                    log.info("Detached kernel driver interface %d", index)
            usb.util.claim_interface(dev, DATA_IF)
            self.claimed = True

            # On a reopened interface the device might still have status
            # responses from the previous image. Drain a bounded amount
            # before requesting a new 23-byte HELLO response.
            self._drain_pending()

            self._command(HELLO)
            try:
                rx = self._read_matching_reply("HELLO", HELLO_REPLY,
                                               timeout_ms=2000, size=IN_BUFFER_SIZE)
            except usb.core.USBError as exc:
                raise RuntimeError(f"HELLO 未收到响应：{exc}") from exc
            if rx is None:
                self.needs_reset = True
                raise RuntimeError("HELLO 在 2 秒内未收到有效设备 ID；其他回复已记录日志")
            self.display_id = HELLO_REPLY.search(rx).group(0).decode("ascii", "replace")
            log.info("Display ID %s", self.display_id)

            if not preserve_media:
                self._command(STOP_VIDEO)
                self._command(STOP_MEDIA)
                self._read_status("post STOP_MEDIA")
            self._command(bytes.fromhex("7B EF 69 00 00 00 01 00 00 00") + bytes([160]))
            self._command(OPTIONS + bytes([0, 0, 0, 0]))
            log.info("USB display initialized; session will be reused")
            self.needs_reset = False
            return self
        except Exception:
            self.close()
            raise

    def _drain_pending(self, max_reads=8):
        for _ in range(max_reads):
            try:
                data = bytes(self.dev.read(IN_EP, IN_BUFFER_SIZE, timeout=50))
                if not data:
                    break
                log.info("Cleared %d stale RX bytes before HELLO", len(data))
            except usb.core.USBTimeoutError:
                break
            except usb.core.USBError as exc:
                # Overflow here means the stream is not clean enough to continue
                # safely; don't silently assume the next HELLO will be correct.
                raise RuntimeError(f"USB 接收队列清理失败：{exc}") from exc

    def _write(self, data: bytes):
        for offset in range(0, len(data), CHUNK_SIZE):
            chunk = data[offset:offset + CHUNK_SIZE]
            try:
                sent = self.dev.write(OUT_EP, chunk, timeout=6000)
            except Exception:
                self.needs_reset = True
                raise
            self.bytes_sent += sent
            if sent != len(chunk):
                self.needs_reset = True
                raise RuntimeError(f"USB 未写完整：{sent}/{len(chunk)}")

    def _command(self, data: bytes, pad_byte: int = 0):
        self._write(data + bytes([pad_byte]) * (-len(data) % FRAME))

    def _read_matching_reply(self, context, pattern, *, timeout_ms, size, max_bytes=32768,
                             quiet_ms=0):
        """Read until this request's reply arrives, within fixed time/byte bounds.

        Firmware 1.89 on the user's screen emits seq_png_init_sucess after
        overlay initialization, despite the reference listing no reply. That
        acknowledgement (or a late status during HELLO) must not be mistaken
        for the next command's reply. Accumulate fragments and inspect content.
        """
        deadline = time.monotonic() + timeout_ms / 1000
        received = bytearray()
        seen = set()
        candidate = None
        for _ in range(64):
            remaining = int((deadline-time.monotonic()) * 1000)
            if remaining <= 0 or len(received) >= max_bytes:
                break
            try:
                read_timeout = min(200, remaining)
                if candidate is not None and quiet_ms:
                    read_timeout = min(read_timeout, quiet_ms)
                chunk = bytes(self.dev.read(IN_EP, size, timeout=read_timeout))
            except usb.core.USBTimeoutError:
                if candidate is not None:
                    return bytes(received)
                continue
            except usb.core.USBError:
                self.needs_reset = True
                raise
            if not chunk:
                if candidate is not None:
                    return bytes(received)
                continue
            received.extend(chunk[:max_bytes-len(received)])
            candidate = pattern.search(received)
            if candidate is not None:
                boundary = received[candidate.end():candidate.end()+1]
                if not quiet_ms or boundary in (b"\x00",b"\r",b"\n"):
                    return bytes(received)
                # A bare decimal has no terminator on some firmware. Wait for
                # a short quiet gap so a split number is not accepted early.
                continue
            preview = chunk.replace(b"\x00", b"").decode("ascii", "replace").strip()[:160]
            if preview and preview not in seen:
                log.info("Waiting for %s; received another device reply: %s", context, preview)
                seen.add(preview)
        if received:
            preview = bytes(received).replace(b"\x00", b"").decode("ascii", "replace").strip()[:240]
            log.warning("No matching %s reply within %dms; received: %s", context, timeout_ms, preview)
        else:
            log.warning("No %s reply within %dms", context, timeout_ms)
        return None

    def _read_status(self, context: str, *, size=IN_BUFFER_SIZE):
        try:
            rx = bytes(self.dev.read(IN_EP, size, timeout=700))
            log.debug("%s status: %d bytes", context, len(rx))
            return rx
        except usb.core.USBTimeoutError:
            # Some ROMs produce no status at this stage.
            log.debug("%s status: timeout (optional)", context)
            return b""
        except usb.core.USBError:
            # Overflow / STALL must be visible to the caller so the session
            # is closed; otherwise the following command could desync.
            log.exception("%s status read failed", context)
            self.needs_reset = True
            raise

    def connection_details(self):
        return {"present": self.dev is not None, "matched": self.connected,
                "usb_id": "1d6b:0121", "serial": SERIAL if self.connected else None,
                "bus": self.dev.bus if self.dev else None,
                "address": self.dev.address if self.dev else None,
                "display_id": self.display_id, "handshake": self.connected, "error": ""}

    def reset_usb(self, stop_event):
        """Reset only the verified display device, then wait for rediscovery.

        Run in the USB worker, with no simultaneous bulk transfers. A reset
        handle may become invalid when Linux re-enumerates the same device.
        """
        self.close()
        if stop_event.is_set():
            raise InterruptedError("USB 复位已取消")
        backend = libusb_backend()
        if backend is None:
            raise RuntimeError("libusb 初始化失败，无法复位")
        dev = usb.core.find(idVendor=VID, idProduct=PID, backend=backend)
        if dev is None:
            raise RuntimeError("未发现目标屏幕，等待重新连接后再复位")
        try:
            if dev.serial_number != SERIAL:
                raise RuntimeError("USB 序列号不符，已拒绝复位")
            self.needs_reset = True
            log.warning("Resetting verified display USB %04x:%04x serial=%s bus=%s address=%s",
                        VID, PID, SERIAL, dev.bus, dev.address)
            try:
                dev.reset()
            except usb.core.USBError as exc:
                if getattr(exc, "backend_error_code", None) not in (-4, -5):
                    raise
                log.info("Reset invalidated the old USB handle; waiting for rediscovery")
        finally:
            usb.util.dispose_resources(dev)
        if stop_event.wait(.5):
            raise InterruptedError("USB 复位已取消")
        deadline = time.monotonic() + 10
        while not stop_event.is_set():
            found = usb.core.find(idVendor=VID, idProduct=PID, backend=backend)
            if found is not None:
                try:
                    if found.serial_number == SERIAL:
                        log.info("Verified display rediscovered after USB reset: bus=%s address=%s",
                                 found.bus, found.address)
                        self.needs_reset = False
                        self.update_count = 0
                        return
                except usb.core.USBError:
                    log.debug("USB descriptors not ready after reset; retrying discovery",exc_info=True)
                finally:
                    usb.util.dispose_resources(found)
            if time.monotonic() >= deadline:
                raise RuntimeError("USB 复位后 10 秒内未重新发现目标屏幕")
            if stop_event.wait(.25):
                break
        raise InterruptedError("USB 复位已取消")

    def _full(self, image):
        payload = frame_bytes(image)
        self._command(PRE_UPDATE)
        self._write(b"\x2c" * FRAME)
        self._command(BITMAP_2INCH)
        self._write(payload)
        self._read_status("full")
        self._command(QUERY)
        self._read_status("full query")

    def initialize_video_overlay(self, mount_ccw):
        """Reset the local baseline; the next update establishes it with CA/D0."""
        if mount_ccw not in (0, 90, 180, 270):
            raise ValueError("安装角度无效")
        with self.io_lock:
            self.video_overlay = None
            self.video_overlay_mount = mount_ccw
            self.video_overlay_sequence = 0
            self.video_overlay_empty_replies = 0
            self.video_overlay_visible = 0
            self.video_overlay_regions = 0
            if self.dev is not None:
                self._drain_pending()

    def check_alive(self):
        """Standard USB GET_STATUS; does not touch the device's media player."""
        with self.io_lock:
            if not self.connected:
                raise RuntimeError("屏幕 USB 会话已断开")
            self.dev.ctrl_transfer(0x80, 0, 0, 0, 2, timeout=1000)

    def update_video_overlay(self, image, mount_ccw):
        """Establish a CA/D0 baseline, then send CC overlay diffs with POSLEN.

        Match QUERY_STATUS by content; firmware may emit an asynchronous
        initialization acknowledgement first. The video background is never
        decoded or sent by this method. Reference:
        bezel-evo protocol-turing-rev-c.md §§9.2, 13.5; pixel-formats.md §§4–8.
        """
        if image.size != (480, 480) or mount_ccw not in (0, 90, 180, 270):
            raise ValueError("透明图层尺寸或安装角度无效")
        current = image.convert("RGBA")
        with self.io_lock:
            started = time.perf_counter()
            before = self.bytes_sent
            transfer_seconds = wait_seconds = 0.0

            def write(data, *, command=False):
                nonlocal transfer_seconds
                since = time.perf_counter()
                try:
                    if command:
                        self._command(data)
                    else:
                        self._write(data)
                finally:
                    transfer_seconds += time.perf_counter() - since

            def result(method, changed, pixel_bytes=0, poslen_bytes=0):
                elapsed = time.perf_counter() - started
                return {"method": method, "bytes": self.bytes_sent-before,
                        "changed_pixels": changed, "visible_pixels": self.video_overlay_visible,
                        "regions": self.video_overlay_regions, "seconds": elapsed,
                        "pixel_bytes": pixel_bytes, "poslen_bytes": poslen_bytes,
                        "transfer_seconds": transfer_seconds, "wait_seconds": wait_seconds,
                        "encode_seconds": max(0.0, elapsed-transfer_seconds-wait_seconds)}

            try:
                # Match this query, skipping any delayed overlay acknowledgement.
                # A 1024-byte buffer matches the status response / USB packets.
                write(QUERY, command=True)
                since = time.perf_counter()
                reply = self._read_matching_reply("video overlay query", OVERLAY_STATUS_REPLY,
                                                  timeout_ms=700, size=1024)
                wait_seconds += time.perf_counter() - since
                need_full = (self.video_overlay is None or self.video_overlay_mount != mount_ccw)
                if reply is not None:
                    match = OVERLAY_STATUS_REPLY.search(reply)
                    self.video_overlay_empty_replies = 0
                    need_full = need_full or match.group(1) == b"1"
                else:
                    self.video_overlay_empty_replies += 1
                    if self.video_overlay_empty_replies > 3:
                        self.needs_reset = True
                        raise RuntimeError("连续 4 次未收到叠加层状态响应")

                if need_full:
                    native = _device_image(current, mount_ccw)
                    colours = native.tobytes("raw", "BGRA")
                    mask, visible, regions = visibility_list(native)
                    write(b"\xca\xef\x69" + len(colours).to_bytes(4, "big") + b"\x00" * 3,
                          command=True)
                    since = time.perf_counter()
                    time.sleep(.1)  # Allocate the full overlay receive buffer once.
                    wait_seconds += time.perf_counter() - since
                    write(frame_stream(colours))
                    # Vendor sends no D0 for an empty POSLEN. The full BGRA
                    # frame already writes transparent pixels in that case.
                    if mask:
                        body = mask + b"\xef\x69"
                        write(b"\xd0\xef\x69" + len(body).to_bytes(4, "big") + b"\x00" * 3,
                              command=True)
                        write(frame_stream(body))
                    self.video_overlay = current.copy()
                    self.video_overlay_mount = mount_ccw
                    self.video_overlay_sequence = 0
                    self.video_overlay_visible, self.video_overlay_regions = visible, regions
                    log.info("Video overlay baseline sent; visible=%d regions=%d bytes=%d",
                             visible, regions, self.bytes_sent-before)
                    return result("overlay-full", 480*480, len(colours), len(mask))

                packet = build_overlay_packet(current, self.video_overlay, mount_ccw,
                                              self.video_overlay_sequence)
                if packet is None:
                    return result("skip", 0)
                write(packet.header, command=True)
                write(packet.payload)
                # Commit only after the complete transaction was written. Any
                # failure discards the baseline; recovery must rebuild it.
                self.video_overlay = packet.next_frame
                self.video_overlay_mount = mount_ccw
                self.video_overlay_sequence = (self.video_overlay_sequence+1) & 0xFFFFFFFF
                self.video_overlay_visible = packet.visible_pixels
                self.video_overlay_regions = packet.visible_segments
                return result("overlay-delta", packet.changed_pixels, packet.pixel_bytes, packet.poslen_bytes)
            except Exception:
                self.video_overlay = None
                self.video_overlay_sequence = 0
                raise

    def _partial(self, image, box):
        """Rev C UPDATE_BITMAP, 2.1-inch firmware uses BGR (3 bytes/pixel).

        Follows the memory addressing of the Rev C upstream implementation:
        rotate rect 90 degrees CCW, then write rows at memory address
        (x0 + row) * 480 + y0 with the width encoded in 2 big-endian bytes.
        """
        x, y, x2, y2 = box
        crop = image.crop(box)
        rotated = crop.rotate(90, expand=True).convert("RGB")
        x0 = 480 - x - rotated.height
        y0 = y
        raw = bytearray()
        bgr = rotated.tobytes("raw", "BGR")
        stride = rotated.width * 3
        for row in range(rotated.height):
            addr = (x0 + row) * 480 + y0
            raw.extend(addr.to_bytes(3, "big"))
            raw.extend(rotated.width.to_bytes(2, "big"))
            raw.extend(bgr[row * stride:(row + 1) * stride])
        # Last 2 bytes EF69 are counted in the big-endian length.
        size = (len(raw) + 2).to_bytes(3, "big")
        header = (UPDATE + size + b"\x00" * 3 +
                  self.update_count.to_bytes(4, "big"))
        self.update_count = (self.update_count + 1) & 0xFFFFFFFF
        # Insert 0x00 separator after every 249 payload bytes, like upstream.
        if len(raw) > 250:
            raw = bytearray(b"\x00".join(
                raw[i:i + 249] for i in range(0, len(raw), 249)
            ))
        raw.extend(b"\xef\x69")
        # Useful diagnostic: verify the raw update payload changes when
        # the image changes, not a reused pre-rendered PNG.
        self.last_packet_hash = hashlib.sha256(raw).hexdigest()[:12]
        log.info("UPDATE_BITMAP area=%s payload=%d sha256=%s",
                 box, len(raw), self.last_packet_hash)
        self._command(header)
        self._command(raw)
        self._command(QUERY)
        self._read_status("incremental query")

    @staticmethod
    def _dirty_regions(previous, current):
        """Merge tile runs into bounded rectangles, so distant sensor labels
        can update independently without overwriting a large middle region."""
        if previous is None:
            return [(0, 0, 480, 480)]
        difference = ImageChops.difference(previous, current)
        if not difference.getbbox():
            return []
        tile = 40
        rows = []
        for ty in range(0, 480, tile):
            runs = []
            tx = 0
            while tx < 480:
                if difference.crop((tx, ty, tx + tile, ty + tile)).getbbox():
                    start = tx
                    tx += tile
                    while tx < 480 and difference.crop(
                        (tx, ty, tx + tile, ty + tile)
                    ).getbbox():
                        tx += tile
                    runs.append((start, tx))
                else:
                    tx += tile
            rows.append((ty, runs))
        regions = []
        active = {}
        for ty, runs in rows:
            next_active = {}
            for pair in runs:
                if pair in active:
                    ix = active[pair]
                    rect = regions[ix]
                    regions[ix] = (rect[0], rect[1], rect[2], ty + tile)
                else:
                    ix = len(regions)
                    regions.append((pair[0], ty, pair[1], ty + tile))
                next_active[pair] = ix
            active = next_active
        return regions

    @staticmethod
    def estimate_regions(previous, current):
        """Only estimate changed areas for the *partial* protocol.

        A large/fragmented change is merged into one 0xCC rectangle, never
        substituted with full-frame 0xC8. Both protocols remain explicit.
        """
        regions = Display._dirty_regions(previous, current)
        if not regions:
            return [], 0
        if len(regions) > 12:
            bbox = ImageChops.difference(previous, current).getbbox() if previous else (
                0, 0, 480, 480)
            # Align to 40-pixel grid to keep the 0xCC row writer simple.
            x0, y0, x1, y1 = bbox
            regions = [(
                (x0 // 40) * 40, (y0 // 40) * 40,
                min(480, ((x1 + 39) // 40) * 40),
                min(480, ((y1 + 39) // 40) * 40),
            )]
        estimate = 0
        for x0, y0, x1, y1 in regions:
            width, height = x1 - x0, y1 - y0
            data = width * height * 3 + width * 5 + 2
            data += data // 249 + 750
            estimate += data
        return regions, estimate

    def draw(self, image, mount_ccw: int = 90, mode="full",
             skip_unchanged=True):
        """Strict transport: full means ONLY 0xC8, partial means ONLY 0xCC.

        A first partial frame sends the whole 480x480 image via 0xCC so that
        there is no hidden initial full-mode transmission. If unsupported,
        propagate the USB/firmware error instead of silently falling back.
        """
        if not self.connected:
            raise RuntimeError("USB 会话尚未建立")
        if mount_ccw not in (0, 90, 180, 270):
            raise ValueError("安装角度无效")
        if mode not in ("full", "partial"):
            raise ValueError("USB 模式仅允许 full 或 partial")
        corrected = image.convert("RGB").rotate(-mount_ccw, expand=True)
        previous = (self.last_frame if self.mount_ccw == mount_ccw
                    and self.last_mode == mode else None)
        frame_hash = hashlib.sha256(corrected.tobytes()).hexdigest()[:12]
        # We report a count of changed *pixels*, not bytes.
        changed_pixels = None
        if previous is not None:
            diff = ImageChops.difference(previous, corrected)
            red, green, blue = diff.split()
            union = ImageChops.lighter(ImageChops.lighter(red, green), blue)
            changed_pixels = 480 * 480 - union.histogram()[0]
        started = time.perf_counter()
        before = self.bytes_sent

        if skip_unchanged and changed_pixels == 0:
            return {
                "method": "skip", "mode": mode,
                "bytes": 0, "seconds": 0.0, "regions": 0,
                "estimated_bytes": 0, "changed_pixels": 0,
                "frame_hash": frame_hash,
            }

        if mode == "full":
            regions = [(0, 0, 480, 480)]
            estimate = 926500
        else:
            if previous is None:
                regions, estimate = [(0, 0, 480, 480)], 700000
            else:
                regions, estimate = self.estimate_regions(previous, corrected)
            if not regions and not skip_unchanged:
                regions, estimate = [(0, 0, 480, 480)], 700000

        try:
            if mode == "full":
                self._full(corrected)
            else:
                for box in regions:
                    self._partial(corrected, box)
        except Exception:
            self.last_frame = None
            self.mount_ccw = None
            self.last_mode = None
            raise

        self.last_frame = corrected.copy()
        self.mount_ccw = mount_ccw
        self.last_mode = mode
        return {
            "method": mode, "mode": mode,
            "bytes": self.bytes_sent - before,
            "seconds": round(time.perf_counter() - started, 4),
            "regions": len(regions),
            "estimated_bytes": estimate,
            "changed_pixels": changed_pixels,
            "frame_hash": frame_hash,
            "packet_hash": self.last_packet_hash if mode == "partial" else "",
        }

    def benchmark(self, image, mount_ccw=90, mode="full"):
        """Real transport-only benchmark: show TEST patch and restore it.

        Never mix the 0xCC protocol with 0xC8 inside one chosen mode.
        """
        if mode not in ("full", "partial"):
            raise ValueError("测速模式无效")
        if not self.connected:
            raise RuntimeError("USB 会话尚未建立")

        measurements = []
        try:
            base_result = self.draw(image, mount_ccw=mount_ccw,
                                    mode=mode, skip_unchanged=False)
            base = image.convert("RGB")
            for size in (160, 240):
                offset = (480 - size) // 2
                box = (offset, offset, offset+size, offset+size)
                marked = base.copy()
                painter = ImageDraw.Draw(marked)
                painter.rectangle(box,fill=(23,131,156))
                painter.rectangle((offset+8,offset+8,offset+size-8,offset+size-8),
                                  outline=(251,225,155),width=5)
                painter.text((offset+15, offset+size//2-10),
                             f"USB {size} TEST", fill=(255,255,255))
                for stage, frame in (("show", marked), ("restore", base)):
                    result = self.draw(frame,mount_ccw=mount_ccw,mode=mode,
                                       skip_unchanged=False)
                    elapsed = max(0.001,result["seconds"])
                    measurements.append({
                        "size":size,"stage":stage,
                        "bytes":result["bytes"],
                        "seconds":result["seconds"],
                        "bps":int(result["bytes"]/elapsed),
                        "frame_hash":result["frame_hash"],
                        "mode":mode,
                    })
        except Exception:
            self.last_frame = None
            self.last_mode = None
            raise
        # Include all USB command+status overhead; conservative min sample
        samples_bps=[int(x["bytes"]/max(0.001,x["seconds"])) for x in measurements]
        speed = min(samples_bps+[int(base_result["bytes"]/max(0.001,base_result["seconds"]))])
        return {
            "mode":mode, "bps":speed,
            "initial":base_result,"samples":measurements,
            "measured_at":time.time(),
            "uses_visible_changes":True,
            "note":f"仅测试 {mode} 协议：测试区域会变色并恢复原图，请确认实际屏幕发生变化。",
        }


    def close(self):
        self.video_overlay = None
        self.video_overlay_mount = None
        self.video_overlay_sequence = 0
        self.video_overlay_empty_replies = 0
        self.video_overlay_visible = 0
        self.video_overlay_regions = 0
        dev = self.dev
        if dev is None:
            return
        self.display_id = None
        self.last_frame = None
        self.last_mode = None
        if self.claimed:
            try:
                usb.util.release_interface(dev, DATA_IF)
            except usb.core.USBError as exc:
                log.warning("USB interface release error: %s", exc)
            self.claimed = False
        for interface in reversed(self.detached):
            try:
                dev.attach_kernel_driver(interface)
            except usb.core.USBError as exc:
                log.warning("Kernel driver attach error on if%d: %s", interface, exc)
        self.detached.clear()
        try:
            usb.util.dispose_resources(dev)
        except Exception:
            log.exception("USB cleanup failed")
        self.dev = None

    def __enter__(self):
        return self.connect()

    def __exit__(self, exc_type, exc, tb):
        self.close()
        return False
