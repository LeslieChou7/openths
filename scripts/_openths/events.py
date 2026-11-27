import json
import re
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation

from .common import QueryFailure


def event_row(row, anomaly):
    if not isinstance(row, dict):
        raise ValueError("row")

    def field(key, typ, nullable=False):
        value = row.get(key)
        if nullable and value is None:
            return None
        if type(value) is not typ or (typ is str and not value.isprintable()):
            raise ValueError(key)
        return value

    code = field("stock_code" if anomaly else "Code", str)
    if not re.fullmatch(r"[0-9]{6}", code):
        raise ValueError("code")
    if anomaly:
        item = dict(
            stock_code=code,
            market=field("stock_market", str),
            name=field("stock_name", str, True),
            ths_code=field("ths_code", str),
        )
        for key in ("monitor_type", "start_date", "end_date"):
            item[key + "_raw"] = field(key, int)
            if item[key + "_raw"] < 0:
                raise ValueError(key)
        return item
    item = dict(
        stock_code=code,
        market=None,
        name=field("Name", str),
        reason=field("Reason", str),
        suspension_date_raw=field("tpDate", str),
        resumption_date_raw=field("fpDate", str),
        resumption_label=field("isFp", str, True),
        days_raw=field("Days", int, True),
    )
    for name, key in (
        ("price", "Price"),
        ("change", "Change"),
        ("index_change", "indexChange"),
    ):
        raw = field(key, str)
        item[name + "_raw"] = raw
        try:
            number = Decimal(raw)
            item[name] = str(number) if number.is_finite() else None
        except InvalidOperation:
            item[name] = None
    return item


def fetch_events(args, get_body):
    anomaly = args.command == "stock-anomalies"
    pages = []
    seen = 0
    for page in range(1, args.pages + 1):
        if anomaly:
            category = "monitor" if args.kind == "current" else "history"
            host = "dq.10jqka.com.cn"
            path = f"//fuyao/stock_halt_monitor/monitor_stock/v1/list?date_type={category}&page_num={page}"
        else:
            category = "tpLatest" if args.kind == "suspension" else "fpLatest"
            host = "data.hexin.cn"
            path = f"/Tfp/{category}/page/{page}/"
        raw = get_body(args, host, path, 0)
        try:
            response = json.loads(raw.decode("utf-8"))
            total = None
            if anomaly:
                if type(response["status_code"]) is not int:
                    raise ValueError("status")
                if response["status_code"]:
                    raise QueryFailure("business")
                total = response["data"]["total_num"]
                if type(total) is not int or total < 0:
                    raise ValueError("total")
                response = response["data"]["list"]
            if not isinstance(response, list):
                raise ValueError("list")
            items = [event_row(row, anomaly) for row in response]
        except (ValueError, KeyError, TypeError, RecursionError):
            raise QueryFailure("protocol") from None
        seen += len(items)
        if anomaly:
            more = seen < total
            if seen > total or (more and len(items) < 20):
                more = None
            available = args.kind == "history" and page == 1 and more is True
            source = "https://" + host + path.split("?")[0]
        else:
            more = None if items else False
            available = page == 1 and len(items) == 10
            source = "https://" + host + "/Tfp/" + category
        pages.append(
            dict(
                kind=args.kind,
                items=items,
                total=total,
                source=source,
                received_at=datetime.now(UTC),
                has_more=more,
                next_page_available=available,
            )
        )
        if not available:
            break
    return dict(pages=pages, requested_pages=args.pages, returned_pages=len(pages))
