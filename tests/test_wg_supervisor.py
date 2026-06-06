"""Unit tests for the WireGuard startup supervisor."""

from __future__ import annotations

import asyncio
import contextlib
import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from aiohttp import web

from companion import wg_supervisor
from companion.wg_supervisor import VPNOptions, load_options, reconcile

# Normalized form (no inter-section blank line) so it round-trips byte-for-byte
# through save_config's config normalizer.
_VALID_CONF = """[Interface]
PrivateKey = aaaa
Address = 10.0.0.2/24
[Peer]
PublicKey = bbbb
AllowedIPs = 0.0.0.0/0
"""

_HOSTNAME_CONF = """[Interface]
PrivateKey = aaaa
Address = 10.0.0.2/24
[Peer]
PublicKey = bbbb
Endpoint = home.example.com:51826
AllowedIPs = 0.0.0.0/0
"""


@pytest.fixture
def wg_conf_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect _WG_CONFIG_DIR to a tmp path so writes don't escape the sandbox."""
    monkeypatch.setattr("companion.wg._WG_CONFIG_DIR", tmp_path)
    return tmp_path


@pytest.fixture
def fallback_dir(tmp_path: Path) -> Path:
    d = tmp_path / "fallback"
    d.mkdir()
    return d


# ---------------------------------------------------------------------------
# load_options
# ---------------------------------------------------------------------------


class TestLoadOptions:
    def test_missing_file(self, tmp_path: Path) -> None:
        assert load_options(tmp_path / "nope.json") is None

    def test_missing_vpn_section(self, tmp_path: Path) -> None:
        p = tmp_path / "options.json"
        p.write_text(json.dumps({"other": {}}))
        assert load_options(p) is None

    def test_invalid_json(self, tmp_path: Path) -> None:
        p = tmp_path / "options.json"
        p.write_text("not json")
        assert load_options(p) is None

    def test_minimal_valid(self, tmp_path: Path) -> None:
        p = tmp_path / "options.json"
        p.write_text(json.dumps({"vpn": {"enabled": True}}))
        opts = load_options(p)
        assert opts is not None
        assert opts.enabled is True
        assert opts.tunnel == "wg0"
        assert opts.config == ""

    def test_full(self, tmp_path: Path) -> None:
        p = tmp_path / "options.json"
        p.write_text(
            json.dumps(
                {
                    "vpn": {
                        "enabled": True,
                        "tunnel": "wg1",
                        "config": _VALID_CONF,
                    }
                }
            )
        )
        opts = load_options(p)
        assert opts is not None
        assert opts.enabled is True
        assert opts.tunnel == "wg1"
        assert opts.config == _VALID_CONF

    def test_rejects_bad_tunnel_name(self, tmp_path: Path) -> None:
        p = tmp_path / "options.json"
        p.write_text(json.dumps({"vpn": {"enabled": True, "tunnel": "bad name"}}))
        assert load_options(p) is None


# ---------------------------------------------------------------------------
# reconcile
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestReconcileDisabled:
    async def test_noop_when_disabled_and_down(self, wg_conf_dir: Path, fallback_dir: Path) -> None:
        opts = VPNOptions(enabled=False, tunnel="wg0", config="")
        with (
            patch("companion.wg_supervisor.wg._is_interface_up", AsyncMock(return_value=False)),
            patch("companion.wg_supervisor.wg._run_wg_cmd", AsyncMock(return_value=(0, "", ""))) as run_cmd,
        ):
            await reconcile(opts, fallback_dir=fallback_dir)
        # wg-quick down should NOT be called when already down
        assert not any(call.args[:2] == ("wg-quick", "down") for call in run_cmd.call_args_list)

    async def test_brings_down_when_disabled_and_up(self, wg_conf_dir: Path, fallback_dir: Path) -> None:
        opts = VPNOptions(enabled=False, tunnel="wg0", config="")
        with (
            patch("companion.wg_supervisor.wg._is_interface_up", AsyncMock(return_value=True)),
            patch("companion.wg_supervisor.wg._run_wg_cmd", AsyncMock(return_value=(0, "", ""))) as run_cmd,
        ):
            result = await reconcile(opts, fallback_dir=fallback_dir)
        run_cmd.assert_called_with("wg-quick", "down", "wg0")
        assert result is None  # disabled → nothing for the server loop to watch


