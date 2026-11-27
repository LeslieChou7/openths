#!/usr/bin/env python3
"""OpenTHS 独立只读行情 JSON 入口。"""

from __future__ import annotations

import argparse
import http.client
import importlib
import importlib.util
import json
import math
import re
import sys
from collections.abc import Mapping
from dataclasses import fields, is_dataclass
from datetime import UTC, date, datetime, time, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from time import monotonic, sleep
from typing import Any
from urllib.parse import urlencode


def _load_package():
    name = "_openths"
    directory = Path(__file__).resolve().with_name(name)
    loaded = sys.modules.get(name)
    if loaded is not None and Path(loaded.__file__).parent != directory:
        # Each checkout can be exercised in the same research process.
        for key in tuple(sys.modules):
            if key == name or key.startswith(name + "."):
                del sys.modules[key]
        loaded = None
    if loaded is None:
        spec = importlib.util.spec_from_file_location(name, directory / "__init__.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)


_load_package()
_common = importlib.import_module("_openths.common")
_hot = importlib.import_module("_openths.hot")
_events = importlib.import_module("_openths.events")
_dragon = importlib.import_module("_openths.dragon")
QUOTE_MARKETS = importlib.import_module("_openths.quote").QUOTE_MARKETS
QueryFailure = _common.QueryFailure
unique_object = _common.unique_object
invalid_number = _common.invalid_number
decode_rows = _hot.decode_rows


SORTS = (
    "change_rate",
    "turnover",
    "rapid_change_rate",
    "large_order_net_volume",
    "main_net_inflow",
)
COMPONENTS = ("breadth", "limit_counts", "turnover", "fund_flow", "size_comparison")
AUCTION_METHODS = {
    "auction-ranking": "get_auction_ranking",
    "auction-sector-changes": "get_auction_sector_changes",
    "watch-auction-sector-changes": "watch_auction_sector_changes",
}
TCP_COMMANDS = frozenset(("quote", "ranking", "market"))
LOCAL_COMMANDS = frozenset(
    ("capabilities", "boards", "sector-capabilities", "limit-capabilities")
)


def positive_timeout(value: str) -> float:
    """解析有限的正秒数。"""
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise argparse.ArgumentTypeError("超时须为有限正数")
    return number


def bounded_pages(value: str) -> int:
    """限制本工具的显式分页工作量。"""
    number = int(value)
    if not 1 <= number <= 10:
        raise argparse.ArgumentTypeError("页数须在 1..10 内")
    return number


def sector_pages(value: str) -> int:
    number = bounded_pages(value)
    if number > 2:
        raise argparse.ArgumentTypeError("板块最多显式查询两页，具体范围由SDK限制")
    return number


def nonnegative_index(value: str) -> int:
    """解析从零开始的显式选择位置。"""
    number = int(value)
    if number < 0:
        raise argparse.ArgumentTypeError("序号须大于等于零")
    return number


def stock_code(value: str) -> str:
    """保留前导零，仅接受 SDK 支持的代码编码。"""
    if len(value) != 6 or not value.isascii() or not value.isdigit():
        raise argparse.ArgumentTypeError("证券代码须为六位 ASCII 数字字符串")
    return value


def build_parser() -> argparse.ArgumentParser:
    """构建命令行；无解析副作用。"""
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("capabilities", help="本封装静态能力目录，不需 SDK")
    for name in (
        "boards",
        "quote",
        "ranking",
        "market",
        "hot",
        "dragon-list",
        "dragon-events",
        "dragon-detail",
        "sector-capabilities",
        "sector-ranking",
        "sector-constituents",
        "stock-anomalies",
        "trading-status-events",
        "limit-capabilities",
        "limit-summary",
        "limit-pool",
        *AUCTION_METHODS,
    ):
        command = commands.add_parser(name)
        command.add_argument("--connect-timeout", type=positive_timeout, default=5.0)
        command.add_argument("--request-timeout", type=positive_timeout, default=15.0)
        command.add_argument("--retries", type=int, choices=(0, 1), default=0)
        if name not in LOCAL_COMMANDS:
            command.add_argument("--live", action="store_true")
        if name in AUCTION_METHODS:
            command.add_argument("--date", type=date.fromisoformat, required=True)
            if name == "auction-ranking":
                command.add_argument(
                    "--kind",
                    choices=("one_price", "yesterday_limit_up", "weak_to_strong"),
                    required=True,
                )
            else:
                command.add_argument(
                    "--side", choices=("bullish", "bearish"), required=True
                )
            if name == "watch-auction-sector-changes":
                command.add_argument("--interval", type=positive_timeout, required=True)
                command.add_argument(
                    "--max-duration", type=positive_timeout, required=True
                )
                command.add_argument("--max-requests", type=int, required=True)
        if name in TCP_COMMANDS:
            command.add_argument("--host", required=True)
            command.add_argument(
                "--port",
                required=True,
                type=int,
                choices=range(1, 65536),
                metavar="PORT",
            )
            command.add_argument("--app-config", required=True, type=Path)
            command.add_argument("--app-info", required=True)
        if name in ("limit-summary", "limit-pool"):
            command.add_argument("--date", type=date.fromisoformat, required=True)
            command.add_argument(
                "--markets",
                nargs="+",
                choices=("HS", "GEM2STAR"),
                default=("HS", "GEM2STAR"),
            )
            command.add_argument("--include-st", action="store_true")
            command.add_argument("--include-unopened", action="store_true")
        if name == "limit-summary":
            command.add_argument("--allow-partial", action="store_true")
        if name == "limit-pool":
            command.add_argument(
                "--pool",
                required=True,
                choices=(
                    "limit_up",
                    "approaching_limit_up",
                    "consecutive_limit_up",
                    "broken_limit_up",
                    "limit_down",
                ),
            )
            command.add_argument("--sort", choices=("change_rate", "latest"))
            command.add_argument("--direction", choices=("asc", "desc"))
            command.add_argument("--pages", type=int, choices=(1, 2), default=1)
        if name in ("stock-anomalies", "trading-status-events"):
            command.add_argument(
                "--kind",
                required=True,
                choices=("current", "history")
                if name == "stock-anomalies"
                else ("suspension", "resumption"),
            )
            command.add_argument("--pages", type=sector_pages, default=1)
        if name == "quote":
            command.add_argument("--code", required=True, type=stock_code)
            command.add_argument("--market", required=True, choices=QUOTE_MARKETS)
        if name in ("sector-ranking", "sector-constituents"):
            command.add_argument("--date", type=date.fromisoformat, required=True)
            command.add_argument(
                "--category",
                choices=("all", "industry", "concept", "style", "region"),
                default="industry",
            )
            command.add_argument(
                "--mode",
                choices=("historical_day", "intraday", "multi_day"),
                default="historical_day",
            )
            command.add_argument("--end-date", type=date.fromisoformat)
            command.add_argument("--start-time", type=time.fromisoformat)
            command.add_argument("--end-time", type=time.fromisoformat)
            command.add_argument(
                "--sort",
                choices=("change_rate", "main_net_inflow", "turnover"),
                default="change_rate",
            )
            command.add_argument("--direction", choices=("asc", "desc"), default="desc")
            command.add_argument("--pages", type=sector_pages, default=1)
        if name == "sector-constituents":
            command.add_argument("--sector-code", default="881146")
            command.add_argument("--sector-market", default="48")
            command.add_argument("--ranking-pages", type=sector_pages, default=1)
            command.add_argument(
                "--ranking-sort",
                choices=("change_rate", "main_net_inflow", "turnover"),
                default="change_rate",
            )
            command.add_argument(
                "--ranking-direction", choices=("asc", "desc"), default="desc"
            )
        if name == "ranking":
            command.add_argument("--sort", choices=SORTS, default="change_rate")
            command.add_argument("--direction", choices=("asc", "desc"), default="desc")
        if name in ("ranking", "dragon-list"):
            command.add_argument("--pages", type=bounded_pages, default=1)
        if name == "market":
            command.add_argument(
                "--components", nargs="+", choices=COMPONENTS, default=COMPONENTS
            )
            command.add_argument("--allow-partial", action="store_true")
        if name == "hot":
            command.add_argument(
                "--board", choices=("stock_hour", "stock_24h"), required=True
            )
        if name.startswith("dragon-"):
            command.add_argument("--date", type=date.fromisoformat, required=True)
        if name in ("dragon-events", "dragon-detail"):
            command.add_argument("--list-page", type=bounded_pages, default=1)
            command.add_argument("--entry-index", type=nonnegative_index, required=True)
        if name == "dragon-detail":
            command.add_argument("--event-index", type=nonnegative_index, required=True)
    return parser


def to_json(value: Any) -> Any:
    """保留公共数据语义，避免递归导出客户端和不透明游标。"""
    if is_dataclass(value) and not isinstance(value, type):
        result = {}
        for field in fields(value):
            if field.name.startswith("_"):
                continue
            item = getattr(value, field.name)
            if field.name == "next_page":
                result["next_page_available"] = item is not None
            else:
                result[field.name] = to_json(item)
        if hasattr(value, "source_data_null"):
            result["source_data_null"] = value.source_data_null
        if hasattr(value, "returned_count"):
            result["returned_count"] = value.returned_count
        return result
    if isinstance(value, (date, datetime, time)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (tuple, list)):
        return [to_json(item) for item in value]
    if isinstance(value, Mapping):
        return {key: to_json(item) for key, item in value.items()}
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise TypeError("unsupported public result type")


def capabilities() -> dict[str, Any]:
    """返回本地静态目录，不能当作服务端发现。"""
    return {
        "source": "local_skill_catalog",
        "local_commands": [
            "capabilities",
            "boards",
            "sector-capabilities",
            "limit-capabilities",
        ],
        "live_commands": [
            "quote",
            "ranking",
            "market",
            "hot",
            "dragon-list",
            "dragon-events",
            "dragon-detail",
            "sector-ranking",
            "sector-constituents",
            "stock-anomalies",
            "trading-status-events",
            "limit-summary",
            "limit-pool",
            *AUCTION_METHODS,
        ],
        "tcp_configuration_required": sorted(TCP_COMMANDS),
        "quote_markets": QUOTE_MARKETS,
        "ranking_sorts": [
            {
                "sort": item,
                "directions": ["asc", "desc"] if item == "change_rate" else ["desc"],
            }
            for item in SORTS
        ],
        "market_components": COMPONENTS,
        "hot_boards": ["stock_hour", "stock_24h"],
        "default_pages": 1,
        "max_pages_per_command": 10,
        "scope_reference": "references/scope.md",
    }


def get_body(args, host, path, retries, *, payload=None, until=None, accept=None):
    deadline = monotonic() + args.request_timeout
    if until is not None:
        deadline = min(deadline, until)
    for attempt in range(retries + 1):
        remaining = deadline - monotonic()
        if remaining <= 0:
            raise QueryFailure("network")
        conn = http.client.HTTPSConnection(
            host, timeout=min(remaining, args.connect_timeout)
        )
        try:
            if payload is None:
                conn.request("GET", path)
            else:
                conn.request(
                    "POST",
                    path,
                    body=json.dumps(payload).encode(),
                    headers={
                        "Content-Type": "application/json",
                        **({"Accept": accept} if accept else {}),
                    },
                )
            response = conn.getresponse()
            if response.status in (401, 403):
                raise QueryFailure("authentication")
            if response.status == 429:
                raise QueryFailure("rate_limit")
            if response.status != 200:
                raise QueryFailure("network")
            chunks = bytearray()
            while len(chunks) <= 4 * 1024 * 1024:
                remaining = deadline - monotonic()
                if remaining <= 0:
                    raise QueryFailure("network")
                if conn.sock:
                    conn.sock.settimeout(remaining)
                chunk = response.read1(65536)
                if not chunk:
                    break
                chunks.extend(chunk)
            else:
                raise QueryFailure("protocol")
            return bytes(chunks)
        except (ValueError, RecursionError):
            raise QueryFailure("protocol") from None
        except (OSError, http.client.HTTPException):
            if attempt == retries:
                raise QueryFailure("network") from None
        finally:
            conn.close()
    raise QueryFailure("network")


def auction_snapshot(args, until=None):
    ranking = args.command == "auction-ranking"
    payload = None
    if ranking:
        if args.date != datetime.now(timezone(timedelta(hours=8))).date():
            raise QueryFailure("configuration")
        route, sort = {
            "one_price": ("limit_board", "imbalance"),
            "yesterday_limit_up": ("yesterday_limit_up", ""),
            "weak_to_strong": ("weak_to_strong", "warnTimestamp"),
        }[args.kind]
        path = "/call_auction_v2/stock_chance/v1/real_time_list/" + route
        payload = dict(
            date=args.date.isoformat(),
            page_num=1,
            page_size=20,
            sort=0,
            sort_field=sort,
            filter=None,
        )
    else:
        side = 1 if args.side == "bullish" else 2
        path = f"/call_auction_v2/variation/v1/get_block_list/{side}?date={args.date.isoformat()}"
    raw = get_body(args, "eq.10jqka.com.cn", path, 0, payload=payload, until=until)
    try:
        envelope = json.loads(
            raw.decode("utf-8"),
            parse_float=Decimal,
            object_pairs_hook=unique_object,
            parse_constant=invalid_number,
        )
        if type(envelope["status_code"]) is not int:
            raise ValueError
        if envelope["status_code"]:
            raise QueryFailure("business")
        data = envelope["data"]
        result = dict(request_date=args.date, received_at=datetime.now(UTC))
        if ranking:
            result.update(
                kind=args.kind,
                columns=[],
                rows=[],
                total=None,
                current_page=None,
                source_data_null=data is None,
            )
            if data is None:
                return result
            columns = [
                dict(code=h["code"], name=h["name"], type_raw=h["type"])
                for h in data["header_list"]
            ]
            keys = [c["code"] for c in columns]
            if len(keys) != len(set(keys)) or any(
                type(v) is not str for c in columns for v in c.values()
            ):
                raise ValueError
            rows = []
            for cells in data["data_list"]:
                if not isinstance(cells, list):
                    raise ValueError
                row = dict(zip(keys, cells, strict=True))
                if any(type(row[k]) is not str for k in ("market", "code")):
                    raise ValueError
                rows.append(row)
            if any(
                type(data[k]) is not int or data[k] < 0
                for k in ("total", "current_page")
            ):
                raise ValueError
            result.update(
                columns=columns,
                rows=rows,
                total=data["total"],
                current_page=data["current_page"],
            )
        else:
            sectors = []
            if not isinstance(data["block_list"], list):
                raise ValueError
            for block in data["block_list"]:
                identity = {k: block[k] for k in ("market", "code", "name")}
                if any(type(v) is not str for v in identity.values()):
                    raise ValueError
                leaders = block.get("leader_stocks")
                if leaders is not None and not isinstance(leaders, list):
                    raise ValueError
                fields = {
                    k: v
                    for k, v in block.items()
                    if k not in identity and k != "leader_stocks"
                }
                sectors.append(dict(**identity, fields=fields, leader_stocks=leaders))
            result.update(side=args.side, sectors=sectors)
        return result
    except (ValueError, KeyError, TypeError, RecursionError):
        raise QueryFailure("protocol") from None


def fetch_auction(args):
    if args.retries:
        raise QueryFailure("configuration")
    if args.command != "watch-auction-sector-changes":
        return auction_snapshot(args)
    if not (
        5 <= args.interval <= 600
        and 0 < args.max_duration <= 600
        and 1 <= args.max_requests <= 120
    ):
        raise QueryFailure("configuration")
    end = monotonic() + args.max_duration
    observations = []
    previous = {}
    for index in range(args.max_requests):
        if index:
            if monotonic() + args.interval >= end:
                break
            sleep(args.interval)
        if monotonic() >= end:
            break
        snapshot = auction_snapshot(args, end)
        current = {(row["market"], row["code"]): row for row in snapshot["sectors"]}
        if len(current) != len(snapshot["sectors"]):
            raise QueryFailure("protocol")
        observations.append(
            dict(
                snapshot=snapshot,
                baseline=index == 0,
                entered=[
                    row for key, row in current.items() if index and key not in previous
                ],
                updated=[
                    row
                    for key, row in current.items()
                    if key in previous and previous[key] != row
                ],
                left=[row for key, row in previous.items() if key not in current],
            )
        )
        previous = current
    return observations


def limit_query(args):
    assets = Path(__file__).resolve().parents[1] / "assets"
    specs = json.loads((assets / "limit-requests.json").read_text(encoding="utf-8"))
    scopes = json.loads(
        (assets / "limit-capabilities.json").read_text(encoding="utf-8")
    )["public_verified_scopes"]
    if len(set(args.markets)) != len(args.markets) or args.retries:
        raise QueryFailure("configuration")
    filters = dict(
        markets=args.markets,
        include_st=args.include_st,
        include_unopened=args.include_unopened,
    )
    tokens = [m for m in ("HS", "GEM2STAR") if m in args.markets]
    if args.include_st:
        tokens.append("ST")
    if args.include_unopened:
        tokens.append("NEW")
    filter_text = ",".join(tokens)
    day = args.date.strftime("%Y%m%d")
    summary = args.command == "limit-summary"
    defaults = dict(
        limit_up="330324",
        limit_down="330334",
        approaching_limit_up="199112",
        broken_limit_up="199112",
        consecutive_limit_up="330329",
    )
    if summary:
        choice = specs["summaries"].get(day + "/" + filter_text)
        if choice is None:
            raise QueryFailure("configuration")
        pool, order, direction = choice
    else:
        pool = args.pool
        order = {"latest": "10", "change_rate": "199112", None: defaults[pool]}[
            args.sort
        ]
        direction = "1" if args.direction == "asc" else "0"
    route = specs["routes"][pool]
    scope = [pool, day, filter_text, order, direction]
    query = dict(
        pool=pool,
        trade_date=args.date,
        filters=filters,
        sort_field=(
            None
            if (summary and order == defaults[pool] and direction == "0")
            or (not summary and args.sort is None)
            else order
        ),
        order_type=direction,
    )
    pages = []
    count = 1 if summary else args.pages
    for page in range(1, count + 1):
        if [*scope, str(page)] not in scopes:
            raise QueryFailure("configuration")
        params = dict(
            page=page,
            limit=15,
            field=route["fields"],
            filter=filter_text,
            date=day,
            order_field=order,
            order_type=direction,
        )
        raw = get_body(
            args, "data.10jqka.com.cn", route["path"] + "?" + urlencode(params), 0
        )
        wire = json.loads(
            raw.decode(),
            parse_float=Decimal,
            object_pairs_hook=unique_object,
            parse_constant=invalid_number,
        )
        if type(wire["status_code"]) is not int:
            raise ValueError("status")
        if wire["status_code"]:
            raise QueryFailure("business")
        data = wire["data"]
        meta = data["page"]
        total, size, source_page, page_count = (
            meta[k] for k in ("total", "limit", "page", "count")
        )
        if (
            any(
                type(n) is not int or n < 0
                for n in (total, size, source_page, page_count)
            )
            or size != 15
        ):
            raise ValueError("page")
        if (
            data.get("date") not in (day, None)
            or source_page != page
            and not (page == 1 and source_page == total == 0)
        ):
            raise ValueError("context")
        items = []
        for row in data["info"]:
            if not isinstance(row["code"], str) or not re.fullmatch(
                r"[0-9]{6}", row["code"]
            ):
                raise ValueError("code")
            market = row.get("market_id")
            items.append(
                dict(
                    code=row["code"],
                    market=None if market is None else str(market),
                    name=row.get("name"),
                    fields=row,
                )
            )
        common = dict(
            query=query,
            source_date_raw=data.get("date"),
            received_at=datetime.now(UTC),
            source="limit_focus_mobile_v2",
            warnings=[],
        )
        stats = data["limit_up_count"]
        if summary:
            for side in ("today", "yesterday"):
                values = stats.get(side) or {}
                result = {key: values.get(key) for key in ("num", "open_num", "rate")}
                if result["rate"] is not None:
                    result["rate"] = Decimal(str(result["rate"]))
                if None in result.values():
                    if not args.allow_partial:
                        raise ValueError("incomplete summary")
                    common["warnings"] = ["incomplete_summary"]
                common[side] = result
            return dict(**common, yesterday_date_raw=None, source_time=None)
        more = page < page_count
        expected = max(0, min(size, total - (page - 1) * size))
        if len(items) != expected or page_count != (total + size - 1) // size:
            more = None
            common["warnings"].append("source_page_inconsistent")
        available = more is True and [*scope, str(page + 1)] in scopes
        pages.append(
            dict(
                **common,
                items=items,
                total=total,
                page=page,
                page_count=page_count,
                page_size=size,
                has_more=more,
                next_page_available=available,
                summary_raw=stats,
                source_page=source_page,
                returned_count=len(items),
            )
        )
        if not available:
            break
    return dict(pages=pages, returned_pages=len(pages), requested_pages=count)


def sector_query(args):
    source = "sector_statistics_v1"
    scopes = set(
        json.loads(
            (
                Path(__file__).resolve().parents[1]
                / "assets"
                / "sector-capabilities.json"
            ).read_text(encoding="utf-8")
        )["verified_business_scopes"]
    )
    end_day = args.end_date or args.date
    period = dict(
        mode=args.mode,
        start_date=args.date,
        end_date=end_day,
        start_time=args.start_time,
        end_time=args.end_time,
    )
    for t in (args.start_time, args.end_time):
        if t is not None and (t.tzinfo is not None or t.microsecond):
            raise QueryFailure("configuration")
    history = dict(
        history_type="0" if args.mode == "intraday" else "1",
        start_date=args.date.strftime("%Y%m%d")
        + (args.start_time or time()).strftime("%H%M%S"),
        end_date=end_day.strftime("%Y%m%d")
        + (args.end_time or time()).strftime("%H%M%S"),
    )

    def pages_for(ref, sort, direction, count):
        block = ref is None
        kind = "block" if block else "stock"
        category = args.category if block else None
        query = dict(
            kind=kind,
            period=period,
            sort=sort,
            direction=direction,
            category=category,
            sector=ref,
        )
        if block:
            identity = dict(
                type=("all", "industry", "concept", "style", "region").index(category)
            )
            route = "block_info/v1/get_block_list"
        else:
            identity = dict(block_code=ref["code"], block_market=ref["market"])
            route = "stock_info/v1/get_stock_list_by_block"
        prefix = "/".join(
            [
                kind,
                category or "",
                identity.get("block_code", ""),
                identity.get("block_market", ""),
                *history.values(),
                sort,
                direction,
            ]
        )
        pages = []
        for page in range(1, count + 1):
            if f"{prefix}/{page}/size20" not in scopes:
                raise QueryFailure("configuration")
            body = dict(
                **identity,
                history_info=history,
                page_info=dict(page=page, page_size=20),
                sort_info=dict(
                    sort_field={
                        "change_rate": "0",
                        "main_net_inflow": "1",
                        "turnover": "2",
                    }[sort],
                    sort_type=direction,
                ),
            )
            raw = get_body(
                args,
                "dq.10jqka.com.cn",
                "/interval_calculation/" + route,
                0,
                payload=body,
                accept="application/json, text/plain, */*",
            )
            wire = json.loads(
                raw.decode(),
                parse_float=Decimal,
                object_pairs_hook=unique_object,
                parse_constant=invalid_number,
            )
            if type(wire["status_code"]) is not int:
                raise ValueError("status")
            if wire["status_code"]:
                raise QueryFailure("business")
            data = wire["data"]
            total = data["total"]
            if (
                type(total) is not int
                or total < 0
                or not isinstance(data["list"], list)
                or len(data["list"]) > 20
            ):
                raise ValueError("page")
            items = []
            for row in data["list"]:
                code, market = row[kind + "_code"], row[kind + "_market"]
                if not all(
                    isinstance(v, str) and re.fullmatch(r"[0-9]{1,32}", v)
                    for v in (code, market)
                ):
                    raise ValueError("identity")
                values = dict(code=code, market=market, name=row.get(kind + "_name"))
                for key, wire_key in (
                    ("change_rate", "margin_of_increase"),
                    ("main_net_inflow", "net_inflow_of_main_force"),
                    ("turnover", "turnover"),
                    ("turnover_rate", "turnover_rate"),
                ):
                    value = row.get(wire_key)
                    if value is not None:
                        value = Decimal(str(value))
                        if not value.is_finite():
                            raise ValueError("metric")
                    values[key] = value
                items.append(
                    dict(
                        values=values,
                        sector=dict(code=code, market=market, source=source)
                        if block
                        else None,
                    )
                )
            seen = (page - 1) * 20 + len(items)
            warnings = []
            if seen > total or (not items and seen < total):
                more = None
                warnings.append("source_total_and_page_inconsistent")
            elif seen == total:
                more = False
            elif len(items) == 20:
                more = True
            else:
                more = None
                warnings.append("short_page_before_source_total")
            available = more is True and f"{prefix}/{page + 1}/size20" in scopes
            pages.append(
                dict(
                    query=query,
                    items=items,
                    total=total,
                    page=page,
                    received_at=datetime.now(UTC),
                    has_more=more,
                    next_page_available=available,
                    warnings=warnings,
                    source=source,
                    source_time=None,
                    page_size=20,
                    returned_count=len(items),
                )
            )
            if not available:
                break
        return pages

    if args.command == "sector-ranking":
        pages = pages_for(None, args.sort, args.direction, args.pages)
        return dict(pages=pages, requested_pages=args.pages, returned_pages=len(pages))
    ranking = pages_for(
        None, args.ranking_sort, args.ranking_direction, args.ranking_pages
    )
    refs = [
        item["sector"]
        for page in ranking
        for item in page["items"]
        if (item["values"]["code"], item["values"]["market"])
        == (args.sector_code, args.sector_market)
    ]
    if len(refs) != 1:
        raise QueryFailure("selection")
    pages = pages_for(refs[0], args.sort, args.direction, args.pages)
    return dict(
        ranking_pages=ranking,
        constituent_pages=pages,
        requested_constituent_pages=args.pages,
        returned_constituent_pages=len(pages),
    )


def wire_module():
    return importlib.import_module("_openths.wire")


def tcp_query(args):
    wire = wire_module()
    try:
        config = args.app_config.read_bytes().decode("utf-8")
    except (OSError, UnicodeError):
        raise QueryFailure("configuration") from None
    try:
        if args.command == "market":
            return wire.market_overview(args, config)
        if args.command == "quote":
            return wire.tcp_page(args, config)
        pages = []
        start = 0
        for _ in range(args.pages):
            page = wire.tcp_page(args, config, start)
            pages.append(page)
            if not page["next_page_available"]:
                break
            start += len(page["items"])
        return dict(pages=pages, requested_pages=args.pages, returned_pages=len(pages))
    except wire.SessionRejected:
        raise QueryFailure("authentication") from None
    except (EOFError, OSError):
        raise QueryFailure("network") from None
    except (ValueError, KeyError, TypeError):
        raise QueryFailure("protocol") from None


def run(args: argparse.Namespace) -> Any:
    """在联网门禁与参数检查后加载 SDK、读取配置并释放客户端。"""
    if args.command == "capabilities":
        return capabilities()
    if args.command in LOCAL_COMMANDS:
        catalog = (
            Path(__file__).resolve().parents[1] / "assets" / f"{args.command}.json"
        )
        return json.loads(catalog.read_text(encoding="utf-8"))
    if args.command not in LOCAL_COMMANDS and not getattr(args, "live", False):
        raise QueryFailure("configuration")
    if args.command == "hot":
        return _hot.fetch_hot(args, get_body)
    if args.command in ("stock-anomalies", "trading-status-events"):
        return _events.fetch_events(args, get_body)
    if args.command in AUCTION_METHODS:
        return fetch_auction(args)
    if args.command in ("limit-summary", "limit-pool"):
        try:
            return limit_query(args)
        except (ValueError, TypeError, KeyError, InvalidOperation, RecursionError):
            raise QueryFailure("protocol") from None
    if args.command.startswith("dragon-"):
        try:
            return _dragon.dragon_query(args, get_body)
        except (KeyError, ValueError, InvalidOperation, TypeError, RecursionError):
            raise QueryFailure("protocol") from None
    if args.command in ("sector-ranking", "sector-constituents"):
        times = (args.start_time, args.end_time)
        if (
            (
                args.mode == "historical_day"
                and (args.end_date is not None or any(t is not None for t in times))
            )
            or (
                args.mode == "intraday"
                and (args.end_date is not None or any(t is None for t in times))
            )
            or (
                args.mode == "multi_day"
                and (args.end_date is None or any(t is not None for t in times))
            )
        ):
            raise QueryFailure("configuration")
        if args.command == "sector-constituents" and any(
            not value.isascii() or not value.isdigit() or not 1 <= len(value) <= 32
            for value in (args.sector_code, args.sector_market)
        ):
            raise QueryFailure("configuration")
    if args.command in ("limit-summary", "limit-pool") and (
        len(set(args.markets)) != len(args.markets) or args.retries != 0
    ):
        raise QueryFailure("configuration")
    if args.command in ("sector-ranking", "sector-constituents"):
        try:
            return sector_query(args)
        except (ValueError, InvalidOperation, KeyError, TypeError, RecursionError):
            raise QueryFailure("protocol") from None
    return tcp_query(args)


def main(argv: list[str] | None = None) -> int:
    """执行命令并输出一个 JSON 文档；错误不含请求内容。"""
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command not in LOCAL_COMMANDS and not args.live:
        parser.error("必须显式传入 --live 才读取配置或请求真实服务")
    if (
        args.command == "ranking"
        and args.sort != "change_rate"
        and args.direction != "desc"
    ):
        parser.error("此排序仅支持 desc")
    if args.command == "market" and len(set(args.components)) != len(args.components):
        parser.error("components 不可重复")
    try:
        data = to_json(run(args))
        output = json.dumps(
            {"command": args.command, "data": data}, ensure_ascii=False, allow_nan=False
        )
    except QueryFailure as error:
        print(json.dumps({"error": {"category": error.category}}), file=sys.stderr)
        return 1
    except Exception:  # noqa: BLE001 -- 序列化等意外失败也不能泄露对象内容。
        print(json.dumps({"error": {"category": "internal"}}), file=sys.stderr)
        return 1
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
