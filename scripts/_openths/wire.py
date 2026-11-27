from __future__ import annotations

import socket
import time

from .market import market_overview as market_overview
from .quote import quote_body, quote_table
from .ranking import ranking_body, ranking_table
from .transport import BASE64 as BASE64
from .transport import WIRE64 as WIRE64
from .transport import SessionRejected as SessionRejected
from .transport import WireError as WireError
from .transport import (
    initialize,
    receive,
    request_frame,
    send_until,
    snappy,
)
from .transport import packet as packet
from .transport import unmap_body as unmap_body


def tcp_page(args, config, start=0):
    from datetime import UTC, datetime

    deadline = time.monotonic() + args.request_timeout
    quote = args.command == "quote"
    frame_id = 2267 if quote else 2312
    body = (
        quote_body(args.code, args.market, 1)
        if quote
        else ranking_body(args.sort, args.direction, start, 1)
    )
    for attempt in range(args.retries + 1):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError
        try:
            with socket.create_connection(
                (args.host, args.port), timeout=min(remaining, args.connect_timeout)
            ) as sock:
                session = initialize(sock, config, args.app_info, deadline)

                def send(payload, number, flags, session=session):
                    send_until(
                        sock,
                        request_frame(
                            payload,
                            number=number,
                            flags=flags,
                            frame_id=frame_id,
                            app_info=args.app_info,
                            session=session,
                        ),
                        deadline,
                    )

                send(body, 1, 0x01010000)
                for _ in range(20):
                    response = packet(receive(sock, deadline))
                    if (
                        response.flags in (11, 65547)
                        and response.number == 0
                        and len(response.extension) == 2
                    ):
                        continue
                    break
                else:
                    raise WireError("auxiliary message limit")
                if (response.number, response.instance) != (1, 1):
                    raise WireError("response identity")
                if response.frame_id != frame_id and not (
                    response.frame_id == 0 and len(response.extension) == 2
                ):
                    raise WireError("response frame")
                if quote:
                    if response.flags not in (0x100, 0x1100):
                        raise WireError("quote flags")
                    raw = (
                        snappy(response.body)
                        if response.flags == 0x1100
                        else response.body
                    )
                    result = quote_table(raw, args.code, args.market)
                    send(b"[frame]\r\nid=2267\r\npageList=\r\n", 2, 0x01060000)
                    result["received_at"] = datetime.now(UTC)
                    return result
                if response.flags != 0x1130 or not (
                    response.extension == b"\0\xff"
                    or (
                        len(response.extension) == 8
                        and response.frame_id == 2312
                        and response.extension.startswith(b"\0" * 4)
                    )
                ):
                    raise WireError("ranking header")
                return ranking_table(
                    snappy(response.body), args.sort, args.direction, start
                )
        except (OSError, EOFError):
            if attempt == args.retries:
                raise
    raise TimeoutError
