"""Tests for the WireGuard dyndns re-resolution monitor."""

from __future__ import annotations

import asyncio
import contextlib
import time
from unittest.mock import AsyncMock, patch

from companion import wg_monitor
from companion.wg import _PeerEndpoint

_PEER = _PeerEndpoint(pubkey="PUBKEY==", hostname="home.example.com", port=51820)

_CONF_WITH_HOSTNAME = (
    "[Interface]\nPrivateKey = K\nAddress = 10.0.0.1/24\n"
    "[Peer]\nPublicKey = PUBKEY==\nEndpoint = home.example.com:51820\nAllowedIPs = 0/0\n"
)
_CONF_IP_ONLY = (
    "[Interface]\nPrivateKey = K\nAddress = 10.0.0.1/24\n"
    "[Peer]\nPublicKey = PUBKEY==\nEndpoint = 1.2.3.4:51820\nAllowedIPs = 0/0\n"
)


def _dump(pubkey: str, last_hs: int) -> str:
    """Build a fake ``wg show dump`` output with one peer."""
    return f"(none)\tPUBLIC\t51820\t(none)\n{pubkey}\t(none)\t5.6.7.8:51820\t0/0\t{last_hs}\t0\t0\t25\n"


# ---------------------------------------------------------------------------
# _tick (unchanged logic, kept for regression)
# ---------------------------------------------------------------------------


class TestTick:
    async def test_resolves_and_calls_wg_set(self) -> None:
        with (
            patch("companion.wg._dns_lookup_ip", AsyncMock(return_value="5.6.7.8")),
            patch("companion.wg._run_wg_cmd", AsyncMock(return_value=(0, "", ""))) as run,
        ):
            ok = await wg_monitor._tick("wg0", [_PEER])
        assert ok is True
        run.assert_called_once_with("wg", "set", "wg0", "peer", "PUBKEY==", "endpoint", "5.6.7.8:51820")

    async def test_dns_failure_returns_false_skips_wg_set(self) -> None:
        with (
            patch("companion.wg._dns_lookup_ip", AsyncMock(return_value=None)),
            patch("companion.wg._run_wg_cmd", AsyncMock(return_value=(0, "", ""))) as run,
        ):
            ok = await wg_monitor._tick("wg0", [_PEER])
        assert ok is False
        run.assert_not_called()

    async def test_wg_set_failure_returns_false(self) -> None:
        with (
            patch("companion.wg._dns_lookup_ip", AsyncMock(return_value="5.6.7.8")),
            patch("companion.wg._run_wg_cmd", AsyncMock(return_value=(1, "", "permission denied"))),
        ):
            ok = await wg_monitor._tick("wg0", [_PEER])
        assert ok is False


# ---------------------------------------------------------------------------
# _is_peer_alive
# ---------------------------------------------------------------------------


class TestIsPeerAlive:
    async def test_fresh_handshake_returns_true(self) -> None:
        recent = int(time.time()) - 10
        dump = _dump("PUBKEY==", recent)
        with patch("companion.wg._run_wg_cmd", AsyncMock(return_value=(0, dump, ""))):
            assert await wg_monitor._is_peer_alive("wg0", [_PEER]) is True

    async def test_stale_handshake_returns_false(self) -> None:
        stale = int(time.time()) - (wg_monitor._STALE_HANDSHAKE + 20)
        dump = _dump("PUBKEY==", stale)
        with patch("companion.wg._run_wg_cmd", AsyncMock(return_value=(0, dump, ""))):
            assert await wg_monitor._is_peer_alive("wg0", [_PEER]) is False

    async def test_no_handshake_returns_false(self) -> None:
        dump = _dump("PUBKEY==", 0)
        with patch("companion.wg._run_wg_cmd", AsyncMock(return_value=(0, dump, ""))):
            assert await wg_monitor._is_peer_alive("wg0", [_PEER]) is False

    async def test_wg_command_fails_returns_false(self) -> None:
        with patch("companion.wg._run_wg_cmd", AsyncMock(return_value=(1, "", "error"))):
            assert await wg_monitor._is_peer_alive("wg0", [_PEER]) is False

    async def test_peer_not_in_dump_returns_false(self) -> None:
        dump = "(none)\tPUBLIC\t51820\t(none)\n"  # interface line only, no peers
        with patch("companion.wg._run_wg_cmd", AsyncMock(return_value=(0, dump, ""))):
            assert await wg_monitor._is_peer_alive("wg0", [_PEER]) is False


# ---------------------------------------------------------------------------
# _reconnect_loop
# ---------------------------------------------------------------------------


