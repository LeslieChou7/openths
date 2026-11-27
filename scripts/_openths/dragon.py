import json
import re
from datetime import UTC, date, datetime
from decimal import Decimal
from time import monotonic
from urllib.parse import urlencode

from .common import QueryFailure, invalid_number, select, unique_object


def dragon_data(get_body, args, route, params, until=None):
    path = "/dataapi/transaction/" + route
    if params:
        path += "?" + urlencode(params)
    body = get_body(args, "data.10jqka.com.cn", path, args.retries, until=until)
    value = json.loads(
        body.decode(),
        parse_float=Decimal,
        object_pairs_hook=unique_object,
        parse_constant=invalid_number,
    )
    if type(value["status_code"]) is not int:
        raise ValueError("status")
    if value["status_code"] != 0 or value.get("succeed") is False:
        raise QueryFailure("business")
    return value["data"]


def dragon_amounts(row, keys=("buy_value", "sell_value", "net_value")):
    result = {}
    for key in keys:
        value = row.get(key)
        if value is not None:
            if isinstance(value, bool):
                raise ValueError(key)
            value = Decimal(str(value))
            if not value.is_finite():
                raise ValueError(key)
        result[key] = value
    return result


def dragon_entry(row, day):
    code, market = row["stock_code"], row["market_id"]
    if (
        not isinstance(code, str)
        or not re.fullmatch(r"[0-9]{6}", code)
        or not isinstance(market, str)
    ):
        raise ValueError("identity")
    ref = dict(
        market=market, stock_code=code, request_date=day, range_days=row["range_days"]
    )
    return dict(
        ref=ref,
        stock_name=row["stock_name"],
        hot_rank=row.get("hot_rank"),
        limit_reason=row.get("limit_reason"),
        amount_unit=None,
        **dragon_amounts(row),
        **dragon_amounts(row, ("change", "net_rate")),
    )


def dragon_events(get_body, args, entry):
    ref = entry["ref"]
    group = {1: "one", 3: "three", 10: "ten", 30: "thirty"}.get(ref["range_days"])
    if group is None:
        raise ValueError("range")
    data = dragon_data(
        get_body, args, "stock/v1/info", dict(code=ref["stock_code"], date=args.date)
    )
    if (data["stock_code"], data["market_id"]) != (ref["stock_code"], ref["market"]):
        raise ValueError("identity")
    result = []
    for row in data["details"][group]:
        reasons = [dict(id=r["id"], content=r["content"]) for r in row["reason"]]
        start, end = (date.fromisoformat(row[k]) for k in ("start_date", "end_date"))
        if (
            row["range_type"] != str(ref["range_days"])
            or end != args.date
            or start > end
        ):
            raise ValueError("event identity")
        identity = dict(
            **ref,
            range_type=row["range_type"],
            start_date=start,
            end_date=end,
            reasons=reasons,
        )
        event = dict(
            ref=identity,
            stock_name=data["stock_name"],
            reasons=reasons,
            amount_unit=None,
            **dragon_amounts(row),
        )
        result.append((event, row))
    return result


def dragon_query(args, get_body):
    count = args.pages if args.command == "dragon-list" else args.list_page
    pages = []
    for index in range(1, count + 1):
        deadline = monotonic() + args.request_timeout
        calendar = dragon_data(get_body, args, "market/v1/trade_day", {}, deadline)
        raw_days = calendar["trade_day_list"]
        if not isinstance(raw_days, list) or not raw_days:
            raise ValueError("calendar")
        days = [date.fromisoformat(day) for day in raw_days]
        if (
            days != sorted(set(days))
            or calendar["last_date"] != days[-1].isoformat()
            or type(calendar["last_update_status"]) is not bool
        ):
            raise ValueError("calendar")
        if args.date not in days:
            raise QueryFailure("configuration")
        params = dict(
            order_field="hot_rank",
            order_type="asc",
            date=args.date,
            filter="",
            page=index,
            size=50,
            module="all",
            order_null_greater=1,
        )
        data = dragon_data(get_body, args, "stock/v1/list", params, deadline)
        total, stock_count, rows = data["count"], data["stock_count"], data["items"]
        offset = (index - 1) * 50
        if (
            data["module"] != "all"
            or type(data["page"]) is not int
            or data["page"] != index
            or type(data["size"]) is not int
            or data["size"] != 50
            or type(total) is not int
            or offset > total
            or (
                stock_count is not None
                and (type(stock_count) is not int or not 0 <= stock_count <= total)
            )
            or not isinstance(rows, list)
            or len(rows) != min(50, total - offset)
        ):
            raise ValueError("page")
        items = [dragon_entry(row, args.date) for row in rows]
        pending = (
            calendar["last_date"] == args.date.isoformat()
            and calendar["last_update_status"] is False
        )
        if pending and items:
            raise ValueError("status")
        more = offset + len(items) < total
        pages.append(
            dict(
                items=items,
                total=data["count"],
                stock_count=data["stock_count"],
                page=index,
                size=50,
                next_page_available=more,
                has_more=more,
                status="pending" if pending else "unknown",
                request_date=args.date,
                received_at=datetime.now(UTC),
                source_date=None,
            )
        )
        if not more:
            break
    if args.command == "dragon-list":
        return dict(requested_pages=count, returned_pages=len(pages), pages=pages)
    if len(pages) != count:
        raise QueryFailure("selection")
    entry = select(pages[-1]["items"], args.entry_index)
    pairs = dragon_events(get_body, args, entry)
    if args.command == "dragon-events":
        return dict(entry=entry, events=[event for event, _ in pairs])
    event, _ = select(pairs, args.event_index)
    matches = [
        (current, row)
        for current, row in dragon_events(get_body, args, entry)
        if current["ref"] == event["ref"]
    ]
    if len(matches) != 1:
        raise QueryFailure("selection")
    current, row = matches[0]
    detail = dict(current, received_at=datetime.now(UTC))
    for side in ("buy", "sell"):
        detail[side + "_seats"] = [
            dict(
                id=seat["id"],
                org_id=seat.get("org_id"),
                name=seat["name"],
                short_name=seat.get("short_name"),
                direction=side,
                rank=seat["rank_value"],
                amount_unit=None,
                **dragon_amounts(seat),
            )
            for seat in row[side + "_items"]
        ]
    return dict(entry=entry, event=event, detail=detail)
