from __future__ import annotations

import struct

from .transport import MAX_FRAME, WireError


def ranking_body(sort: str, direction: str, start: int, instance: int) -> bytes:
    fields = {
        "change_rate": 34818,
        "turnover": 19,
        "rapid_change_rate": 48,
        "large_order_net_volume": 34370,
        "main_net_inflow": 34391,
    }
    if (
        sort not in fields
        or direction not in ("asc", "desc")
        or (direction == "asc" and sort != "change_rate")
        or type(start) is not int
        or not 0 <= start < 10_000_000
        or type(instance) is not int
        or not 1 <= instance <= 65535
    ):
        raise WireError("ranking parameters")
    lines = [
        "[frame]",
        "id=2312",
        f"pageList={instance}",
        f"reqPage={instance}",
        "reqPageCount=1",
        f"[{instance}]",
        "id=1282",
        f"startrow={start}",
        "rowcount=24",
        "marketId=0",
        f"sortorder={int(direction == 'asc')}",
        f"sortid={fields[sort]}",
    ]
    return ("\r\n".join(lines) + "\r\n").encode("ascii")


def expand_table_runs(data: bytes, size: int) -> bytes:
    """解码 tb3 内层按字节列存放的重复段，末段可有一字节填充。"""
    if not 0 < size <= MAX_FRAME:
        raise WireError("table output bounds")
    stream = iter(data)

    def octet():
        try:
            return next(stream)
        except StopIteration:
            raise WireError("truncated table runs") from None

    control = octet()
    mask = 128

    def bit():
        nonlocal control, mask
        value = int(bool(control & mask))
        mask >>= 1
        if not mask:
            control = octet()
            mask = 128
        return value

    previous = octet()
    out = bytearray([previous])
    while len(out) < size:
        if not bit():
            out.append(octet())
            previous = octet()
            out.append(previous)
            continue
        if not bit():
            previous = octet()
            out.append(previous)
        ones = 0
        while ones < 6 and bit():
            ones += 1
        if ones == 6:
            count = 12
            while True:
                n = octet()
                if n >= 128:
                    n = (n - 128) * 256 + octet()
                count += n
                if count > size + 1 - len(out):
                    raise WireError("table run overflow")
                if n != 32767:
                    break
        elif ones == 3:
            count = 5 + 2 * bit() + bit()
        elif ones in (1, 4):
            count = (2 if ones == 1 else 9) + bit()
        else:
            count = {0: 1, 2: 4, 5: 11}[ones]
        if count > size + 1 - len(out):
            raise WireError("table run overflow")
        out.extend(bytes([previous]) * count)
    return bytes(out[:size])


def table_records(data: bytes):
    """恢复 tb1/tb3 多列行，返回列描述、元数据区及行字节。"""
    if len(data) < 24 or data[:6] not in (b"tb1.0\0", b"tb3.0\0"):
        raise WireError("table signature")
    rows, _, _, start, width, count = struct.unpack_from("<iIiHHH", data, 6)
    descriptors_end = 24 + 4 * count
    if not (
        0 <= rows <= 10000
        and 1 <= count <= 256
        and descriptors_end <= start <= len(data)
        and rows * width <= MAX_FRAME
    ):
        raise WireError("table header bounds")
    columns = tuple(struct.iter_unpack("<HBB", data[24:descriptors_end]))
    if sum(n + 1 for _, n, _ in columns) != width:
        raise WireError("table stride")
    if data.startswith(b"tb3"):
        if (
            start + 8 > len(data)
            or int.from_bytes(data[start : start + 4], "little")
            != len(data) - start - 4
            or int.from_bytes(data[start + 4 : start + 8], "big") != rows * width
        ):
            raise WireError("compressed table bounds")
        plane = expand_table_runs(data[start + 8 :], rows * width)
        body = bytearray(rows * width)
        for i in range(width):
            body[i::width] = plane[i * rows : (i + 1) * rows]
    else:
        body = data[start:]
        if len(body) != rows * width:
            raise WireError("row data bounds")
    return columns, data[descriptors_end:start], bytes(body)


