"""Entrypoint for the hactl-companion add-on."""

from __future__ import annotations

import argparse
import logging
import os

from aiohttp import web

from companion import __version__
from companion.server import create_app

logger = logging.getLogger("companion")


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="hactl-companion",
        description="Home Assistant companion server for hactl CLI.",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"hactl-companion {__version__}",
    )
    parser.add_argument("--host", default="0.0.0.0,[::]", help="bind address (default: 0.0.0.0,[::])")
    parser.add_argument("--port", type=int, default=9100, help="bind port (default: 9100)")
    parser.add_argument(
        "--log-level",
        default=os.environ.get("LOG_LEVEL", "info"),
        help="log level (default: info, or LOG_LEVEL env)",
    )
    return parser.parse_args(argv)


def _parse_host(host_str: str) -> str | list[str]:
    """Parse host argument — supports comma-separated for dual-stack."""
    hosts = [h.strip().strip("[]") for h in host_str.split(",")]
    return hosts if len(hosts) > 1 else hosts[0]


def main(argv: list[str] | None = None) -> None:
    args = _parse_args(argv)

    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    host = _parse_host(args.host)
    app = create_app()
    logger.info("hactl-companion v%s listening on %s:%s", __version__, args.host, args.port)
    web.run_app(app, host=host, port=args.port, print=None)


if __name__ == "__main__":
    main()
