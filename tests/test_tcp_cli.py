"""仅替换套接字，真实执行匿名握手、业务编解码、关闭和 CLI 输出。"""

import contextlib
import importlib.util
import io
import json
import struct
import unittest
from pathlib import Path
from unittest.mock import patch

spec = importlib.util.spec_from_file_location(
    "tcp_cli", Path(__file__).resolve().parents[1] / "scripts/query.py"
)
query = importlib.util.module_from_spec(spec)
spec.loader.exec_module(query)


def framed(number, flags, frame, instance, ext, body):
    packet = (
        struct.pack(
            "<HiiHiii", 24 + len(ext), number, flags, instance, len(body), frame, 0
        )
        + ext
        + body
    )
    return b"\xfd" * 4 + f"{len(packet):08x}".encode() + b"\0" + packet


class Socket:
    def __init__(self, response):
        self.input = io.BytesIO(
            framed(0, 6, 2201, 0, b"\0\0", b"\0" * 9)
            + framed(0, 11, 0, 0, b"\0\0", b'{"sessionId":"test"}')
            + response
        )
        self.sent = []
        self.closed = False

    def settimeout(self, duration):
        assert duration > 0

    def recv(self, count):
        return self.input.read(min(count, 8))

    def sendall(self, data):
        self.sent.append(data)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.closed = True


class CliTests(unittest.TestCase):
    def call(self, sock, command, extra):
        out = io.StringIO()
        with (
            patch("socket.create_connection", return_value=sock),
            patch.object(Path, "read_bytes", return_value=b"public\r\n"),
            contextlib.redirect_stdout(out),
        ):
            code = query.main(
                [
                    command,
                    "--host",
                    "example.invalid",
                    "--port",
                    "9528",
                    "--app-info",
                    "example",
                    "--app-config",
                    "test",
                    "--live",
                    *extra,
                ]
            )
        self.assertTrue(sock.closed)
        self.assertEqual(code, 0)
        return json.loads(out.getvalue())["data"]

    def test_quote(self):
        meta = struct.pack("<HHHHHii", 0, 0, 1, 0xE008, 2, 4, 36103)
        data = (
            b"tb1.0\0"
            + struct.pack("<iIiHHHHBB", 2, 0, 0, 28 + len(meta), 13, 1, 0, 12, 0)
            + meta
        )
        wire = query.wire_module()
        for code, market in (("601326", "17"), ("000001", "33"), ("920679", "151")):
            with self.subTest(code=code, market=market):
                cells = b"".join(
                    text.encode("utf-16le").ljust(12, b"\0") + b"\0"
                    for text in (code, market)
                )
                sock = Socket(framed(1, 0x100, 0, 1, b"\0\xff", data + cells))
                result = self.call(sock, "quote", ["--code", code, "--market", market])
                self.assertEqual(
                    (result["stock_code"], result["market"]), (code, market)
                )
                self.assertEqual(len(sock.sent), 3)
                request = wire.unmap_body(wire.packet(sock.sent[1][13:]).body)
                self.assertIn(
                    f"stockcode={code}\r\nmarketcode={market}\r\n".encode(), request
                )
                self.assertEqual(
                    wire.unmap_body(wire.packet(sock.sent[-1][13:]).body),
                    b"[frame]\r\nid=2267\r\npageList=\r\n",
                )

    def test_ranking(self):
        cols = [(4, 12, 0), (36103, 4, 0), (55, 2, 0)]
        meta = b"\0\0" * 4 + struct.pack("<HHiHi", 2, 34055 | 8192, 0, 34056 | 8192, 1)
        raw = (
            b"tb1.0\0"
            + struct.pack("<iIiHHH", 1, 0, 0, 36 + len(meta), 21, 3)
            + b"".join(struct.pack("<HBB", *x) for x in cols)
            + meta
        )
        for text in ("000001", "33", "A"):
            raw += text.encode("utf-16le") + b"\0"
        body = bytes([len(raw), 240, len(raw) - 1]) + raw
        sock = Socket(framed(1, 0x1130, 2312, 1, b"\0\xff", body))
        page = self.call(sock, "ranking", ["--pages", "2"])
        self.assertEqual(page["returned_pages"], 1)
        self.assertEqual(page["pages"][0]["items"][0]["stock_code"], "000001")
        self.assertEqual(len(sock.sent), 2)

    def test_market_counts(self):
        import base64

        wire = query.wire_module()
        resource = {"all": {"total": {"up": 7, "down": 2}, "data": {"zt": 1, "dt": 0}}}
        raw = json.dumps(
            {
                "hs_datacenter_ztdt": [
                    {"stockcode": "1A0001", "value": json.dumps(resource)}
                ]
            }
        ).encode()
        length = len(raw)
        prefix = (
            bytes([length])
            if length < 128
            else bytes([(length & 127) | 128, length >> 7])
        )
        compressed = prefix + b"\xf0" + bytes([length - 1]) + raw
        mapped = base64.b64encode(compressed).translate(
            bytes.maketrans(wire.BASE64, wire.WIRE64)
        )
        sock = Socket(framed(1, 0x100B, 6001, 1, b"\x03\xff", mapped))
        data = self.call(sock, "market", ["--components", "breadth", "limit_counts"])
        self.assertEqual(data["breadth"]["data"]["rising"]["value"], "7")
        self.assertEqual(data["limit_counts"]["data"]["limit_down"]["value"], "0")
        self.assertEqual(data["turnover"]["status"], "not_requested")
        self.assertEqual(len(sock.sent), 2)
