import argparse
import json
import math
import re
from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Context, Decimal

from .common import QueryFailure, invalid_number, unique_object


class HotBusinessError(ValueError):
    """来源报告业务失败。"""


def _rank(value, minimum, maximum):
    if value is None:
        return None
    if isinstance(value, str) and re.fullmatch(r"-?[0-9]{1,10}", value):
        value = int(value)
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError("rank")
    return value


def _heat(value):
    if value is None:
        return None
    if not isinstance(value, (str, int, Decimal)) or isinstance(value, bool):
        raise ValueError("rate")
    raw = str(value)
    if len(raw) > 128 or not re.fullmatch(r"[+-]?[0-9]+(?:[.][0-9]+)?", raw):
        raise ValueError("rate")
    value = Decimal(raw)
    if value.copy_abs() > Decimal("1e18"):
        raise ValueError("rate")
    number = float(value)
    if number > 10000:
        scaled = Decimal.from_float(number / 10000)
        rounded = scaled.quantize(
            Decimal("0.1"), context=Context(prec=64, rounding=ROUND_HALF_UP)
        )
        display = f"{rounded:.1f}万热度"
    else:
        display = f"{math.floor(number)}热度"
    return dict(
        raw=raw,
        value=str(value),
        display=display,
        unit=None,
        derivation="app_number_floor_or_wan_fixed1",
    )


def decode_rows(body: bytes) -> list[dict]:
    """解码一次响应，保留来源顺序和重复项。"""
    payload = json.loads(
        body.decode("utf-8"),
        parse_float=Decimal,
        object_pairs_hook=unique_object,
        parse_constant=invalid_number,
    )
    if not isinstance(payload, dict) or type(payload.get("status_code")) is not int:
        raise ValueError("status_code")
    if payload["status_code"]:
        raise HotBusinessError("business")
    data = payload.get("data")
    if not isinstance(data, dict) or not isinstance(data.get("stock_list"), list):
        raise ValueError("stock_list")
    rows = []
    for item in data["stock_list"]:
        if not isinstance(item, dict):
            raise ValueError("item")
        code = item.get("code")
        if not isinstance(code, str) or not re.fullmatch(r"[0-9]{6}", code):
            raise ValueError("code")
        market = item.get("market")
        rows.append(
            dict(
                stock_code=code,
                market=None if market is None else str(market),
                name=item.get("name"),
                rank=_rank(item.get("order"), 1, 10_000_000),
                heat=_heat(item.get("rate")),
                rank_change=_rank(
                    item.get("hot_rank_chg"), -1_000_000_000, 1_000_000_000
                ),
                latest=None,
                change_rate=None,
            )
        )
    return rows


def fetch_hot(args: argparse.Namespace, get_body) -> dict:
    period = {"stock_hour": "hour", "stock_24h": "day"}[args.board]
    path = f"/open/api/hot_list/v1/hot_stock/a/{period}/data.txt"
    body = get_body(args, "eq.10jqka.com.cn", path, args.retries)
    try:
        rows = decode_rows(body)
    except HotBusinessError:
        raise QueryFailure("business") from None
    except (ValueError, RecursionError):
        raise QueryFailure("protocol") from None
    return dict(
        board=args.board,
        items=rows,
        received_at=datetime.now(UTC),
        source="hot_stock/a/" + period,
        unknowns=["heat_algorithm", "rank_change_baseline"],
        total=None,
        has_more=None,
        source_time=None,
        next_page_available=False,
        returned_count=len(rows),
    )
