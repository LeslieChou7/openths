"""竞价命令通过真实入口解码合成报文。"""

import contextlib
import importlib.util
import io
import json
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

spec = importlib.util.spec_from_file_location(
    "auction_query", Path(__file__).resolve().parents[1] / "scripts/query.py"
)
query = importlib.util.module_from_spec(spec)
spec.loader.exec_module(query)


class AuctionTests(unittest.TestCase):
    def test_commands(self):
        day = datetime.now(timezone(timedelta(hours=8))).date().isoformat()
        for command, options in (
            ("auction-ranking", ["--kind", "one_price"]),
            ("auction-sector-changes", ["--side", "bearish"]),
            (
                "watch-auction-sector-changes",
                [
                    "--side",
                    "bearish",
                    "--interval",
                    "5",
                    "--max-duration",
                    "2",
                    "--max-requests",
                    "1",
                ],
            ),
        ):
            argv = [command, "--date", day, *options]
            with self.assertRaises(query.QueryFailure):
                query.run(query.build_parser().parse_args(argv))
            ranking = command == "auction-ranking"
            header = [{"code": k, "name": k, "type": "raw"} for k in ("code", "market")]
            data = (
                dict(
                    header_list=header,
                    data_list=[["000001", "33"]],
                    total=1,
                    current_page=0,
                )
                if ranking
                else dict(
                    block_list=[
                        dict(code="B1", market="48", name="sample", leader_stocks=[])
                    ]
                )
            )
            out = io.StringIO()
            with (
                self.subTest(command=command),
                patch.object(query.http.client, "HTTPSConnection") as https,
                contextlib.redirect_stdout(out),
            ):
                conn = https.return_value
                response = conn.getresponse.return_value
                response.status = 200
                response.read1.side_effect = [
                    json.dumps(dict(status_code=0, data=data)).encode(),
                    b"",
                ]
                self.assertEqual(query.main([*argv, "--live"]), 0)
                result = json.loads(out.getvalue())["data"]
                if ranking:
                    self.assertEqual(result["rows"][0]["code"], "000001")
                    self.assertEqual(
                        conn.request.call_args.args,
                        (
                            "POST",
                            "/call_auction_v2/stock_chance/v1/real_time_list/limit_board",
                        ),
                    )
                    self.assertEqual(
                        json.loads(conn.request.call_args.kwargs["body"])["sort_field"],
                        "imbalance",
                    )
                else:
                    conn.request.assert_called_once_with(
                        "GET",
                        f"/call_auction_v2/variation/v1/get_block_list/2?date={day}",
                    )
                    if command.startswith("watch-"):
                        self.assertTrue(result[0]["baseline"])
                        result = result[0]["snapshot"]
                    self.assertEqual(result["sectors"][0]["market"], "48")
                conn.close.assert_called_once()
