"""M15封装离线测试，默认不访问网络。"""

import contextlib
import importlib.util
import io
import json
import unittest
from decimal import Decimal
from pathlib import Path
from types import MappingProxyType
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location(
    "limit_query", Path(__file__).resolve().parents[1] / "scripts/query.py"
)
query = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(query)


class LimitTests(unittest.TestCase):
    def test_live_gate(self):
        args = query.build_parser().parse_args(
            ["limit-pool", "--pool", "limit_down", "--date", "2026-11-13"]
        )
        with self.assertRaises(query.QueryFailure):
            query.run(args)

    def test_commands(self):
        for name in ("limit-pool", "limit-summary"):
            options = ["--pool", "limit_up"] if name == "limit-pool" else []
            stats = {"num": 1, "open_num": 0, "rate": 1}
            wire = dict(
                status_code=0,
                data=dict(
                    date="20261117",
                    page=dict(total=1, count=1, page=1, limit=15),
                    info=[dict(code="000123", market_id=33, name="sample")],
                    limit_up_count=dict(today=stats, yesterday=stats),
                ),
            )
            out = io.StringIO()
            with (
                self.subTest(name=name),
                patch.object(
                    query, "get_body", return_value=json.dumps(wire).encode()
                ) as read,
                contextlib.redirect_stdout(out),
            ):
                self.assertEqual(
                    query.main([name, "--date", "2026-11-17", "--live", *options]), 0
                )
                self.assertTrue(
                    read.call_args.args[2].startswith(
                        "/dataapi/limit_up/limit_up_pool?"
                    )
                )
                data = json.loads(out.getvalue())["data"]
                if name == "limit-summary":
                    self.assertEqual(data["today"]["rate"], "1")
                else:
                    self.assertEqual(data["pages"][0]["items"][0]["code"], "000123")

    def test_unsupported_scope(self):
        args = query.build_parser().parse_args(
            ["limit-summary", "--date", "2025-01-01", "--live"]
        )
        with (
            patch.object(query, "get_body", side_effect=AssertionError),
            self.assertRaises(query.QueryFailure) as error,
        ):
            query.run(args)
        self.assertEqual(error.exception.category, "configuration")

    def test_capabilities_local_and_mapping_semantics(self):
        args = query.build_parser().parse_args(["limit-capabilities"])
        self.assertEqual(len(query.run(args)["public_verified_scopes"]), 24)
        self.assertEqual(
            query.to_json(
                MappingProxyType({"ratio": Decimal("0.50"), "unknown": None})
            ),
            {"ratio": "0.50", "unknown": None},
        )

    def test_reject_page_three(self):
        with self.assertRaises(SystemExit):
            query.build_parser().parse_args(
                [
                    "limit-pool",
                    "--pool",
                    "limit_down",
                    "--date",
                    "2026-11-13",
                    "--pages",
                    "3",
                ]
            )
