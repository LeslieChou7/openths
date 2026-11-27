"""封装离线测试：全部数据合成，禁止网络，不证明生产兼容性。"""

import contextlib
import importlib.util
import io
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location(
    "openths_query", Path(__file__).resolve().parents[1] / "scripts" / "query.py"
)
query = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(query)

TCP = [
    "--host",
    "example.invalid",
    "--port",
    "9528",
    "--app-config",
    "unused.txt",
    "--app-info",
    "public-profile",
]


class QueryTests(unittest.TestCase):
    def setUp(self):
        for name in ("socket.socket", "socket.create_connection"):
            blocker = patch(
                name, side_effect=AssertionError("offline test attempted network")
            )
            blocker.start()
            self.addCleanup(blocker.stop)

    def invoke(self, argv):
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            try:
                code = query.main(argv)
            except SystemExit as error:
                code = error.code
        return code, stdout.getvalue(), stderr.getvalue()

    def test_capabilities_without_sdk_import(self):
        with patch.dict(sys.modules, {"pyths": None}):
            code, out, err = self.invoke(["capabilities"])
        self.assertEqual((code, err), (0, ""))
        data = json.loads(out)["data"]
        self.assertEqual(len(data["ranking_sorts"]), 5)
        self.assertEqual(data["source"], "local_skill_catalog")
        self.assertNotIn("sdk_commit", data)
        self.assertIn("sector-capabilities", data["local_commands"])
        self.assertTrue(
            {"sector-ranking", "sector-constituents"} <= set(data["live_commands"])
        )
        self.assertNotIn("staged_commands", data)

    def test_help_without_sdk(self):
        with patch.dict(sys.modules, {"pyths": None}):
            self.assertEqual(self.invoke(["--help"])[0], 0)

    def test_all_remote_commands_require_live_before_sdk_or_config(self):
        cases = [
            ["quote", "--code", "000001", "--market", "33", *TCP],
            ["ranking", *TCP],
            ["market", *TCP],
            ["hot", "--board", "stock_hour"],
            ["dragon-list", "--date", "2026-11-05"],
            ["dragon-events", "--date", "2026-11-05", "--entry-index", "0"],
            [
                "dragon-detail",
                "--date",
                "2026-11-05",
                "--entry-index",
                "0",
                "--event-index",
                "0",
            ],
        ]
        with (
            patch.object(query, "run", side_effect=AssertionError("guard bypass")),
            patch.object(Path, "read_bytes", side_effect=AssertionError("config read")),
        ):
            for argv in cases:
                with self.subTest(command=argv[0]):
                    code, out, _ = self.invoke(argv)
                    self.assertEqual((code, out), (2, ""))

    def test_boards_uses_local_catalog(self):
        code, out, err = self.invoke(["boards"])
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(
            [item["id"] for item in json.loads(out)["data"]],
            ["stock_hour", "stock_24h"],
        )

    def test_direct_runner_cannot_bypass_live_guard(self):
        args = query.build_parser().parse_args(["hot", "--board", "stock_hour"])
        with patch.object(
            query, "tcp_query", side_effect=AssertionError("client created")
        ):
            with self.assertRaises(query.QueryFailure) as error:
                query.run(args)
        self.assertEqual(error.exception.category, "configuration")

    def test_ranking_page_offset_and_source_end(self):
        pages = [
            dict(items=[{}] * 24, next_page_available=True),
            dict(items=[{}], next_page_available=False),
        ]
        with (
            patch.object(query.wire_module(), "tcp_page", side_effect=pages) as fetch,
            patch.object(Path, "read_bytes", return_value=b"profile\r\n"),
        ):
            code, out, err = self.invoke(["ranking", "--live", "--pages", "10", *TCP])
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(json.loads(out)["data"]["returned_pages"], 2)
        self.assertEqual(
            [call.args[1:] for call in fetch.call_args_list],
            [("profile\r\n", 0), ("profile\r\n", 24)],
        )

    def test_invalid_args_before_client(self):
        cases = [
            ["ranking", "--live", "--sort", "turnover", "--direction", "asc", *TCP],
            ["ranking", "--live", "--pages", "11", *TCP],
            ["ranking", "--live", "--pages", "0", *TCP],
            ["quote", "--live", "--code", "０００００１", "--market", "33", *TCP],
            ["quote", "--live", "--code", "1", "--market", "33", *TCP],
            ["market", "--live", "--components", "breadth", "breadth", *TCP],
            ["hot", "--live", "--board", "stock_hour", "--request-timeout", "nan"],
            ["hot", "--live", "--board", "stock_hour", "--request-timeout", "inf"],
            ["hot", "--live", "--board", "stock_hour", "--connect-timeout", "0"],
            ["hot", "--live", "--board", "stock_hour", "--retries", "2"],
            ["dragon-list", "--live", "--date", "2026-02-30"],
            ["dragon-detail", "--live", "--date", "2026-11-05", "--entry-index", "0"],
        ]
        with patch.object(
            query, "run", side_effect=AssertionError("invalid arguments ran")
        ):
            for argv in cases:
                with self.subTest(argv=argv):
                    self.assertEqual(self.invoke(argv)[0], 2)

    def test_market_partial_result_preserved(self):
        with (
            patch.object(
                importlib.import_module("_openths.market"),
                "market_source",
                side_effect=OSError("private"),
            ),
            patch.object(Path, "read_bytes", return_value=b"profile"),
        ):
            code, out, err = self.invoke(
                ["market", "--components", "breadth", "--allow-partial", *TCP, "--live"]
            )
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(
            json.loads(out)["data"]["breadth"]["error"], {"category": "network"}
        )

    def test_hot_unknowns_duplicates_and_returned_count(self):
        row = {"code": "000001", "rate": "1.20", "order": 1}
        body = json.dumps({"status_code": 0, "data": {"stock_list": [row, row]}})
        with patch.object(query.http.client, "HTTPSConnection") as https:
            conn = https.return_value
            response = conn.getresponse.return_value
            response.status = 200
            response.read1.side_effect = [body.encode(), b""]
            code, out, _ = self.invoke(["hot", "--live", "--board", "stock_hour"])
            conn.request.assert_called_once_with(
                "GET", "/open/api/hot_list/v1/hot_stock/a/hour/data.txt"
            )
            conn.close.assert_called_once()
        self.assertEqual(code, 0)
        data = json.loads(out)["data"]
        self.assertEqual(data["returned_count"], 2)
        self.assertIsNone(data["total"])
        self.assertIsNone(data["source_time"])
        self.assertEqual(data["items"][0]["heat"]["value"], "1.20")
        self.assertEqual(data["items"][0], data["items"][1])

    def test_transport_errors_redacted(self):
        wire = query.wire_module()
        for error, category in (
            (wire.SessionRejected, "authentication"),
            (OSError, "network"),
            (wire.WireError, "protocol"),
            (RuntimeError, "internal"),
        ):
            with (
                patch.object(wire, "tcp_page", side_effect=error("SECRET")),
                patch.object(Path, "read_bytes", return_value=b"profile"),
            ):
                code, out, err = self.invoke(["ranking", *TCP, "--live"])
            self.assertEqual((code, out), (1, ""))
            self.assertEqual(json.loads(err)["error"]["category"], category)
            self.assertNotIn("SECRET", err)

    def test_config_read_error_is_redacted_before_client(self):
        for error in (OSError("SECRET_PATH"), UnicodeError("SECRET_BYTES")):
            with patch.object(Path, "read_bytes", side_effect=error):
                code, out, err = self.invoke(["ranking", "--live", *TCP])
            self.assertEqual((code, out), (1, ""))
            self.assertEqual(json.loads(err)["error"]["category"], "configuration")
            self.assertNotIn("SECRET", err)

    def test_serialization_failure_does_not_print_object_repr(self):
        with (
            patch.object(query.wire_module(), "tcp_page", return_value=object()),
            patch.object(Path, "read_bytes", return_value=b"profile"),
        ):
            code, out, err = self.invoke(
                ["quote", "--code", "000001", "--market", "33", *TCP, "--live"]
            )
        self.assertEqual((code, out), (1, ""))
        self.assertEqual(json.loads(err)["error"]["category"], "internal")


if __name__ == "__main__":
    unittest.main()
