"""独立合成字节测试，覆盖短读分帧及原始 Snappy 指令。"""

import importlib.util
import io
import struct
import sys
import time
import unittest
from pathlib import Path

cli_spec = importlib.util.spec_from_file_location(
    "quote_wire_test_cli", Path(__file__).resolve().parents[1] / "scripts/query.py"
)
cli = importlib.util.module_from_spec(cli_spec)
cli_spec.loader.exec_module(cli)

spec = importlib.util.spec_from_file_location(
    "quote_wire", Path(__file__).resolve().parents[1] / "scripts/quote_wire.py"
)
wire = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = wire
spec.loader.exec_module(wire)


class ShortReader:
    def __init__(self, raw):
        self.stream = io.BytesIO(raw)

    def settimeout(self, remaining):
        assert remaining > 0

    def recv(self, count):
        return self.stream.read(min(3, count))


class WireTests(unittest.TestCase):
    def test_keepalive_and_frames(self):
        reader = ShortReader(
            b"\xfd" * 4 + b"00000000\0" + wire.frame(b"one") + wire.frame(b"two")
        )
        self.assertEqual(
            [wire.receive(reader, time.monotonic() + 1) for _ in range(2)],
            [b"one", b"two"],
        )

    def test_truncation_and_invalid_lengths(self):
        for raw, error in [
            (b"abc", EOFError),
            (b"\xfd" * 4 + b"ffffffff\0", wire.WireError),
            (b"\xfd" * 4 + b"00000004\0ab", EOFError),
        ]:
            with self.assertRaises(error):
                wire.receive(ShortReader(raw), time.monotonic() + 1)

    def test_application_packet(self):
        raw = struct.pack("<HiiHiii", 26, 1, 0x1100, 1, 3, 2267, 0) + b"\0\0abc"
        self.assertEqual(wire.packet(raw).body, b"abc")
        with self.assertRaises(wire.WireError):
            wire.packet(raw[:-1])

    def test_snappy(self):
        for tag, operand in [(1, b"\x01"), (14, b"\x01\0"), (15, b"\x01\0\0\0")]:
            self.assertEqual(wire.snappy(bytes([5, 0, 65, tag]) + operand), b"AAAAA")
        self.assertEqual(wire.snappy(b"\x03\x08abc"), b"abc")
        for raw in [b"", b"\x01", b"\x03\x08ab", b"\x04\x01\x00"]:
            with self.assertRaises(wire.WireError):
                wire.snappy(raw)

    def test_quote_table_identity_and_precision(self):
        keys = [4, 36103, 19]
        width = 16
        metadata = struct.pack("<HHHHH3i", 0, 0, 1, 0xE008, len(keys), *keys)
        header = b"tb1.0\0" + struct.pack(
            "<iIiHHHHBB", 3, 0, 0, 28 + len(metadata), width + 1, 1, 0, width, 0
        )
        body = b"".join(
            text.encode("utf-16le").ljust(width, b"\0") + bytes([fmt])
            for text, fmt in [("000001", 0), ("33", 0), ("1.20", 48)]
        )
        raw = header + metadata + body
        quote = wire.quote_table(raw, "000001", "33")
        self.assertEqual(quote["turnover"]["display"], "1.20亿")
        self.assertIsNone(quote["latest"])
        for invalid, code in [
            (raw, "000002"),
            (raw[:-1], "000001"),
            (raw + b"X", "000001"),
        ]:
            with self.assertRaises(wire.WireError):
                wire.quote_table(invalid, code, "33")

    def test_quote_request(self):
        raw = wire.quote_body("000063", "33", 2)
        self.assertIn(b"pageList=2\r\nreqPage=2\r\n", raw)
        self.assertTrue(
            raw.endswith(b"id=1269\r\nstockcode=000063\r\nmarketcode=33\r\n")
        )

    def test_ranking_combinations_and_offset(self):
        for sort, direction, key in [
            ("change_rate", "asc", 34818),
            ("change_rate", "desc", 34818),
            ("turnover", "desc", 19),
            ("rapid_change_rate", "desc", 48),
            ("large_order_net_volume", "desc", 34370),
            ("main_net_inflow", "desc", 34391),
        ]:
            body = wire.ranking_body(sort, direction, 24, 2)
            self.assertIn(b"startrow=24\r\nrowcount=24\r\n", body)
            self.assertTrue(
                body.endswith(
                    f"sortorder={int(direction == 'asc')}\r\nsortid={key}\r\n".encode()
                )
            )
        with self.assertRaises(wire.WireError):
            wire.ranking_body("turnover", "asc", 0, 1)

    def test_tb3_transpose(self):
        header = b"tb3.0\0" + struct.pack("<iIiHHHHBB", 3, 0, 0, 28, 3, 1, 12289, 2, 0)
        plane = b"abcdefghi"
        compressed = b"\0" + plane
        raw = (
            header
            + struct.pack("<I", len(compressed) + 4)
            + struct.pack(">I", 9)
            + compressed
        )
        self.assertEqual(wire.table_records(raw)[2], b"adgbehcfi")
        for broken in (raw[:-1], raw + b"x", raw[:28]):
            with self.assertRaises(wire.WireError):
                wire.table_records(broken)

    def test_ranking_field_projection_and_last_page(self):
        columns = [
            (4, 12, 0),
            (36103, 4, 0),
            (55, 2, 0),
            (0x1000 | 10, 4, 0),
            (0x1000 | 34818, 4, 0),
        ]
        descriptors = b"".join(struct.pack("<HBB", *col) for col in columns)
        meta = b"\0\0" * 6 + struct.pack(
            "<HHiHi", 2, 34055 | 8192, 24, 34056 | 8192, 25
        )
        cells = (
            "000001".encode("utf-16le")
            + b"\0"
            + "33".encode("utf-16le")
            + b"\0"
            + "A".encode("utf-16le")
            + b"\0"
            + struct.pack("<IBIB", 0xA000013C, 10, 0xA00007DF, 42)
        )
        header = b"tb1.0\0" + struct.pack(
            "<iIiHHH",
            1,
            0,
            0,
            24 + len(descriptors) + len(meta),
            len(cells),
            len(columns),
        )
        raw = header + descriptors + meta + cells
        page = wire.ranking_table(raw, "change_rate", "desc", 24)
        self.assertFalse(page["next_page_available"])
        self.assertEqual(page["total"], 25)
        self.assertEqual(page["items"][0]["stock_code"], "000001")
        self.assertEqual(page["items"][0]["change_rate"]["display"], "20.15%")
        self.assertIsNone(page["items"][0]["turnover"])
        with self.assertRaises(wire.WireError):
            wire.ranking_table(raw, "change_rate", "desc", 0)

    def test_market_breadth_and_limits_share_one_resource(self):
        import json

        value = {
            "all": {"total": {"up": 3, "down": 1}, "data": {"zt": 2, "dt": 4}},
            "time": ["10:00"],
            "zt": [0],
            "dt": [1],
        }
        raw = json.dumps(
            {"hs_datacenter_ztdt": [dict(stockcode="1A0001", value=json.dumps(value))]}
        ).encode()
        breadth, limits, label = wire.breadth_resource(raw)
        self.assertEqual(
            (
                breadth["rising"]["value"],
                limits["limit_up"]["value"],
                limits["limit_down"]["value"],
                label,
            ),
            ("3", "2", "1", "10:00"),
        )
        self.assertIsNone(breadth["total"])
        self.assertIsNone(breadth["unchanged"])
        with self.assertRaises(wire.WireError):
            wire.breadth_resource(raw.replace(b"1A0001", b"1A0002"))
        self.assertTrue(
            wire.breadth_body(2).decode("utf-16le").endswith("marketcode=16\r\n\r\n")
        )

    def test_session_requires_both_replies(self):
        def response(flags, frame_id, body):
            return wire.frame(
                struct.pack("<HiiHiiiH", 26, 0, flags, 0, len(body), frame_id, 0, 0)
                + body
            )

        meta = response(11, 0, b'{"sessionId":"demo"}')
        ok = response(6, 2201, b"\x00" * 9)

        class Connection(ShortReader):
            def sendall(self, data):
                self.sent = data

        for raw in (ok + meta, meta + ok):
            conn = Connection(raw)
            self.assertEqual(
                wire.initialize(conn, "A", "example", time.monotonic() + 1), "demo"
            )
            request = wire.packet(conn.sent[13:])
            self.assertEqual(request.flags, 0x70000)
            self.assertEqual(wire.unmap_body(request.body), b"\x01\x00A\x00\x00\x00")
        for raw, error in [
            (meta, EOFError),
            (response(6, 999, b"\0" * 9), wire.SessionRejected),
            (response(6, 2201, b"\0" * 3 + b"\xff" * 6), wire.WireError),
        ]:
            with self.assertRaises(error):
                wire.initialize(Connection(raw), "A", "example", time.monotonic() + 1)
        self.assertEqual(wire.checksum(b"abc"), 1219131554)

    def test_turnover_matched_minute_and_fallback(self):
        def curve(day, minute, amount):
            n = int(day[-2:])
            stamp = (
                (126 << 20)
                | (11 << 16)
                | (n << 11)
                | ((minute // 100) << 6)
                | (minute % 100)
            )
            return {4: "883957", 36103: "48", 32790: int(day)}, {
                1: [stamp],
                19: [amount],
            }

        current = curve("20261104", 930, 1500000000)
        for minute, basis in [
            (930, "上一交易日同刻"),
            (935, "上一交易日末值（同刻缺失回退）"),
        ]:
            result = wire.turnover_result(
                current, curve("20261103", minute, 1200000000)
            )
            self.assertEqual(result["comparison_basis"], basis)
            self.assertEqual(result["comparison"]["raw"], "+3亿")
            self.assertEqual(result["reference_time_label"], f"20261103/{minute:04d}")
        with self.assertRaises(wire.WireError):
            wire.turnover_result(current, curve("20261102", 930, 1))

    def test_fund_flow_identity_and_scale(self):
        def part(code, market, n):
            return {4: code, 36103: market, 33282: "DDE", 55: "index"}, {
                1: [930],
                33016: [n],
            }

        sh = part("1A0001", "16", 125000000)
        sz = part("399001", "32", -25000000)
        result = wire.fund_flow(sh, sz)
        self.assertEqual(result["net"]["raw"], "+1.00亿")
        self.assertEqual(result["sources"][1]["net"]["raw"], "-25000000")
        with self.assertRaises(wire.WireError):
            wire.fund_flow(sz, sh)
