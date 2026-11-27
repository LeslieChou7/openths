from __future__ import annotations

import socket
import struct
import time

from .ranking import expand_table_runs, grid_cells
from .transport import (
    MAX_FRAME,
    SessionRejected,
    WireError,
    business_body,
    initialize,
    packet,
    receive,
    request_frame,
    send_until,
    snappy,
    unmap_body,
)


def breadth_body(instance: int) -> bytes:
    return business_body(
        6001,
        instance,
        (
            "id=1002",
            "key=hs_datacenter_ztdt",
            "action=subscribe",
            "stockcode=1A0001",
            "marketcode=16",
            "",
        ),
        "utf-16le",
    )


def market_metric(label, raw, value, unit, precision, derivation=None):
    return dict(
        label=label,
        raw=raw,
        value=value,
        unit=unit,
        precision=precision,
        currency=None,
        derivation=derivation,
    )


def breadth_resource(raw: bytes):
    import json

    def count(label, n):
        if type(n) is not int or not 0 <= n <= 10_000_000:
            raise WireError("invalid market count")
        return market_metric(label, str(n), str(n), "家", "exact")

    try:
        entries = json.loads(raw.decode("utf-8"))["hs_datacenter_ztdt"]
        if (
            not isinstance(entries, list)
            or len(entries) != 1
            or entries[0]["stockcode"] != "1A0001"
        ):
            raise WireError("breadth identity")
        data = json.loads(entries[0]["value"])
        totals = data["all"]["total"]
        aggregate = data["all"]["data"]
        breadth = dict(
            rising=count("上涨家数", totals["up"]),
            falling=count("下跌家数", totals["down"]),
            unchanged=count("平盘家数", totals["deuce"]) if "deuce" in totals else None,
            total=None,
            other=[count("tp", totals["tp"])] if "tp" in totals else [],
        )
        times, up, down = (data.get(k, []) for k in ("time", "zt", "dt"))
        if (
            not all(isinstance(values, list) for values in (times, up, down))
            or not len(up) == len(down) == len(times) <= 2000
        ):
            raise WireError("limit series")
        stamp = times[-1] if times else None
        if stamp is not None and (not isinstance(stamp, str) or len(stamp) > 32):
            raise WireError("source time")
        limits = dict(
            limit_up=count("涨停家数", (up and up[-1]) or aggregate["zt"]),
            limit_down=count("跌停家数", (down and down[-1]) or aggregate["dt"]),
            other=[],
        )
        return breadth, limits, stamp
    except (ValueError, TypeError, KeyError, RecursionError) as exc:
        raise WireError("invalid market breadth response") from exc


def size_snapshot(raw: bytes) -> dict:
    from decimal import Decimal

    fields, rows = grid_cells(raw)
    if len(rows) != 2 or fields.get(34055) != 0 or fields.get(34056) != 2:
        raise WireError("index comparison bounds")
    result = {}
    for row, key, code in zip(
        rows, ("large", "small"), ("1B0300", "1B0852"), strict=True
    ):
        if row.get(4, (None,))[0] != code or row.get(34338, (None,))[0] != 16:
            raise WireError("index comparison identity")
        name = row.get(55, (None,))[0]
        number, flag = row.get(34818, (None, 0))
        if not isinstance(name, str) or not name or flag & 240 != 32:
            raise WireError("index comparison fields")
        metric = None
        if number is not None:
            if not isinstance(number, Decimal):
                raise WireError("index comparison value")
            metric = market_metric(
                "涨跌幅", format(number, "f") + "%", number, "%", "rounded"
            )
        result[key] = dict(
            label=name, value=metric, source_time=None, source_time_label=None
        )
    return result


def curve_number(data):
    from decimal import Decimal

    word = int.from_bytes(data, "little")
    if word in (0xFFFFFFFF, 0x80000000):
        return None
    n = word & 0x7FFFFFF
    scale = (word >> 28) & 7
    return Decimal(-n if word & 0x08000000 else n).scaleb(
        -scale if word >> 31 else scale
    )


