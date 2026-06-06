"""Startup supervisor: reconcile WireGuard tunnel state from HA add-on options.

Reads ``/data/options.json`` (written by the HA Supervisor from the add-on
Configuration tab) and brings the tunnel up or down to match the user's
declared intent.

This module is the only "VPN can manage itself from inside the add-on" path —
the hactl CLI typically talks to HA *over* the VPN and cannot be the entry
point for bringing it up.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path

from aiohttp import web

from companion import wg, wg_monitor

logger = logging.getLogger(__name__)

_OPTIONS_PATH = Path("/data/options.json")
_FALLBACK_DIR = Path("/config/hactl")

# How long to wait for the first handshake before warning the tunnel didn't connect.
_CONNECT_TIMEOUT: float = 15.0
_CONNECT_INTERVAL: float = 1.0


@dataclass
class VPNOptions:
    enabled: bool
    tunnel: str
    config: str


@dataclass
class ReconcileResult:
    """What reconcile brought up, so the server loop can watch/monitor it."""

    tunnel: str
    conf_text: str


def load_options(path: Path = _OPTIONS_PATH) -> VPNOptions | None:
    """Read the add-on options.json and return parsed VPN options.

    Returns ``None`` when the file is missing (e.g. running outside Supervisor)
    or has no ``vpn`` section. Invalid tunnel names are rejected.
    """
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        logger.debug("options.json not found at %s; skipping VPN reconcile", path)
        return None
    except OSError as exc:
        logger.warning("could not read options.json (%s): %s", path, exc)
        return None

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        logger.warning("options.json is not valid JSON: %s", exc)
        return None

    vpn = data.get("vpn") if isinstance(data, dict) else None
    if not isinstance(vpn, dict):
        return None

    tunnel = str(vpn.get("tunnel", "wg0") or "wg0")
    if not wg._TUNNEL_RE.fullmatch(tunnel):
        logger.warning("invalid vpn.tunnel %r in options.json; skipping VPN reconcile", tunnel)
        return None

    return VPNOptions(
        enabled=bool(vpn.get("enabled", False)),
        tunnel=tunnel,
        config=str(vpn.get("config") or ""),
    )


def _resolve_conf_text(opts: VPNOptions, fallback_dir: Path) -> str:
    """Return the wg.conf text to use, or '' if none is available.

    Precedence: ``vpn.config`` from options > ``<fallback_dir>/<tunnel>.conf``.
    """
    if opts.config.strip():
        return opts.config

    fallback = fallback_dir / f"{opts.tunnel}.conf"
    if fallback.exists():
        try:
            return fallback.read_text(encoding="utf-8")
        except OSError as exc:
            logger.warning("could not read fallback config %s: %s", fallback, exc)
    return ""


async def reconcile(opts: VPNOptions, *, fallback_dir: Path = _FALLBACK_DIR) -> ReconcileResult | None:
    """Bring the tunnel state into line with ``opts``.

    Returns a :class:`ReconcileResult` when a tunnel is up (so the server loop
    can start the dyndns monitor and confirm the connection), else ``None``.
    Never raises — failures are logged so the rest of the add-on can still come
    up. ``enabled=False`` is the master kill-switch.

    Post-up work (monitor + connection watch) is intentionally NOT done here:
    this runs under ``asyncio.run`` in __main__, whose loop is closed right
    after, which would cancel any task started here. See register_startup_tasks.
    """
    try:
        if not opts.enabled:
            if await wg._is_interface_up(opts.tunnel):
                logger.info("vpn.enabled=false; bringing tunnel %s down", opts.tunnel)
                rc, _, stderr = await wg._run_wg_cmd("wg-quick", "down", opts.tunnel)
                if rc != 0:
                    logger.warning("wg-quick down %s failed (rc=%s): %s", opts.tunnel, rc, stderr.strip())
            return None

        conf_text = _resolve_conf_text(opts, fallback_dir)
        if not conf_text:
            logger.warning(
                "vpn.enabled=true but no config provided "
                "(vpn.config is empty and %s/%s.conf is missing); tunnel not started",
                fallback_dir,
                opts.tunnel,
            )
            return None

        try:
            wg._validate_conf(conf_text)
        except Exception as exc:
            logger.warning("vpn config rejected: %s; tunnel not started", exc)
            return None

        # Persist the resolved config to the canonical location (keeps the file
        # in sync when it came from vpn.config) and materialize /etc/wireguard.
        wg.save_config(opts.tunnel, conf_text, persist_dir=fallback_dir)

        failed = await wg._resolve_endpoint_hostnames(conf_text)
        if failed:
            logger.warning(
                "DNS resolution failed for endpoint(s): %s; tunnel not started",
                ", ".join(failed),
            )
            return None

        if not await wg._is_interface_up(opts.tunnel):
            logger.info("vpn.enabled=true; bringing tunnel %s up", opts.tunnel)
            rc, _, stderr = await wg._run_wg_cmd("wg-quick", "up", opts.tunnel)
            if rc != 0:
                logger.warning("wg-quick up %s failed (rc=%s): %s", opts.tunnel, rc, stderr.strip())
                return None
        else:
            logger.info("tunnel %s already up; left in place", opts.tunnel)

        await _log_up_summary(opts.tunnel)
        return ReconcileResult(tunnel=opts.tunnel, conf_text=conf_text)
    except Exception:
        logger.exception("VPN reconcile failed for tunnel %s", opts.tunnel)
        return None


async def _log_up_summary(tunnel: str) -> None:
    """Log a short, honest 'interface configured' line (no connectivity claim)."""
    try:
        rc, stdout, _ = await wg._run_wg_cmd("wg", "show", tunnel, "dump")
        if rc != 0:
            return
        peers = wg._parse_wg_dump(stdout).get("peers")
        peers = peers if isinstance(peers, list) else []
        if not peers:
            logger.info("wg %s up — no peers configured", tunnel)
            return
        peer = peers[0] if isinstance(peers[0], dict) else {}
        more = f" (+{len(peers) - 1} more)" if len(peers) > 1 else ""
        logger.info("wg %s up — peer %s%s", tunnel, peer.get("endpoint") or "(none)", more)
    except Exception:
        logger.debug("could not build wg up-summary for %s", tunnel, exc_info=True)


def register_startup_tasks(app: web.Application, tunnel: str, conf_text: str) -> None:
    """Start post-up WG work in the server's (long-lived) event loop.

    Done via ``on_startup`` rather than in reconcile so the dyndns monitor and
    the connection watcher run in the loop that stays alive — fixing a bug where
    a monitor started under __main__'s ``asyncio.run`` was cancelled on exit.
    """

    async def _on_startup(app: web.Application) -> None:
        # Dyndns re-resolution for hostname peers (no-op for IP endpoints).
        wg_monitor.start_monitor(tunnel, conf_text)
        # Fire-and-forget: must not await, or serving would block on the handshake.
        app["_wg_watch"] = asyncio.create_task(_watch_connection(tunnel))

    async def _on_cleanup(app: web.Application) -> None:
        task = app.get("_wg_watch")
        if task is not None:
            task.cancel()
        wg_monitor.stop_monitor(tunnel)

    app.on_startup.append(_on_startup)
    app.on_cleanup.append(_on_cleanup)


async def _watch_connection(
    tunnel: str,
    *,
    timeout: float = _CONNECT_TIMEOUT,
    interval: float = _CONNECT_INTERVAL,
) -> None:
    """Poll for the first WireGuard handshake and log a definitive result.

    Logs ``connected`` once a peer handshake lands within the staleness window,
    or a warning if none arrives before ``timeout``. Best-effort; never raises.
    """
    try:
        deadline = time.monotonic() + timeout
        endpoint = "(none)"
        while time.monotonic() < deadline:
            rc, stdout, _ = await wg._run_wg_cmd("wg", "show", tunnel, "dump")
            if rc == 0:
                peers = wg._parse_wg_dump(stdout).get("peers")
                peers = peers if isinstance(peers, list) else []
                for p in peers:
                    if not isinstance(p, dict):
                        continue
                    endpoint = p.get("endpoint") or endpoint
                    age = p.get("latest_handshake_secs")
                    if isinstance(age, int) and age <= wg_monitor._STALE_HANDSHAKE:
                        logger.info(
                            "wg %s connected — handshake %s ago, rx=%s tx=%s",
                            tunnel,
                            p.get("latest_handshake"),
                            p.get("transfer_rx"),
                            p.get("transfer_tx"),
                        )
                        return
            await asyncio.sleep(interval)
        logger.warning(
            "wg %s up but NOT connected — no handshake after %ds, peer %s unreachable? (kernel keeps retrying)",
            tunnel,
            int(timeout),
            endpoint,
        )
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.debug("connection watch failed for %s", tunnel, exc_info=True)
