"""龙虎榜 CLI 使用独立合成响应验证，不访问生产服务。"""

import contextlib
import importlib.util
import io
import json
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

spec = importlib.util.spec_from_file_location(
    "dragon_query", Path(__file__).resolve().parents[1] / "scripts/query.py"
)
query = importlib.util.module_from_spec(spec)
spec.loader.exec_module(query)
DAY = "2026-10-20"


class DragonTests(unittest.TestCase):
    def run_case(
        self, command, options, *, pending=False, missing_day=False, bad_count=False
    ):
        calls = []

        def read(args, host, path, retries, **kwargs):
            self.assertEqual(host, "data.10jqka.com.cn")
            url = urlsplit(path)
            params = parse_qs(url.query, keep_blank_values=True)
            calls.append(url.path)
            if url.path.endswith("/trade_day"):
                last = "2026-10-21" if missing_day else DAY
                data = dict(
                    last_date=last,
                    last_update_status=not pending,
                    trade_day_list=[last],
                )
            elif url.path.endswith("/list"):
                self.assertEqual(params["date"], [DAY])
                page = int(params["page"][0])
                row = dict(
                    stock_code="000123",
                    market_id="33",
                    stock_name="sample",
                    range_days=1,
                )
                data = dict(
                    page=page,
                    module="all",
                    size=50,
                    count=0 if pending else 3 if bad_count else 51,
                    stock_count=None if pending else 1,
                    items=[] if pending else [row] * (50 if page == 1 else 1),
                )
            else:
                self.assertEqual(params, {"code": ["000123"], "date": [DAY]})
                seat = dict(
                    id="seat", name="sample", rank_value=1, buy_value="123.4500"
                )
                event = dict(
                    range_type="1",
                    start_date=DAY,
                    end_date=DAY,
                    reason=[dict(id="1", content="reason")],
                    buy_items=[seat],
                    sell_items=[],
                )
                data = dict(
                    stock_code="000123",
                    market_id="33",
                    stock_name="sample",
                    details=dict(one=[event]),
                )
            return json.dumps(dict(status_code=0, succeed=True, data=data)).encode()

        out, err = io.StringIO(), io.StringIO()
        with (
            patch.object(query, "get_body", side_effect=read),
            contextlib.redirect_stdout(out),
            contextlib.redirect_stderr(err),
        ):
            status = query.main([command, "--date", DAY, "--live", *options])
        return status, json.loads(out.getvalue() or err.getvalue()), calls

    def test_calendar_must_cover_request_before_fetching_list(self):
        status, result, calls = self.run_case("dragon-list", [], missing_day=True)
        self.assertEqual((status, result["error"]["category"]), (1, "configuration"))
        self.assertEqual(len(calls), 1)

    def test_rejects_inconsistent_list_stopping_pagination(self):
        status, result, calls = self.run_case(
            "dragon-list", ["--pages", "2"], bad_count=True
        )
        self.assertEqual((status, result["error"]["category"]), (1, "protocol"))
        self.assertEqual(len(calls), 2)

    def test_pending(self):
        status, response, calls = self.run_case(
            "dragon-list", ["--pages", "2"], pending=True
        )
        self.assertEqual(status, 0)
        self.assertEqual(len(calls), 2)
        self.assertEqual(response["data"]["pages"][0]["status"], "pending")

    def test_events(self):
        status, response, calls = self.run_case("dragon-events", ["--entry-index", "0"])
        self.assertEqual(status, 0)
        self.assertEqual(len(calls), 3)
        self.assertEqual(response["data"]["events"][0]["ref"]["stock_code"], "000123")

    def test_second_page_detail(self):
        status, response, calls = self.run_case(
            "dragon-detail",
            ["--list-page", "2", "--entry-index", "0", "--event-index", "0"],
        )
        self.assertEqual(status, 0)
        self.assertEqual(len(calls), 6)
        self.assertEqual(
            response["data"]["detail"]["buy_seats"][0]["buy_value"], "123.4500"
        )

    def test_selection(self):
        for options in (
            ["--list-page", "3", "--entry-index", "0", "--event-index", "0"],
            ["--entry-index", "50", "--event-index", "0"],
            ["--entry-index", "0", "--event-index", "1"],
        ):
            status, response, _ = self.run_case("dragon-detail", options)
            self.assertEqual(
                (status, response), (1, {"error": {"category": "selection"}})
            )
