"""Entrypoint for the hactl-companion add-on."""

from __future__ import annotations

import argparse
import asyncio
import logging
import os

from aiohttp import web

from companion import __version__, logbuffer, wg_supervisor
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

    level = getattr(logging, args.log_level.upper(), logging.INFO)
    # No timestamp in the format: the HA add-on log viewer (journald) already
    # prefixes each line with its own time. Adding %(asctime)s here produced a
    # confusing double timestamp. `hactl companion logs` still shows times — it
    # formats them from each record's epoch, captured by the ring buffer below.
    logging.basicConfig(
        level=level,
        format="%(levelname)s %(name)s: %(message)s",
    )
    # Capture our own records so `hactl companion logs` can read them back over
    # Ingress (add-on logs never reach HA core's logger).
    logbuffer.install(level=level)

    host = _parse_host(args.host)
    config_base_path = "/config"

    # Announce ourselves first, before doing any tunnel work, so the log reads
    # top-to-bottom in the order things actually happen.
    logger.info("hactl-companion v%s starting (port %s)", __version__, args.port)
    supervisor_token_status = (
        "present" if os.environ.get("SUPERVISOR_TOKEN") else "MISSING (direct access via SUPERVISOR_TOKEN will fail)"
    )
    logger.info("supervisor token: %s", supervisor_token_status)

    # Reconcile VPN tunnel state from HA add-on options before serving.
    # A failure here must never block the rest of the add-on from starting.
    reconciled = None
    try:
        opts = wg_supervisor.load_options()
        if opts is not None:
            reconciled = asyncio.run(wg_supervisor.reconcile(opts))
    except Exception:
        logger.exception("VPN reconcile failed; continuing add-on startup")

    app = create_app(config_base_path)
    # Post-up WG work (dyndns monitor + connection confirmation) must run in the
    # server's event loop, not reconcile's throwaway one — see wg_supervisor.
    if reconciled is not None:
        wg_supervisor.register_startup_tasks(app, reconciled.tunnel, reconciled.conf_text)

    # access_log=None silences aiohttp's built-in access logger; our
    # access_log_middleware already emits one structured line per request.
    web.run_app(app, host=host, port=args.port, print=None, access_log=None)


if __name__ == "__main__":
    main()
