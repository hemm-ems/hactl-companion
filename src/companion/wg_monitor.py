"""Background monitor that keeps WireGuard peer endpoints in sync with dyndns.

WireGuard resolves hostnames once at tunnel startup and never re-resolves them.
This module watches every hostname-endpoint peer's handshake staleness. When a
peer goes quiet the monitor resolves the hostname again and pushes the updated
address via ``wg set peer ... endpoint``. Re-resolve attempts follow a fixed
backoff schedule (5 s → 10 s → 30 s → 60 s → … → 1 h) and never give up.
"""

from __future__ import annotations

import asyncio
import itertools
import logging
import time

from companion import wg

logger = logging.getLogger(__name__)

_HEALTH_INTERVAL: float = 30.0  # seconds between liveness checks (healthy state)
_STALE_HANDSHAKE: int = 180  # seconds — WireGuard session expiry threshold
# Delays between re-resolve attempts; last value repeats indefinitely.
_BACKOFF: tuple[float, ...] = (5, 10, 30, 60, 120, 300, 600, 1800, 3600)

_monitors: dict[str, asyncio.Task[None]] = {}


def start_monitor(tunnel: str, conf_text: str) -> None:
    """Start (or restart) the liveness + re-resolution monitor for *tunnel*.

    No-op when the config contains no hostname endpoints.
    """
    peers = wg._parse_hostname_peers(conf_text)
    if not peers:
        return
    stop_monitor(tunnel)
    _monitors[tunnel] = asyncio.create_task(
        _monitor_loop(tunnel, peers),
        name=f"wg-monitor-{tunnel}",
    )
    logger.info("wg monitor started for tunnel %s (%d hostname peer(s))", tunnel, len(peers))


def stop_monitor(tunnel: str) -> None:
    """Cancel the monitor for *tunnel* if one is running."""
    task = _monitors.pop(tunnel, None)
    if task:
        task.cancel()
        logger.info("wg monitor stopped for tunnel %s", tunnel)


async def _monitor_loop(tunnel: str, peers: list[wg._PeerEndpoint]) -> None:
    try:
        while True:
            await asyncio.sleep(_HEALTH_INTERVAL)
            if await _is_peer_alive(tunnel, peers):
                continue
            logger.warning("wg monitor: stale handshake on %s — re-resolving endpoint(s)", tunnel)
            await _reconnect_loop(tunnel, peers)
            logger.info("wg monitor: tunnel %s reconnected", tunnel)
    except asyncio.CancelledError:
        pass


async def _reconnect_loop(tunnel: str, peers: list[wg._PeerEndpoint]) -> None:
    """Re-resolve and push the endpoint until the peer reconnects. Never gives up."""
    for attempt, delay in enumerate(itertools.chain(_BACKOFF, itertools.repeat(_BACKOFF[-1]))):
        ok = await _tick(tunnel, peers)
        if not ok:
            logger.debug(
                "wg monitor: re-resolve attempt %d failed for tunnel %s; next in %.0fs",
                attempt + 1,
                tunnel,
                delay,
            )
        await asyncio.sleep(delay)
        if await _is_peer_alive(tunnel, peers):
            return


async def _is_peer_alive(tunnel: str, peers: list[wg._PeerEndpoint]) -> bool:
    """Return True when every hostname-endpoint peer has a handshake newer than _STALE_HANDSHAKE s.

    Uses ``wg show <tunnel> dump`` which returns tab-separated output with the
    latest-handshake as a Unix timestamp in field index 4 of each peer line.
    """
    rc, stdout, _ = await wg._run_wg_cmd("wg", "show", tunnel, "dump")
    if rc != 0:
        return False

    handshakes: dict[str, int] = {}
    for line in stdout.splitlines()[1:]:  # first line is the interface row
        parts = line.split("\t")
        if len(parts) >= 5:
            try:
                handshakes[parts[0]] = int(parts[4])
            except ValueError:
                handshakes[parts[0]] = 0

    now = int(time.time())
    return all(now - handshakes.get(peer.pubkey, 0) <= _STALE_HANDSHAKE for peer in peers)


async def _tick(tunnel: str, peers: list[wg._PeerEndpoint]) -> bool:
    """Resolve every hostname endpoint and push the result to WireGuard."""
    all_ok = True
    for peer in peers:
        ip = await wg._dns_lookup_ip(peer.hostname)
        if ip is None:
            logger.warning("wg monitor: re-resolve failed for %s (tunnel %s)", peer.hostname, tunnel)
            all_ok = False
            continue
        rc, _, stderr = await wg._run_wg_cmd(
            "wg",
            "set",
            tunnel,
            "peer",
            peer.pubkey,
            "endpoint",
            f"{ip}:{peer.port}",
        )
        if rc != 0:
            logger.warning(
                "wg monitor: endpoint update failed for %s: %s",
                peer.hostname,
                stderr.strip(),
            )
            all_ok = False
    return all_ok