class TestReconnectLoop:
    async def test_returns_when_peer_reconnects(self) -> None:
        alive_seq = [False, False, True]
        alive_idx = 0
        sleep_calls: list[float] = []

        async def fake_alive(*_: object) -> bool:
            nonlocal alive_idx
            result = alive_seq[alive_idx]
            alive_idx += 1
            return result

        async def fake_sleep(delay: float) -> None:
            sleep_calls.append(delay)

        with (
            patch("companion.wg_monitor._tick", AsyncMock(return_value=True)),
            patch("companion.wg_monitor._is_peer_alive", side_effect=fake_alive),
            patch("companion.wg_monitor.asyncio.sleep", side_effect=fake_sleep),
        ):
            await wg_monitor._reconnect_loop("wg0", [_PEER])

        assert sleep_calls == [wg_monitor._BACKOFF[0], wg_monitor._BACKOFF[1], wg_monitor._BACKOFF[2]]

    async def test_backoff_sequence_matches_spec(self) -> None:
        call_count = 0
        sleep_calls: list[float] = []

        async def fake_sleep(delay: float) -> None:
            nonlocal call_count
            sleep_calls.append(delay)
            call_count += 1
            if call_count >= len(wg_monitor._BACKOFF):
                raise asyncio.CancelledError

        with (
            patch("companion.wg_monitor._tick", AsyncMock(return_value=True)),
            patch("companion.wg_monitor._is_peer_alive", AsyncMock(return_value=False)),
            patch("companion.wg_monitor.asyncio.sleep", side_effect=fake_sleep),
            contextlib.suppress(asyncio.CancelledError),
        ):
            await wg_monitor._reconnect_loop("wg0", [_PEER])

        assert sleep_calls == list(wg_monitor._BACKOFF)

    async def test_caps_at_max_backoff(self) -> None:
        extra = 3
        call_count = 0
        sleep_calls: list[float] = []
        max_calls = len(wg_monitor._BACKOFF) + extra

        async def fake_sleep(delay: float) -> None:
            nonlocal call_count
            sleep_calls.append(delay)
            call_count += 1
            if call_count >= max_calls:
                raise asyncio.CancelledError

        with (
            patch("companion.wg_monitor._tick", AsyncMock(return_value=True)),
            patch("companion.wg_monitor._is_peer_alive", AsyncMock(return_value=False)),
            patch("companion.wg_monitor.asyncio.sleep", side_effect=fake_sleep),
            contextlib.suppress(asyncio.CancelledError),
        ):
            await wg_monitor._reconnect_loop("wg0", [_PEER])

        assert sleep_calls[-extra:] == [wg_monitor._BACKOFF[-1]] * extra


# ---------------------------------------------------------------------------
# _monitor_loop
# ---------------------------------------------------------------------------


class TestMonitorLoop:
    async def test_healthy_peer_does_not_trigger_reconnect(self) -> None:
        sleep_count = 0

        async def fake_sleep(_: float) -> None:
            nonlocal sleep_count
            sleep_count += 1
            if sleep_count >= 2:
                raise asyncio.CancelledError

        with (
            patch("companion.wg_monitor._is_peer_alive", AsyncMock(return_value=True)),
            patch("companion.wg_monitor._reconnect_loop", AsyncMock()) as reconnect,
            patch("companion.wg_monitor.asyncio.sleep", side_effect=fake_sleep),
        ):
            await wg_monitor._monitor_loop("wg0", [_PEER])

        reconnect.assert_not_called()

    async def test_stale_peer_triggers_reconnect_loop(self) -> None:
        sleep_count = 0

        async def fake_sleep(_: float) -> None:
            nonlocal sleep_count
            sleep_count += 1
            if sleep_count >= 2:
                raise asyncio.CancelledError

        with (
            patch("companion.wg_monitor._is_peer_alive", AsyncMock(return_value=False)),
            patch("companion.wg_monitor._reconnect_loop", AsyncMock()) as reconnect,
            patch("companion.wg_monitor.asyncio.sleep", side_effect=fake_sleep),
        ):
            await wg_monitor._monitor_loop("wg0", [_PEER])

        reconnect.assert_called_once_with("wg0", [_PEER])

    async def test_cancel_is_clean(self) -> None:
        async def fake_sleep(_: float) -> None:
            raise asyncio.CancelledError

        with (
            patch("companion.wg_monitor._is_peer_alive", AsyncMock(return_value=True)),
            patch("companion.wg_monitor.asyncio.sleep", side_effect=fake_sleep),
        ):
            await wg_monitor._monitor_loop("wg0", [_PEER])  # must not raise


# ---------------------------------------------------------------------------
# start_monitor / stop_monitor
# ---------------------------------------------------------------------------


class TestStartStopMonitor:
    def setup_method(self) -> None:
        wg_monitor._monitors.clear()

    def teardown_method(self) -> None:
        for task in wg_monitor._monitors.values():
            task.cancel()
        wg_monitor._monitors.clear()

    async def test_no_task_created_for_ip_only_config(self) -> None:
        wg_monitor.start_monitor("wg0", _CONF_IP_ONLY)
        assert "wg0" not in wg_monitor._monitors

    async def test_task_created_for_hostname_config(self) -> None:
        wg_monitor.start_monitor("wg0", _CONF_WITH_HOSTNAME)
        assert "wg0" in wg_monitor._monitors
        wg_monitor._monitors["wg0"].cancel()

    async def test_stop_cancels_and_removes_task(self) -> None:
        wg_monitor.start_monitor("wg0", _CONF_WITH_HOSTNAME)
        task = wg_monitor._monitors["wg0"]
        wg_monitor.stop_monitor("wg0")
        assert "wg0" not in wg_monitor._monitors
        await asyncio.sleep(0)
        assert task.cancelled() or task.done()

    async def test_start_replaces_existing_monitor(self) -> None:
        wg_monitor.start_monitor("wg0", _CONF_WITH_HOSTNAME)
        first = wg_monitor._monitors["wg0"]
        wg_monitor.start_monitor("wg0", _CONF_WITH_HOSTNAME)
        second = wg_monitor._monitors["wg0"]
        assert first is not second
        await asyncio.sleep(0)
        assert first.cancelled() or first.done()
        second.cancel()

    def test_stop_noop_when_no_monitor(self) -> None:
        wg_monitor.stop_monitor("wg0")  # must not raise