@pytest.mark.asyncio
class TestReconcileEnabled:
    async def test_starts_with_inline_config(self, wg_conf_dir: Path, fallback_dir: Path) -> None:
        opts = VPNOptions(enabled=True, tunnel="wg0", config=_VALID_CONF)
        with (
            patch("companion.wg_supervisor.wg._is_interface_up", AsyncMock(return_value=False)),
            patch("companion.wg_supervisor.wg._run_wg_cmd", AsyncMock(return_value=(0, "", ""))) as run_cmd,
            patch("companion.wg_supervisor.wg._resolve_endpoint_hostnames", AsyncMock(return_value=[])),
            patch("companion.wg_supervisor.wg_monitor.start_monitor"),
        ):
            await reconcile(opts, fallback_dir=fallback_dir)
        conf_path = wg_conf_dir / "wg0.conf"
        assert conf_path.exists()
        assert conf_path.read_text() == _VALID_CONF
        assert conf_path.stat().st_mode & 0o777 == 0o600
        run_cmd.assert_any_call("wg-quick", "up", "wg0")
        # Inline vpn.config is now synced into the canonical persistent file, so
        # it survives and stays consistent with what hactl would read/write.
        persisted = fallback_dir / "wg0.conf"
        assert persisted.read_text() == _VALID_CONF
        assert persisted.stat().st_mode & 0o777 == 0o600

    async def test_falls_back_to_file(self, wg_conf_dir: Path, fallback_dir: Path) -> None:
        (fallback_dir / "wg0.conf").write_text(_VALID_CONF)
        opts = VPNOptions(enabled=True, tunnel="wg0", config="")
        with (
            patch("companion.wg_supervisor.wg._is_interface_up", AsyncMock(return_value=False)),
            patch("companion.wg_supervisor.wg._run_wg_cmd", AsyncMock(return_value=(0, "", ""))) as run_cmd,
            patch("companion.wg_supervisor.wg._resolve_endpoint_hostnames", AsyncMock(return_value=[])),
            patch("companion.wg_supervisor.wg_monitor.start_monitor"),
        ):
            await reconcile(opts, fallback_dir=fallback_dir)
        assert (wg_conf_dir / "wg0.conf").read_text() == _VALID_CONF
        run_cmd.assert_any_call("wg-quick", "up", "wg0")

    async def test_dns_failure_logs_and_skips(
        self,
        wg_conf_dir: Path,
        fallback_dir: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        opts = VPNOptions(enabled=True, tunnel="wg0", config=_HOSTNAME_CONF)
        with (
            patch("companion.wg_supervisor.wg._is_interface_up", AsyncMock(return_value=False)),
            patch("companion.wg_supervisor.wg._run_wg_cmd", AsyncMock(return_value=(0, "", ""))) as run_cmd,
            patch(
                "companion.wg_supervisor.wg._resolve_endpoint_hostnames",
                AsyncMock(return_value=["home.example.com"]),
            ),
            caplog.at_level("WARNING", logger=wg_supervisor.logger.name),
        ):
            await reconcile(opts, fallback_dir=fallback_dir)
        # wg-quick must NOT be called
        assert not any("wg-quick" in str(c.args) for c in run_cmd.call_args_list)
        assert any("DNS resolution failed" in rec.message for rec in caplog.records)

    async def test_logs_up_summary_and_returns_result(
        self,
        wg_conf_dir: Path,
        fallback_dir: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        opts = VPNOptions(enabled=True, tunnel="wg0", config=_VALID_CONF)
        dump = "PRIV\tIFACE\t51820\toff\nPEER\t(none)\t1.2.3.4:51826\t10.6.0.0/24\t0\t184\t584\toff\n"

        async def fake_run(*args: str, timeout: int = 30) -> tuple[int, str, str]:
            if args[:2] == ("wg", "show"):
                return (0, dump, "")
            return (0, "", "")

        with (
            patch("companion.wg_supervisor.wg._is_interface_up", AsyncMock(return_value=False)),
            patch("companion.wg_supervisor.wg._run_wg_cmd", AsyncMock(side_effect=fake_run)),
            patch("companion.wg_supervisor.wg._resolve_endpoint_hostnames", AsyncMock(return_value=[])),
            caplog.at_level("INFO", logger=wg_supervisor.logger.name),
        ):
            result = await reconcile(opts, fallback_dir=fallback_dir)
        # Honest "interface configured" line — no connectivity claim here.
        summary = [r.message for r in caplog.records if r.message.startswith("wg wg0 up")]
        assert summary, f"no up-summary logged; got {[r.message for r in caplog.records]}"
        assert "peer 1.2.3.4:51826" in summary[0]
        # reconcile no longer claims "active"/"connected"
        assert not any("connected" in r.message or "active" in r.message for r in caplog.records)
        assert result is not None
        assert result.tunnel == "wg0"
        assert result.conf_text == _VALID_CONF

    async def test_reconcile_does_not_start_monitor(self, wg_conf_dir: Path, fallback_dir: Path) -> None:
        # Monitor start now happens in the server loop (register_startup_tasks),
        # not in reconcile (whose asyncio.run loop would cancel it).
        opts = VPNOptions(enabled=True, tunnel="wg0", config=_HOSTNAME_CONF)
        with (
            patch("companion.wg_supervisor.wg._is_interface_up", AsyncMock(return_value=False)),
            patch("companion.wg_supervisor.wg._run_wg_cmd", AsyncMock(return_value=(0, "", ""))),
            patch("companion.wg_supervisor.wg._resolve_endpoint_hostnames", AsyncMock(return_value=[])),
            patch("companion.wg_supervisor.wg_monitor.start_monitor") as start_mock,
        ):
            result = await reconcile(opts, fallback_dir=fallback_dir)
        start_mock.assert_not_called()
        assert result is not None and result.tunnel == "wg0"

    async def test_no_config_anywhere_logs_and_skips(
        self,
        wg_conf_dir: Path,
        fallback_dir: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        opts = VPNOptions(enabled=True, tunnel="wg0", config="")
        with (
            patch("companion.wg_supervisor.wg._is_interface_up", AsyncMock(return_value=False)),
            patch("companion.wg_supervisor.wg._run_wg_cmd", AsyncMock(return_value=(0, "", ""))) as run_cmd,
            caplog.at_level("WARNING", logger=wg_supervisor.logger.name),
        ):
            await reconcile(opts, fallback_dir=fallback_dir)
        assert not (wg_conf_dir / "wg0.conf").exists()
        # wg-quick must NOT be called when there is no config
        assert not any("wg-quick" in str(c.args) for c in run_cmd.call_args_list)
        assert any("no config provided" in rec.message for rec in caplog.records)

    async def test_already_up_does_not_call_wg_quick_up(
        self,
        wg_conf_dir: Path,
        fallback_dir: Path,
    ) -> None:
        opts = VPNOptions(enabled=True, tunnel="wg0", config=_VALID_CONF)
        with (
            patch("companion.wg_supervisor.wg._is_interface_up", AsyncMock(return_value=True)),
            patch("companion.wg_supervisor.wg._run_wg_cmd", AsyncMock(return_value=(0, "", ""))) as run_cmd,
            patch("companion.wg_supervisor.wg._resolve_endpoint_hostnames", AsyncMock(return_value=[])),
            patch("companion.wg_supervisor.wg_monitor.start_monitor"),
        ):
            await reconcile(opts, fallback_dir=fallback_dir)
        assert not any(call.args[:2] == ("wg-quick", "up") for call in run_cmd.call_args_list)

    async def test_invalid_config_logs_and_skips(
        self,
        wg_conf_dir: Path,
        fallback_dir: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        opts = VPNOptions(enabled=True, tunnel="wg0", config="garbage")
        with (
            patch("companion.wg_supervisor.wg._is_interface_up", AsyncMock(return_value=False)),
            patch("companion.wg_supervisor.wg._run_wg_cmd", AsyncMock(return_value=(0, "", ""))) as run_cmd,
            caplog.at_level("WARNING", logger=wg_supervisor.logger.name),
        ):
            result = await reconcile(opts, fallback_dir=fallback_dir)
        assert not (wg_conf_dir / "wg0.conf").exists()
        assert not any("wg-quick" in str(c.args) for c in run_cmd.call_args_list)
        assert any("rejected" in rec.message for rec in caplog.records)
        assert result is None


_HANDSHAKE_DUMP = "PRIV\tIFACE\t51820\toff\nPEER\t(none)\t1.2.3.4:51826\t10.6.0.0/24\t{hs}\t184\t584\toff\n"


@pytest.mark.asyncio
class TestWatchConnection:
    async def test_logs_connected_on_handshake(self, caplog: pytest.LogCaptureFixture) -> None:
        import time

        recent = int(time.time())  # fresh handshake → within staleness window
        dump = _HANDSHAKE_DUMP.format(hs=recent)
        with (
            patch("companion.wg_supervisor.wg._run_wg_cmd", AsyncMock(return_value=(0, dump, ""))),
            caplog.at_level("INFO", logger=wg_supervisor.logger.name),
        ):
            await wg_supervisor._watch_connection("wg0", timeout=2.0, interval=0.01)
        connected = [r.message for r in caplog.records if "connected" in r.message]
        assert connected, f"no connected line; got {[r.message for r in caplog.records]}"
        assert "rx=184 B tx=584 B" in connected[0]

    async def test_warns_when_no_handshake(self, caplog: pytest.LogCaptureFixture) -> None:
        dump = _HANDSHAKE_DUMP.format(hs=0)  # never handshaked
        with (
            patch("companion.wg_supervisor.wg._run_wg_cmd", AsyncMock(return_value=(0, dump, ""))),
            caplog.at_level("WARNING", logger=wg_supervisor.logger.name),
        ):
            await wg_supervisor._watch_connection("wg0", timeout=0.05, interval=0.01)
        warnings = [r.message for r in caplog.records if "NOT connected" in r.message]
        assert warnings, f"no not-connected warning; got {[r.message for r in caplog.records]}"
        assert "1.2.3.4:51826" in warnings[0]


@pytest.mark.asyncio
class TestRegisterStartupTasks:
    async def test_on_startup_starts_monitor_and_watcher(self) -> None:
        app = web.Application()
        wg_supervisor.register_startup_tasks(app, "wg0", _VALID_CONF)
        assert app.on_startup and app.on_cleanup

        with (
            patch("companion.wg_supervisor.wg_monitor.start_monitor") as start_mock,
            patch("companion.wg_supervisor.wg_monitor.stop_monitor") as stop_mock,
            patch("companion.wg_supervisor._watch_connection", AsyncMock()),
        ):
            for handler in app.on_startup:
                await handler(app)
            start_mock.assert_called_once_with("wg0", _VALID_CONF)
            assert "_wg_watch" in app and isinstance(app["_wg_watch"], asyncio.Task)
            for handler in app.on_cleanup:
                await handler(app)
            stop_mock.assert_called_once_with("wg0")
            with contextlib.suppress(asyncio.CancelledError):
                await app["_wg_watch"]
            assert app["_wg_watch"].cancelled() or app["_wg_watch"].done()
