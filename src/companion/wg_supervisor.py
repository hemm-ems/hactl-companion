"""Startup supervisor: reconcile WireGuard tunnel state from HA add-on options.

Reads ``/data/options.json`` (written by the HA Supervisor from the add-on
Configuration tab) and brings the tunnel up or down to match the user's
declared intent.

This module is the only "VPN can manage itself from inside the add-on" path —
the hactl CLI typically talks to HA *over* the VPN and cannot be the entry
point for bringing it up.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path

from companion import wg, wg_monitor

logger = logging.getLogger(__name__)

_OPTIONS_PATH = Path("/data/options.json")
_FALLBACK_DIR = Path("/config/hactl")


@dataclass
class VPNOptions:
    enabled: bool
    tunnel: str
    config: str


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


async def reconcile(opts: VPNOptions, *, fallback_dir: Path = _FALLBACK_DIR) -> None:
    """Bring the tunnel state into line with ``opts``.

    Never raises — failures are logged so the rest of the add-on can still
    come up. ``enabled=False`` is treated as the master kill-switch.
    """
    try:
        if not opts.enabled:
            if await wg._is_interface_up(opts.tunnel):
                logger.info("vpn.enabled=false; bringing tunnel %s down", opts.tunnel)
                rc, _, stderr = await wg._run_wg_cmd("wg-quick", "down", opts.tunnel)
                if rc != 0:
                    logger.warning("wg-quick down %s failed (rc=%s): %s", opts.tunnel, rc, stderr.strip())
            return

        conf_text = _resolve_conf_text(opts, fallback_dir)
        if not conf_text:
            logger.warning(
                "vpn.enabled=true but no config provided "
                "(vpn.config is empty and %s/%s.conf is missing); tunnel not started",
                fallback_dir,
                opts.tunnel,
            )
            return

        try:
            wg._validate_conf(conf_text)
        except Exception as exc:
            logger.warning("vpn config rejected: %s; tunnel not started", exc)
            return

        # Persist the resolved config to the canonical location (keeps the file
        # in sync when it came from vpn.config) and materialize /etc/wireguard.
        wg.save_config(opts.tunnel, conf_text, persist_dir=fallback_dir)

        failed = await wg._resolve_endpoint_hostnames(conf_text)
        if failed:
            logger.warning(
                "DNS resolution failed for endpoint(s): %s; tunnel not started",
                ", ".join(failed),
            )
            return

        if not await wg._is_interface_up(opts.tunnel):
            logger.info("vpn.enabled=true; bringing tunnel %s up", opts.tunnel)
            rc, _, stderr = await wg._run_wg_cmd("wg-quick", "up", opts.tunnel)
            if rc != 0:
                logger.warning("wg-quick up %s failed (rc=%s): %s", opts.tunnel, rc, stderr.strip())
                return
            wg_monitor.start_monitor(opts.tunnel, conf_text)
        else:
            logger.info("tunnel %s already up; left in place", opts.tunnel)
    except Exception:
        logger.exception("VPN reconcile failed for tunnel %s", opts.tunnel)