def grid_metadata(raw: bytes, columns: int) -> dict:
    pos = 0

    def take(count):
        nonlocal pos
        if pos + count > len(raw):
            raise WireError("table metadata length")
        pos += count
        return raw[pos - count : pos]

    def word():
        return int.from_bytes(take(2), "little")

    def string():
        return take(word() * 2).decode("utf-16le")

    for _ in range(columns + 1):
        string()
    fields = {}
    if pos < len(raw):
        for _ in range(word()):
            tag = word()
            key, kind = tag & 0x8FFF, tag & 0x7000
            if key in fields:
                raise WireError("duplicate extension")
            if kind == 0:
                fields[key] = string()
            elif kind == 0x2000 and key != 34068:
                fields[key] = int.from_bytes(take(4), "little", signed=True)
            else:
                raise WireError("unsupported extension")
    if any(raw[pos:]):
        raise WireError("table metadata trailing data")
    return fields


def grid_cells(data: bytes):
    from decimal import Decimal

    columns, meta, raw = table_records(data)
    fields = grid_metadata(meta, len(columns))
    if len({tag & 0x8FFF for tag, _, _ in columns}) != len(columns):
        raise WireError("duplicate column")
    trim = "".join(chr(i) for i in range(33))
    pos = 0
    rows = []
    while pos < len(raw):
        row = {}
        for tag, size, _ in columns:
            encoded = raw[pos : pos + size]
            flag = raw[pos + size]
            pos += size + 1
            kind = tag & 0x7000
            if kind == 0 and size % 2 == 0:
                value = encoded.decode("utf-16le").strip(trim)
            elif kind == 4096 and size == 4:
                word = int.from_bytes(encoded, "little")
                value = None
                if word not in (0xFFFFFFFF, 0x80000000):
                    magnitude = word & 0x7FFFFFF
                    exponent = (word >> 28) & 7
                    value = Decimal(
                        -magnitude if word & (1 << 27) else magnitude
                    ).scaleb(-exponent if word >> 31 else exponent)
            elif (kind, size) in ((8192, 4), (12288, 2)):
                value = int.from_bytes(encoded, "little", signed=True)
            else:
                raise WireError("column type")
            row[tag & 0x8FFF] = (value, flag)
        rows.append(row)
    return fields, rows


def ranking_table(raw: bytes, sort: str, direction: str, start: int):
    from datetime import UTC, datetime
    from decimal import Decimal

    fields, rows = grid_cells(raw)
    total = fields.get(34056)
    if (
        len(rows) > 24
        or fields.get(34055) != start
        or (total is not None and (type(total) is not int or start + len(rows) > total))
    ):
        raise WireError("ranking page bounds")
    extra = {
        "rapid_change_rate": ("m10_rapid", 48, None),
        "large_order_net_volume": (
            "m10_large",
            34370,
            "https://www.10jqka.com.cn/ad_mar/help/dde.html",
        ),
        "main_net_inflow": (
            "m10_main",
            34391,
            "https://news.10jqka.com.cn/20260827/c679348022.shtml",
        ),
    }
    profile, metric, url = extra.get(sort, ("m7_baseline", None, None))
    suffixes = {16: "万手", 32: "%", 48: "亿", 64: "万", 80: "万亿"}
    items = []
    for row in rows:
        identities = [row.get(key, (None,))[0] for key in (4, 36103, 55)]
        if any(not isinstance(v, str) or not v for v in identities):
            raise WireError("ranking identity")
        code, market, name = identities
        item = dict(
            stock_code=code,
            market=market,
            name=name,
            rank=None,
            rapid_change_rate=None,
            large_order_net_volume=None,
            main_net_inflow=None,
        )
        metrics = [(10, "latest"), (34818, "change_rate"), (19, "turnover")]
        if metric is not None:
            metrics.append((metric, sort))
        for key, label in metrics:
            value, flag = row.get(key, (None, 0))
            item[label] = None
            if value is None:
                continue
            text = format(value, "f") if isinstance(value, Decimal) else str(value)
            suffix = suffixes.get(flag & 240, "")
            if (
                key == 48
                and suffix != "%"
                or key in (34370, 34391)
                and flag & 240 not in (0, 32, 48, 64, 80)
            ):
                raise WireError("ranking metric format")
            unit = (
                "%"
                if key in (34818, 48)
                else (suffix or None)
                if key in (34370, 34391)
                else None
            )
            item[label] = dict(
                raw=text, display=text + suffix, format_byte=flag, unit=unit
            )
        items.append(item)
    more = None if total is None else start + len(items) < total
    return dict(
        items=items,
        scope=profile,
        sort=sort,
        direction=direction,
        total=total,
        next_page_available=bool(items) and more is not False,
        has_more=more,
        received_at=datetime.now(UTC),
        source_time=None,
        window_minutes=5 if metric == 48 else None,
        window_source="user_confirmed" if metric == 48 else None,
        definition_source="user_adopted_official" if url else None,
        definition_url=url,
    )
