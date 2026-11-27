from __future__ import annotations

import re
import struct

from .transport import WireError

QUOTE_MARKETS = ("17", "33", "151")


def quote_body(stock_code: str, market: str, instance: int) -> bytes:
    if (
        not re.fullmatch(r"[0-9]{6}", stock_code)
        or market not in QUOTE_MARKETS
        or not 1 <= instance <= 65535
    ):
        raise WireError("quote identity")
    return (
        f"[frame]\r\nid=2267\r\npageList={instance}\r\nreqPage={instance}\r\nreqPageCount=1\r\n"
        f"[{instance}]\r\nid=1269\r\nstockcode={stock_code}\r\nmarketcode={market}\r\n"
    ).encode("ascii")


def quote_table(raw: bytes, code: str, market: str) -> dict:
    if len(raw) < 28 or raw[:6] != b"tb1.0\0":
        raise WireError("quote table header")
    rows, _, _, start, stride, columns, descriptor, width, _ = struct.unpack_from(
        "<iIiHHHHBB", raw, 6
    )
    if (
        rows < 0
        or columns != 1
        or descriptor & 0x7000
        or width % 2
        or stride != width + 1
        or not 28 <= start <= len(raw)
        or rows * stride != len(raw) - start
    ):
        raise WireError("quote table shape")
    pos = 28

    def take(n):
        nonlocal pos
        if pos + n > start:
            raise WireError("quote metadata bounds")
        pos += n
        return raw[pos - n : pos]

    def short():
        return int.from_bytes(take(2), "little")

    def string():
        return take(2 * short()).decode("utf-16le")

    string()  # 表名
    string()  # 单列标题
    keys = None
    for _ in range(short()):
        tag = short()
        if tag == 0xE008 and keys is None:
            length = short()
            if length != rows:
                raise WireError("quote transpose count")
            keys = struct.unpack(f"<{length}i", take(length * 4))
        elif tag & 0x7000 == 0:
            string()
        else:
            raise WireError("unknown quote metadata")
    if keys is None or pos != start:
        raise WireError("missing quote transpose map")
    fields = {}
    trim = "".join(chr(i) for i in range(33))
    for index, key in enumerate(keys):
        offset = start + index * stride
        if key in fields:
            raise WireError("duplicate quote field")
        fields[key] = (
            raw[offset : offset + width].decode("utf-16le").strip(trim),
            raw[offset + width],
        )
    if fields.get(4, (None,))[0] != code or fields.get(36103, (None,))[0] != market:
        raise WireError("quote response identity")
    result = dict(stock_code=code, market=market, received_at=None)
    suffix = {16: "万手", 32: "%", 48: "亿", 64: "万", 80: "万亿"}
    for key, name in (
        (10, "latest"),
        (34318, "change"),
        (34315, "change_rate"),
        (7, "open"),
        (8, "high"),
        (9, "low"),
        (6, "previous_close"),
        (13, "volume"),
        (19, "turnover"),
    ):
        result[name] = None
        if key in fields:
            value, flag = fields[key]
            result[name] = dict(
                raw=value,
                format_byte=flag,
                display=value + suffix.get(flag & 240, ""),
                unit={13: "手", 34315: "%"}.get(key),
            )
    return result
