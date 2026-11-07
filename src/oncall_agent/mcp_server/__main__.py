"""``python -m oncall_agent.mcp_server [stdio|streamable-http] [port]``."""

from __future__ import annotations

import sys

from oncall_agent.config import get_settings
from oncall_agent.mcp_server.server import build_backend, create_server


def main(argv: list[str] | None = None) -> None:
    args = sys.argv[1:] if argv is None else argv
    transport = args[0] if args else "stdio"
    settings = get_settings()
    server = create_server(build_backend(settings), settings)
    if transport == "stdio":
        server.run("stdio")
    elif transport in ("http", "streamable-http"):
        port = int(args[1]) if len(args) > 1 else 8765
        # Bind to localhost by default; expose deliberately (and behind auth) if needed.
        server.run("streamable-http", host="127.0.0.1", port=port)
    else:
        raise SystemExit(f"unknown transport {transport!r}")


if __name__ == "__main__":
    main()