def curve_data(raw: bytes):
    if len(raw) < 24 or raw[:6] not in (b"cv1.0\0", b"cv3.0\0"):
        raise WireError("curve header")
    rows, _, first, start, width, n = struct.unpack_from("<iIiHHH", raw, 6)
    if first or not (
        1 <= rows <= 10000
        and 1 <= n <= 256
        and 24 + 4 * n <= start <= len(raw)
        and width * rows <= MAX_FRAME
    ):
        raise WireError("incomplete curve")
    columns = list(struct.iter_unpack("<HBB", raw[24 : 24 + 4 * n]))
    if (
        width != sum(c[1] for c in columns)
        or len({tag & 0x8FFF for tag, _, _ in columns}) != n
    ):
        raise WireError("curve columns")
    pos = 24 + 4 * n

    def take(count):
        nonlocal pos
        if pos + count > start:
            raise WireError("curve metadata bounds")
        pos += count
        return raw[pos - count : pos]

    def short():
        return int.from_bytes(take(2), "little")

    meta = {}
    for _ in range(short()):
        tag = short()
        key, kind = tag & 0x8FFF, tag & 0x7000
        if key in meta:
            raise WireError("duplicate curve metadata")
        if kind == 0:
            value = take(2 * short()).decode("utf-16le")
        elif kind == 4096:
            value = curve_number(take(4))
        elif kind in (8192, 12288):
            value = int.from_bytes(
                take(4 if kind == 8192 else 2), "little", signed=True
            )
        elif kind == 20480:
            value = take(8)
        elif kind == 24576:
            size = short()
            if size > 100:
                raise WireError("curve array size")
            value = tuple(x[0] for x in struct.iter_unpack("<i", take(4 * size)))
        else:
            raise WireError("curve metadata type")
        meta[key] = value
    if any(take(start - pos)):
        raise WireError("curve metadata padding")
    body = raw[start:]
    if raw.startswith(b"cv3"):
        if (
            len(body) < 8
            or int.from_bytes(body[:4], "little") != len(body) - 4
            or int.from_bytes(body[4:8], "big") != rows * width
        ):
            raise WireError("compressed curve length")
        planes = expand_table_runs(body[8:], rows * width)
        body = bytearray(rows * width)
        for octet in range(width):
            body[octet::width] = planes[octet * rows : (octet + 1) * rows]
    if len(body) != rows * width:
        raise WireError("curve length")
    result = {}
    offset = 0
    for tag, size, _ in columns:
        kind = tag & 0x7000
        if (kind, size) not in ((4096, 4), (8192, 4), (12288, 2)):
            raise WireError("curve column type")
        values = []
        for i in range(offset, len(body), width):
            cell = body[i : i + size]
            values.append(
                curve_number(cell)
                if kind == 4096
                else int.from_bytes(cell, "little", signed=True)
            )
        result[tag & 0x8FFF] = values
        offset += size
    return meta, result


def fund_flow(sh, sz):
    from decimal import ROUND_HALF_UP, Decimal

    def single(number):
        return struct.unpack("<f", struct.pack("<f", number))[0]

    parts = []
    amounts = []
    for curve, code, market in ((sh, "1A0001", "16"), (sz, "399001", "32")):
        meta, columns = curve
        if (meta.get(4), meta.get(36103), meta.get(33282)) != (
            code,
            market,
            "DDE",
        ) or not isinstance(meta.get(55), str):
            raise WireError("DDE curve identity")
        amount = columns[33016][-1]
        stamp = columns[1][-1]
        if amount is None or type(stamp) is not int:
            raise WireError("DDE missing endpoint")
        amounts.append(single(float(amount) / 1e8))
        parts.append(
            dict(
                stock_code=code,
                market=market,
                net=market_metric(
                    meta[55] + " DDE净流入",
                    format(Decimal(amount), "f"),
                    amount,
                    None,
                    "unknown",
                ),
                source_time=None,
                source_time_label=str(stamp),
            )
        )
    net = single(sum(amounts))
    display = Decimal.from_float(net).quantize(Decimal(".01"), rounding=ROUND_HALF_UP)
    metric = market_metric(
        "大盘资金净流入",
        ("+" if net > single(0.01) else "") + f"{display:.2f}亿",
        display,
        "亿",
        "rounded",
        "app_sum_float32_dde",
    )
    return dict(
        net=metric,
        inflow=None,
        outflow=None,
        classification="大盘资金净流入；分类算法未知",
        sources=parts,
    )


def reference_day(current):
    from pathlib import Path

    day = str(current[0].get(32790))
    calendar = (
        Path(__file__)
        .resolve()
        .parents[2]
        .joinpath("assets", "trading-days-2026.txt")
        .read_text("ascii")
        .splitlines()
    )
    if day not in calendar or day == calendar[0]:
        raise WireError("reference day unavailable")
    return calendar[calendar.index(day) - 1]


