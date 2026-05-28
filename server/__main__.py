"""Entry point that wraps `uvicorn.run` so reasonable defaults — host,
port, and WebSocket keepalive — are baked in. Run with:

    python -m server

or override anything via the matching JARVIZ_* env vars in .env.

Equivalent CLI invocation if you'd rather use uvicorn directly:

    uvicorn server.main:app \
        --host $JARVIZ_HTTP_HOST --port $JARVIZ_HTTP_PORT \
        --ws-ping-interval $JARVIZ_WS_PING_INTERVAL_S \
        --ws-ping-timeout  $JARVIZ_WS_PING_TIMEOUT_S
"""

from __future__ import annotations

import uvicorn

from .config import settings


def main() -> None:
    uvicorn.run(
        "server.main:app",
        host=settings.JARVIZ_HTTP_HOST,
        port=settings.JARVIZ_HTTP_PORT,
        # WS keepalive: with these set, a client that vanishes (TCP RST,
        # power-cycled device, dropped Wi-Fi) is detected within
        # roughly ping_interval + ping_timeout seconds and its session
        # slot is released. Without explicit values, uvicorn uses its
        # own 20s/20s defaults, but settings.* let operators tune it.
        ws_ping_interval=settings.JARVIZ_WS_PING_INTERVAL_S,
        ws_ping_timeout=settings.JARVIZ_WS_PING_TIMEOUT_S,
        # Reload only when explicitly opted in; production should never
        # auto-reload (it tears down WS sessions on every file save).
        reload=False,
    )


if __name__ == "__main__":
    main()
