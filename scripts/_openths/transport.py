from __future__ import annotations

import re
import socket
import struct
import time
from dataclasses import dataclass

WIRE64 = bytes.fromhex(
    "6143634d65544b68786e777a6d6f5062734734457655386779643032423371366649565758595a6a41705272447475486b694c6c4e314f39463553374a512b2f"
)
BASE64 = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/"

MAX_FRAME = 8 * 1024 * 1024
HEADER = struct.Struct("<HiiHiii")


class WireError(ValueError):
    """数据不满足已支持的协议结构。"""


def frame(payload: bytes) -> bytes:
    if not 0 < len(payload) <= MAX_FRAME:
        raise WireError("frame length")
    return b"\xfd" * 4 + f"{len(payload):08x}".encode("ascii") + b"\0" + payload


def receive(sock: socket.socket, deadline: float) -> bytes:
    def take(count: int) -> bytes:
        data = bytearray()
        while len(data) < count:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError
            sock.settimeout(remaining)
            chunk = sock.recv(min(65536, count - len(data)))
            if not chunk:
                raise EOFError("truncated frame")
            data.extend(chunk)
        return bytes(data)

    while True:
        header = take(13)
        if not re.fullmatch(rb"\xfd{4}[0-9a-fA-F]{8}\x00", header):
            raise WireError("frame header")
        length = int(header[4:12], 16)
        if length > MAX_FRAME:
            raise WireError("frame length")
        if length:
            return take(length)


@dataclass(frozen=True)
class Packet:
    number: int
    flags: int
    instance: int
    frame_id: int
    reserved: int
    extension: bytes
    body: bytes


def packet(raw: bytes) -> Packet:
    if len(raw) < HEADER.size:
        raise WireError("packet header")
    size, number, flags, instance, length, frame_id, reserved = HEADER.unpack_from(raw)
    if not HEADER.size <= size <= len(raw) or len(raw) - size != length:
        raise WireError("packet length")
    return Packet(
        number, flags, instance, frame_id, reserved, raw[HEADER.size : size], raw[size:]
    )


def snappy(raw: bytes) -> bytes:
    offset = 0
    output = bytearray()

    def integer(width: int) -> int:
        nonlocal offset
        if len(raw) - offset < width:
            raise WireError("truncated block")
        start = offset
        offset += width
        return int.from_bytes(raw[start:offset], "little")

    expected = 0
    for shift in range(0, 35, 7):
        part = integer(1)
        expected |= (part & 127) << shift
        if part < 128:
            break
    else:
        raise WireError("snappy length")
    if expected > MAX_FRAME:
        raise WireError("snappy limit")
    while offset < len(raw):
        tag = integer(1)
        kind, n = tag & 3, tag >> 2
        if kind == 0:
            count = 1 + (n if n < 60 else integer(n - 59))
            if count > min(len(raw) - offset, expected - len(output)):
                raise WireError("literal length")
            output.extend(raw[offset : offset + count])
            offset += count
        else:
            if kind == 1:
                count = 4 + (n & 7)
                distance = ((tag & 224) << 3) | integer(1)
            else:
                count = n + 1
                distance = integer(2 if kind == 2 else 4)
            if not 1 <= distance <= len(output) or count > expected - len(output):
                raise WireError("copy bounds")
            seed = output[
                len(output) - distance : len(output) - distance + min(count, distance)
            ]
            output.extend((seed * ((count + len(seed) - 1) // len(seed)))[:count])
    if len(output) != expected:
        raise WireError("snappy output length")
    return bytes(output)


def business_body(
    frame_id: int, instance: int, lines: tuple[str, ...], encoding="ascii"
) -> bytes:
    if type(instance) is not int or not 1 <= instance <= 65535:
        raise WireError("instance")
    header = (
        "[frame]",
        f"id={frame_id}",
        f"pageList={instance}",
        f"reqPage={instance}",
        "reqPageCount=1",
        f"[{instance}]",
    )
    return ("\r\n".join((*header, *lines)) + "\r\n").encode(encoding)


def checksum(body: bytes) -> int:
    state = 0
    length = len(body)
    trailer = length.to_bytes((length.bit_length() + 7) // 8, "little")
    for value in body + trailer:
        state ^= value << 24
        for _ in range(8):
            state = (
                (state << 1) ^ (0x04C11DB7 if state & 0x80000000 else 0)
            ) & 0xFFFFFFFF
    return state ^ 0xFFFFFFFF


def request_frame(body, *, number, flags, frame_id, app_info, session="", trace=None):
    import base64

    crc = checksum(app_info.encode() + b"TONG" + body + (session or "0").encode())
    encoded = base64.b64encode(body).translate(bytes.maketrans(BASE64, WIRE64))
    tail = b""
    if trace is not None:
        label = b"traceid=" + trace.encode("ascii")
        tail = struct.pack("<H", len(label)) + label
    header = struct.pack(
        "<HiihiiiIIH",
        34 + len(tail),
        number,
        flags,
        -253,
        len(encoded),
        frame_id,
        0 if flags == 0x70000 else len(body),
        0,
        crc,
        0,
    )
    return frame(header + tail + encoded)


def unmap_body(body):
    import base64

    data = body.split(b"\0", 1)[0].split(b"=", 1)[0].replace(b" ", b"+")
    if any(c not in WIRE64 for c in data):
        raise WireError("body alphabet")
    data = data.translate(bytes.maketrans(WIRE64, BASE64))
    try:
        return base64.b64decode(data + b"=" * (-len(data) % 4), validate=True)
    except ValueError as exc:
        raise WireError("invalid encoded body") from exc


class SessionRejected(Exception):
    """服务端拒绝匿名行情初始化。"""


def send_until(sock, data, deadline):
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError
    sock.settimeout(remaining)
    sock.sendall(data)


def initialize(sock, config, app_info, deadline):
    import json
    import uuid

    profile = config.encode("utf-16le")
    if not app_info or len(profile) > 65534:
        raise WireError("initialization parameters")
    body = struct.pack("<H", len(profile) // 2) + profile + b"\0\0"
    send_until(
        sock,
        request_frame(
            body,
            number=0,
            flags=0x70000,
            frame_id=0,
            app_info=app_info,
            trace=uuid.uuid4().hex,
        ),
        deadline,
    )
    accepted = False
    session = None
    for _ in range(20):
        pkt = packet(receive(sock, deadline))
        if len(pkt.extension) != 2 or pkt.flags not in (6, 11, 65547):
            raise WireError("initialization response")
        value = unmap_body(pkt.body) if pkt.extension == b"\x03\xff" else pkt.body
        if pkt.flags == 6:
            if len(value) < 9:
                raise WireError("truncated acknowledgement")
            if pkt.frame_id != 2201 or value[0] & 0x22:
                raise SessionRejected
            # status + random，随后依次为 passport、用户信息、UTF-16 消息。
            pos = 3
            for width in (1, 1, 2):
                if len(value) - pos < 2:
                    raise WireError("acknowledgement length")
                length = int.from_bytes(value[pos : pos + 2], "little")
                pos += 2 + width * length
                if pos > len(value):
                    raise WireError("acknowledgement field")
            accepted = True
        else:
            info = json.loads(value.decode("gbk"))
            if not isinstance(info, dict):
                raise WireError("session metadata")
            if "sessionId" in info:
                ident = info["sessionId"]
                if (
                    not isinstance(ident, str)
                    or not ident
                    or (session is not None and ident != session)
                ):
                    raise WireError("session identity")
                session = ident
        if session is not None and accepted:
            return session
    raise WireError("initialization frame limit")
