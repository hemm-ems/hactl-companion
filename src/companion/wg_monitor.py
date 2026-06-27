"""Background monitor that keeps WireGuard peer endpoints in sync with dyndns.

WireGuard resolves hostnames once at tunnel startup and never re-resolves them.
This module watches every hostname-endpoint peer's handshake staleness. When a
peer goes quiet the monitor resolves the hostname again and pushes the updated
address via ``wg set peer ... endpoint``. Re-resolve attempts follow a fixed
backoff schedule (5 s → 10 s → 30 s → 60 s → … → 1 h) and never give up.

Per-tunnel state is tracked in a registry so ``status()`` can surface what the
monitor is doing (last re-resolve, resolved IPs, backoff, last error) — the log
lines alone are invisible to hactl, which is what makes the tunnel a black box
during a road test.
"""

from __future__ import annotations

import asyncio
import itertools
import logging
import time
from dataclasses import dataclass, field

from companion import wg

logger = logging.getLogger(__name__)

_HEALTH_INTERVAL: float = 30.0  # seconds between liveness checks (healthy state)
_STALE_HANDSHAKE: int = 180  # seconds — WireGuard session expiry threshold
# Delays between re-resolve attempts; last value repeats indefinitely.
_BACKOFF: tuple[float, ...] = (5, 10, 30, 60, 120, 300, 600, 1800, 3600)


@dataclass
class _MonitorState:
    """Live introspection for a tunnel's monitor task."""

    tunnel: str
    hostnames: list[str]
    task: asyncio.Task[None] | None = None
    last_check_ts: float | None = None
    last_reresolve_ts: float | None = None
    resolved: dict[str, str] = field(default_factory=dict)  # hostname -> last IP
    in_backoff: bool = False
    attempt: int = 0
    next_retry_ts: float | None = None
    last_error: str | None = None


# Registry of per-tunnel monitor state (holds the running task too).
_monitors: dict[str, _MonitorState] = {}


def start_monitor(tunnel: str, conf_text: str) -> None:
    """Start (or restart) the liveness + re-resolution monitor for *tunnel*.

    No-op when the config contains no hostname endpoints.
    """
    peers = wg._parse_hostname_peers(conf_text)
    if not peers:
        return
    stop_monitor(tunnel)
    state = _MonitorState(tunnel=tunnel, hostnames=[p.hostname for p in peers])
    _monitors[tunnel] = state
    state.task = asyncio.create_task(_monitor_loop(tunnel, peers), name=f"wg-monitor-{tunnel}")
    logger.info("wg monitor started for tunnel %s (%d hostname peer(s))", tunnel, len(peers))


def stop_monitor(tunnel: str) -> None:
    """Cancel the monitor for *tunnel* if one is running."""
    state = _monitors.pop(tunnel, None)
    if state and state.task:
        state.task.cancel()
        logger.info("wg monitor stopped for tunnel %s", tunnel)


def status(tunnel: str) -> dict[str, object]:
    """Return a JSON-serializable snapshot of the monitor for *tunnel*."""
    state = _monitors.get(tunnel)
    if state is None:
        return {"running": False}
    now = time.time()

    def _ago(ts: float | None) -> int | None:
        return int(now - ts) if ts is not None else None

    return {
        "running": True,
        "hostnames": state.hostnames,
        "healthy": not state.in_backoff,
        "resolved": dict(state.resolved),
        "last_check_secs_ago": _ago(state.last_check_ts),
        "last_reresolve_secs_ago": _ago(state.last_reresolve_ts),
        "attempt": state.attempt,
        "next_retry_secs": (int(state.next_retry_ts - now) if state.next_retry_ts else None),
        "last_error": state.last_error,
    }


async def _monitor_loop(tunnel: str, peers: list[wg._PeerEndpoint]) -> None:
    state = _monitors.get(tunnel)
    try:
        while True:
            await asyncio.sleep(_HEALTH_INTERVAL)
            if state:
                state.last_check_ts = time.time()
            if await _is_peer_alive(tunnel, peers):
                continue
            logger.warning("wg monitor: stale handshake on %s — re-resolving endpoint(s)", tunnel)
            if state:
                state.in_backoff = True
            await _reconnect_loop(tunnel, peers)
            if state:
                state.in_backoff = False
                state.attempt = 0
                state.next_retry_ts = None
                state.last_error = None
            logger.info("wg monitor: tunnel %s reconnected", tunnel)
    except asyncio.CancelledError:
        pass


async def _reconnect_loop(tunnel: str, peers: list[wg._PeerEndpoint]) -> None:
    """Re-resolve and push the endpoint until the peer reconnects. Never gives up."""
    state = _monitors.get(tunnel)
    for attempt, delay in enumerate(itertools.chain(_BACKOFF, itertools.repeat(_BACKOFF[-1]))):
        if state:
            state.attempt = attempt + 1
        ok = await _tick(tunnel, peers)
        if not ok:
            logger.debug(
                "wg monitor: re-resolve attempt %d failed for tunnel %s; next in %.0fs",
                attempt + 1,
                tunnel,
                delay,
            )
        if state:
            state.next_retry_ts = time.time() + delay
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
    state = _monitors.get(tunnel)
    all_ok = True
    for peer in peers:
        ip = await wg._dns_lookup_ip(peer.hostname)
        if ip is None:
            logger.warning("wg monitor: re-resolve failed for %s (tunnel %s)", peer.hostname, tunnel)
            if state:
                state.last_error = f"re-resolve failed for {peer.hostname}"
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
            if state:
                state.last_error = f"endpoint update failed for {peer.hostname}: {stderr.strip()}"
            all_ok = False
            continue
        if state:
            state.resolved[peer.hostname] = ip
            state.last_reresolve_ts = time.time()
    return all_ok