def turnover_result(current, reference):
    from decimal import Decimal

    prior = reference_day(current)
    today = str(current[0][32790])

    def points(curve, day):
        meta, columns = curve
        if (meta.get(4), meta.get(36103)) != ("883957", "48") or str(
            meta.get(32790, day)
        ) != day:
            raise WireError("turnover identity")
        result = []
        for stamp, amount in zip(columns[1], columns[19], strict=True):
            if type(stamp) is not int or amount is None:
                raise WireError("invalid turnover point")
            encoded = f"{1900 + ((stamp >> 20) & 255):04d}{(stamp >> 16) & 15:02d}{(stamp >> 11) & 31:02d}"
            if encoded != day:
                raise WireError("turnover date")
            minute = ((stamp >> 6) & 31) * 100 + (stamp & 63)
            result.append((minute, float(amount)))
        return result

    now, old = points(current, today), points(reference, prior)
    minute, amount = now[-1]
    same = minute in dict(old)
    ref_min, ref = (minute, dict(old)[minute]) if same else old[-1]
    full = len(now) == 241 and minute == 1500
    basis = (
        ("上一交易日全日" if full and len(old) == 241 else "上一交易日同刻")
        if same
        else "上一交易日末值（同刻缺失回退）"
    )

    def metric(label, value, delta=False):
        unit = None
        n = abs(value)
        if n >= 1e5:
            unit, div = ("亿", 1e8) if n >= 1e8 else ("万", 1e4)
            text = str(int(n / div + 0.5))
        else:
            text = str(n)
        text = ("-" if value < 0 else "") + text
        return market_metric(
            label,
            ("+" if delta and value > 0 else "") + text + (unit or ""),
            Decimal(text),
            unit,
            "rounded",
            "app_current_minus_reference" if delta else None,
        )

    return dict(
        current=metric("今日成交额总计" if full else "今日实时成交额", amount),
        comparison=metric(
            ("较上一日" if full else "较上一日此时") if same else "较上一交易日末值",
            amount - ref,
            True,
        ),
        reference=metric(basis + "成交额", ref),
        comparison_basis=basis,
        reference_time=None,
        reference_time_label=f"{prior}/{ref_min:04d}",
        source_time=None,
        source_time_label=f"{today}/{minute:04d}",
        current_source_value=Decimal.from_float(amount),
        reference_source_value=Decimal.from_float(ref),
    )


def market_source(args, config, deadline, kind, reference=None):
    import uuid

    if kind == "breadth":
        frame_id, body = 6001, breadth_body(1)
    elif kind == "size":
        frame_id = 2312
        body = business_body(
            frame_id,
            1,
            (
                "id=1264",
                "startrow=0",
                "sortid=-1",
                "rowcount=2",
                "newrealtime=0",
                "selfstockcustom=1",
                "update=1",
                "columnorder=55|4|34338|34818",
                "marketlist=16|16",
                "stocklist=1B0300|1B0852",
            ),
        )
    elif kind in ("sh", "sz"):
        frame_id = 9001
        code, market = ("1A0001", "16") if kind == "sh" else ("399001", "32")
        body = business_body(
            frame_id,
            1,
            ("id=1229", "", f"stockcode={code}", f"marketcode={market}", "tech=DDE"),
        )
    else:
        frame_id = 2312
        ref = kind == "reference"
        lines = (
            "id=1271" if ref else "id=1229",
            "",
            "stockcode=883957",
            f"fstrend={int(not ref)}",
            "marketcode=48",
        )
        if ref:
            lines += ("preday=0", "leftday=1", "pinterval=1", f"endtime={reference}")
        body = business_body(frame_id, 1, lines)
    for attempt in range(args.retries + 1):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError
        try:
            with socket.create_connection(
                (args.host, args.port), timeout=min(remaining, args.connect_timeout)
            ) as sock:
                session = initialize(sock, config, args.app_info, deadline)
                send_until(
                    sock,
                    request_frame(
                        body,
                        number=1,
                        flags=0x40000 if kind == "breadth" else 0x01010000,
                        frame_id=frame_id,
                        app_info=args.app_info,
                        session=session,
                        trace=uuid.uuid4().hex,
                    ),
                    deadline,
                )
                for _ in range(20):
                    pkt = packet(receive(sock, deadline))
                    if (
                        pkt.number == 0
                        and pkt.flags in (11, 65547)
                        and len(pkt.extension) == 2
                    ):
                        continue
                    break
                else:
                    raise WireError("market frame limit")
                if (pkt.number, pkt.instance) != (1, 1) or len(
                    pkt.body
                ) > 4 * 1024 * 1024:
                    raise WireError("market correlation")
                if kind == "breadth":
                    if (pkt.frame_id, pkt.flags, pkt.extension) != (
                        6001,
                        0x100B,
                        b"\x03\xff",
                    ):
                        raise WireError("breadth header")
                    return breadth_resource(snappy(unmap_body(pkt.body)))
                initial = (
                    pkt.extension == b"\0\xff"
                    and pkt.frame_id == 0
                    and kind != "reference"
                )
                normal = pkt.frame_id == frame_id and (
                    (len(pkt.extension) == 8 and pkt.extension[:4] == b"\0" * 4)
                    or pkt.extension == b"\0\xff"
                )
                flags = {
                    "size": (0x1100,),
                    "sh": (0x120, 0x1120),
                    "sz": (0x120, 0x1120),
                    "current": (0x100, 0x1100, 0x120),
                    "reference": (0x1100,),
                }[kind]
                if not (initial or normal) or pkt.flags not in flags:
                    raise WireError("market source header")
                raw = snappy(pkt.body) if pkt.flags & 0x1000 else pkt.body
                if len(raw) > 4 * 1024 * 1024:
                    raise WireError("market limit")
                return size_snapshot(raw) if kind == "size" else curve_data(raw)
        except (EOFError, OSError):
            if attempt == args.retries:
                raise


