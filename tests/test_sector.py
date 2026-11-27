"""板块业务按 CLI 请求、来源引用与分页进行离线验证。"""

import contextlib
import importlib.util
import io
import json
import unittest
from pathlib import Path
from unittest.mock import patch

spec = importlib.util.spec_from_file_location(
    "sectors", Path(__file__).resolve().parents[1] / "scripts/query.py"
)
query = importlib.util.module_from_spec(spec)
spec.loader.exec_module(query)


class SectorTests(unittest.TestCase):
    def execute(self, command, extra=(), *, missing=False):
        requests = []

        def read(args, host, path, retries, *, payload, accept):
            self.assertEqual(host, "dq.10jqka.com.cn")
            self.assertEqual(retries, 0)
            self.assertIn("application/json", accept)
            requests.append(payload)
            if "type" in payload:
                self.assertTrue(path.endswith("/get_block_list"))
                data = dict(
                    total=1,
                    list=[
                        dict(
                            block_code="881100" if missing else "881146",
                            block_market="48",
                            block_name="sample",
                        )
                    ],
                )
            else:
                self.assertTrue(path.endswith("/get_stock_list_by_block"))
                self.assertEqual(payload["block_code"], "881146")
                page = payload["page_info"]["page"]
                data = dict(
                    total=27,
                    list=[
                        dict(
                            stock_code=f"{n:06d}",
                            stock_market="33",
                            margin_of_increase=1.25,
                        )
                        for n in range(20 if page == 1 else 7)
                    ],
                )
            return json.dumps(dict(status_code=0, data=data)).encode()

        out, err = io.StringIO(), io.StringIO()
        with (
            patch.object(query, "get_body", side_effect=read),
            contextlib.redirect_stdout(out),
            contextlib.redirect_stderr(err),
        ):
            status = query.main([command, "--date", "2026-11-12", "--live", *extra])
        return status, json.loads(out.getvalue() or err.getvalue()), requests

    def test_ranking(self):
        status, output, calls = self.execute("sector-ranking")
        self.assertEqual(status, 0)
        self.assertEqual(calls[0]["history_info"]["history_type"], "1")
        self.assertEqual(
            output["data"]["pages"][0]["items"][0]["sector"]["code"], "881146"
        )

    def test_constituents(self):
        status, output, calls = self.execute("sector-constituents", ["--pages", "2"])
        self.assertEqual(status, 0)
        self.assertEqual([p["page_info"]["page"] for p in calls], [1, 1, 2])
        first, last = output["data"]["constituent_pages"]
        self.assertTrue(first["next_page_available"])
        self.assertFalse(last["has_more"])
        self.assertEqual(last["returned_count"], 7)
        self.assertEqual(last["items"][0]["values"]["change_rate"], "1.25")

    def test_missing_selection(self):
        status, output, calls = self.execute("sector-constituents", missing=True)
        self.assertEqual(
            (status, output, len(calls)), (1, {"error": {"category": "selection"}}, 1)
        )

    def test_unsupported_period(self):
        status, _, calls = self.execute(
            "sector-ranking",
            ["--mode", "intraday", "--start-time", "10:00", "--end-time", "10:30"],
        )
        self.assertEqual((status, calls), (1, []))

    def test_missing_time(self):
        status, _, calls = self.execute("sector-ranking", ["--mode", "intraday"])
        self.assertEqual((status, calls), (1, []))
