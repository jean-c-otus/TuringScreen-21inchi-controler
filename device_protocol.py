"""Protocol packet builders for Turing Rev C (2.1-inch, 480x480).

The transport uses 250-byte frames: 249 payload bytes followed by a 0x00
separator. Command packets are padded independently to a 250-byte boundary.
"""
from __future__ import annotations

FRAME_SIZE = 250
PAYLOAD_SIZE = 249
MARKER = b"\xef\x69"


def pad_command(data: bytes, pad_byte: int = 0) -> bytes:
    if not 0 <= pad_byte <= 255:
        raise ValueError("pad_byte must be 0..255")
    return data + bytes([pad_byte]) * (-len(data) % FRAME_SIZE)


def frame_stream(data: bytes) -> bytes:
    """Encode raw payload as 249 data bytes + one separator/padding byte."""
    if not data:
        return b""
    output = bytearray()
    for offset in range(0, len(data), PAYLOAD_SIZE):
        chunk = data[offset:offset + PAYLOAD_SIZE]
        output.extend(chunk)
        output.extend(b"\x00" * (FRAME_SIZE - len(chunk)))
    return bytes(output)


def path_command(opcode: int, path: str, extra: bytes = b"", sub: int = 0) -> bytes:
    encoded = path.encode("ascii")
    if not encoded or len(encoded) > 255:
        raise ValueError("设备路径必须是 1–255 字节的 ASCII 字符串")
    return (
        bytes([opcode]) + MARKER + b"\x00\x00\x00" + bytes([len(encoded)])
        + bytes([sub]) + b"\x00\x00" + encoded + extra
    )


def upload_command(path: str, size: int) -> bytes:
    if size <= 0 or size > 0xFFFFFFFF:
        raise ValueError("文件大小超出设备协议范围")
    return path_command(0x6F, path, size.to_bytes(4, "little", signed=False))


def overlay_update_command(payload_size: int, sequence: int, visible_size: int) -> bytes:
    if not 0 <= payload_size <= 0xFFFFFFFF:
        raise ValueError("叠加数据长度超出 32 位协议范围")
    if not 0 <= sequence <= 0xFFFFFFFF:
        raise ValueError("叠加帧序号超出 32 位协议范围")
    if not 0 <= visible_size <= 0xFFFFFFFF:
        raise ValueError("可见区域长度超出 32 位协议范围")
    return (
        b"\xcc" + MARKER + payload_size.to_bytes(4, "big") + b"\x00\x00\x00"
        + sequence.to_bytes(4, "big") + visible_size.to_bytes(4, "big")
    )
