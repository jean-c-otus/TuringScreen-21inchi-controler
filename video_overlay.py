"""Transparent Rev C video-overlay packet encoder for the 2.1-inch panel."""
from __future__ import annotations

from dataclasses import dataclass
from PIL import Image

from device_protocol import MARKER, frame_stream, overlay_update_command

WIDTH = 480
HEIGHT = 480
MAX_RUN = 65000


@dataclass
class OverlayPacket:
    header: bytes
    payload: bytes
    next_frame: Image.Image
    changed_pixels: int
    visible_pixels: int
    visible_segments: int
    payload_bytes: int
    pixel_bytes: int
    poslen_bytes: int


def _device_image(image: Image.Image, mount_ccw: int) -> Image.Image:
    if image.size != (WIDTH, HEIGHT):
        raise ValueError("透明叠加图层必须是 480×480")
    if mount_ccw not in (0, 90, 180, 270):
        raise ValueError("安装角度无效")
    # Match the Rev C full-frame path: compensate the physical mount, then
    # rotate into the panel's native framebuffer memory orientation.
    return image.convert("RGBA").rotate(-mount_ccw, expand=True).rotate(90, expand=True)


def visibility_list(native: Image.Image):
    """POSLEN records in native linear coordinates, alpha > 15 (not a diff).

    Reference: bezel-evo docs/reverse-engineering/pixel-formats.md §8.
    Each record is BE24(start) + BE16(count); runs can cross row boundaries.
    """
    alpha = native.getchannel("A").tobytes()
    data = bytearray()
    visible = segments = 0
    index = 0
    while index < len(alpha):
        if alpha[index] <= 15:
            index += 1
            continue
        start = index
        while index < len(alpha) and alpha[index] > 15 and index-start < MAX_RUN:
            index += 1
        count = index-start
        data.extend(start.to_bytes(3, "big"))
        data.extend(count.to_bytes(2, "big"))
        visible += count
        segments += 1
    # The vendor POSLEN encoder leaves an extra zero record for a run ending
    # at the final pixel. Keep that documented wire convention, without its
    # destructive mutation of an input pixel at a 65,000-pixel boundary.
    if alpha and alpha[-1] > 15:
        data.extend(b"\x00" * 5)
    return bytes(data), visible, segments


def _pixel_bytes(rgba: bytes, offset: int) -> bytes:
    red, green, blue, alpha = rgba[offset:offset + 4]
    a4 = alpha >> 4
    # Vendor compressed BGRA, pixel-formats.md §4: B6, G6, R8, alpha4.
    return bytes((
        (blue & 0xFC) | (a4 >> 2),
        (green & 0xFC) | (a4 & 0x03),
        red,
    ))


def build_overlay_packet(
    current: Image.Image,
    previous: Image.Image | None,
    mount_ccw: int,
    sequence: int,
) -> OverlayPacket | None:
    """Build one 0xCC video-overlay update.

    Compare all RGBA bytes, including transitions to transparent. Run records
    contain the changed pixels; the appended POSLEN describes the current
    visible overlay. Header n counts both lists and EF69; p counts POSLEN only.
    """
    current_device = _device_image(current, mount_ccw)
    previous_device = (
        _device_image(previous, mount_ccw) if previous is not None else None
    )
    raw = current_device.tobytes("raw", "RGBA")
    old = previous_device.tobytes("raw", "RGBA") if previous_device else None

    if raw == old:
        return None
    pixel_data = bytearray()
    changed_pixels = 0
    index = 0
    pixels = WIDTH * HEIGHT
    while index < pixels:
        offset = index * 4
        if old is not None and raw[offset:offset+4] == old[offset:offset+4]:
            index += 1
            continue
        start = index
        colours = bytearray()
        while index < pixels and index-start < MAX_RUN:
            offset = index * 4
            if old is not None and raw[offset:offset+4] == old[offset:offset+4]:
                break
            colours.extend(_pixel_bytes(raw, offset))
            index += 1
        count = index-start
        # Closed single-pixel runs use bit 23 instead of a count. The vendor
        # keeps a count for a run still open at the very end of the buffer.
        if count == 1 and index < pixels:
            pixel_data.extend((start | 0x800000).to_bytes(3, "big"))
        else:
            pixel_data.extend(start.to_bytes(3, "big"))
            pixel_data.extend(count.to_bytes(2, "big"))
        pixel_data.extend(colours)
        changed_pixels += count

    visible_data, visible_pixels, visible_segments = visibility_list(current_device)
    raw_body = bytes(pixel_data) + visible_data + MARKER
    framed = frame_stream(raw_body)
    header = overlay_update_command(len(raw_body), sequence, len(visible_data))
    return OverlayPacket(
        header=header,
        payload=framed,
        next_frame=current.copy(),
        changed_pixels=changed_pixels,
        visible_pixels=visible_pixels,
        visible_segments=visible_segments,
        payload_bytes=len(framed),
        pixel_bytes=len(pixel_data),
        poslen_bytes=len(visible_data),
    )
