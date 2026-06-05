"""Unit tests for the WireGuard startup supervisor."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

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
            await reconcile(opts, fallback_dir=fallback_dir)
        run_cmd.assert_called_with("wg-quick", "down", "wg0")


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
        run_cmd.assert_called_with("wg-quick", "up", "wg0")
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
        run_cmd.assert_called_with("wg-quick", "up", "wg0")

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

    async def test_monitor_started_after_tunnel_up(self, wg_conf_dir: Path, fallback_dir: Path) -> None:
        opts = VPNOptions(enabled=True, tunnel="wg0", config=_HOSTNAME_CONF)
        with (
            patch("companion.wg_supervisor.wg._is_interface_up", AsyncMock(return_value=False)),
            patch("companion.wg_supervisor.wg._run_wg_cmd", AsyncMock(return_value=(0, "", ""))),
            patch("companion.wg_supervisor.wg._resolve_endpoint_hostnames", AsyncMock(return_value=[])),
            patch("companion.wg_supervisor.wg_monitor.start_monitor") as start_mock,
        ):
            await reconcile(opts, fallback_dir=fallback_dir)
        start_mock.assert_called_once()
        assert start_mock.call_args.args[0] == "wg0"

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
            await reconcile(opts, fallback_dir=fallback_dir)
        assert not (wg_conf_dir / "wg0.conf").exists()
        assert not any("wg-quick" in str(c.args) for c in run_cmd.call_args_list)
        assert any("rejected" in rec.message for rec in caplog.records)