def market_overview(args, config):
    from datetime import UTC, datetime

    sources = {
        "breadth": (
            "hs_datacenter_ztdt",
            "来源1A0001/16；统计成分未确认",
            ["统计集合及tp含义未知", "时分标签缺少来源日期"],
        ),
        "size_comparison": (
            "1264:1B0300|1B0852",
            "App以沪深300/中证1000当前涨跌幅作大小盘对比",
            ["来源时间未知", "指数样本和加权算法未研究"],
        ),
        "fund_flow": (
            "1229:DDE:1A0001/16+399001/32",
            "App两路DDE末值按float32汇总",
            ["币种及资金分类算法未知", "两来源时间分别保留，非原子快照"],
        ),
        "turnover": (
            "1229+1271:883957/48",
            "App来源883957/48的成交额及上一交易日比较",
            ["统计集合及币种未知", "交易日仅覆盖冻结2026年度日历"],
        ),
    }
    result = {
        k: dict(status="not_requested", data=None, metadata=None, error=None)
        for k in (*sources, "limit_counts")
    }
    deadline = time.monotonic() + args.request_timeout
    for group, (source, scope, unknowns) in sources.items():
        names = [
            k
            for k in args.components
            if k == group or (group == "breadth" and k == "limit_counts")
        ]
        if not names:
            continue
        label = None
        try:
            if group == "breadth":
                breadth, limits, label = market_source(
                    args, config, deadline, "breadth"
                )
                values = dict(breadth=breadth, limit_counts=limits)
            elif group == "fund_flow":
                values = {
                    group: fund_flow(
                        market_source(args, config, deadline, "sh"),
                        market_source(args, config, deadline, "sz"),
                    )
                }
            elif group == "turnover":
                current = market_source(args, config, deadline, "current")
                value = turnover_result(
                    current,
                    market_source(
                        args, config, deadline, "reference", reference_day(current)
                    ),
                )
                values = {group: value}
                label = value["source_time_label"]
            else:
                values = {group: market_source(args, config, deadline, "size")}
            meta = dict(
                source_id=source,
                received_at=datetime.now(UTC),
                source_time=None,
                scope_description=scope,
                source_time_label=label,
                unknowns=unknowns,
            )
            for name in names:
                result[name] = dict(
                    status="ok", data=values[name], metadata=meta, error=None
                )
        except (
            ValueError,
            KeyError,
            TypeError,
            EOFError,
            OSError,
            SessionRejected,
        ) as error:
            if not args.allow_partial:
                raise
            category = (
                "authentication"
                if isinstance(error, SessionRejected)
                else "network"
                if isinstance(error, (EOFError, OSError))
                else "protocol"
            )
            for name in names:
                result[name] = dict(
                    status="error",
                    data=None,
                    metadata=None,
                    error=dict(category=category),
                )
    return dict(**result, scope="m8_baseline")
