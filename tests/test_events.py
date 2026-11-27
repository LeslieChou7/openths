"""M14命令离线封装，不访问生产服务。"""

import contextlib
import importlib.util
import io
import json
import unittest
from pathlib import Path
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location(
    "events_query", Path(__file__).resolve().parents[1] / "scripts/query.py"
)
query = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(query)


class EventTests(unittest.TestCase):
    def test_live_gate(self):
        for command, kind in (
            ("stock-anomalies", "current"),
            ("trading-status-events", "suspension"),
        ):
            args = query.build_parser().parse_args([command, "--kind", kind])
            with self.assertRaises(query.QueryFailure):
                query.run(args)

    def test_commands_receive_wire_responses(self):
        for command, kind, path, payload in (
            (
                "stock-anomalies",
                "history",
                "//fuyao/stock_halt_monitor/monitor_stock/v1/list?date_type=history&page_num=1",
                {
                    "status_code": 0,
                    "data": {
                        "total_num": 1,
                        "list": [
                            {
                                "stock_code": "000001",
                                "stock_market": "33",
                                "ths_code": "000001.SZ",
                                "monitor_type": 2,
                                "start_date": 1,
                                "end_date": 2,
                            }
                        ],
                    },
                },
            ),
            (
                "trading-status-events",
                "suspension",
                "/Tfp/tpLatest/page/1/",
                [
                    {
                        "Code": "000001",
                        "Name": "A",
                        "Reason": "reason",
                        "Price": "0.00",
                        "Change": "未开盘",
                        "indexChange": "0",
                        "tpDate": "2026-01-01",
                        "fpDate": "未公布",
                    }
                ],
            ),
        ):
            output = io.StringIO()
            with (
                self.subTest(command=command),
                patch.object(query.http.client, "HTTPSConnection") as https,
                contextlib.redirect_stdout(output),
            ):
                connection = https.return_value
                response = connection.getresponse.return_value
                response.status = 200
                response.read1.side_effect = [json.dumps(payload).encode(), b""]
                self.assertEqual(
                    query.main([command, "--kind", kind, "--live", "--pages", "2"]), 0
                )
                connection.request.assert_called_once_with("GET", path)
                page = json.loads(output.getvalue())["data"]["pages"][0]
                self.assertEqual(page["items"][0]["stock_code"], "000001")
                self.assertFalse(page["next_page_available"])
                connection.close.assert_called_once()

    def test_page_bound(self):
        with self.assertRaises(SystemExit):
            query.build_parser().parse_args(
                ["stock-anomalies", "--kind", "history", "--pages", "3"]
            )
