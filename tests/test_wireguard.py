"""Unit tests for WireGuard route module."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from companion.wg import (
    _conf_from_json,
    _humanize_age,
    _humanize_bytes,
    _normalize_conf,
    _parse_wg_dump,
    _validate_conf,
    _validate_tunnel,
    materialize,
    save_config,
)

# ---------------------------------------------------------------------------
# _validate_tunnel
# ---------------------------------------------------------------------------


class TestValidateTunnel:
    def test_valid_name(self) -> None:
        assert _validate_tunnel("wg0") == "wg0"

    def test_valid_underscore(self) -> None:
        assert _validate_tunnel("my_tunnel") == "my_tunnel"

    def test_max_length(self) -> None:
        assert _validate_tunnel("a" * 15) == "a" * 15

    def test_too_long(self) -> None:
        from aiohttp.web import HTTPBadRequest

        with pytest.raises(HTTPBadRequest):
            _validate_tunnel("a" * 16)

    def test_empty(self) -> None:
        from aiohttp.web import HTTPBadRequest

        with pytest.raises(HTTPBadRequest):
            _validate_tunnel("")

    def test_injection_semicolon(self) -> None:
        from aiohttp.web import HTTPBadRequest

        with pytest.raises(HTTPBadRequest):
            _validate_tunnel("; rm -rf /")

    def test_injection_path_traversal(self) -> None:
        from aiohttp.web import HTTPBadRequest

        with pytest.raises(HTTPBadRequest):
            _validate_tunnel("../etc")

    def test_injection_pipe(self) -> None:
        from aiohttp.web import HTTPBadRequest

        with pytest.raises(HTTPBadRequest):
            _validate_tunnel("wg0|cat")

    def test_spaces(self) -> None:
        from aiohttp.web import HTTPBadRequest

        with pytest.raises(HTTPBadRequest):
            _validate_tunnel("wg 0")


# ---------------------------------------------------------------------------
# _conf_from_json
# ---------------------------------------------------------------------------


class TestConfFromJson:
    def test_minimal_valid(self) -> None:
        data = {
            "interface": {"private_key": "AAAA", "address": "10.0.0.1/24"},
            "peers": [{"public_key": "BBBB", "allowed_ips": "0.0.0.0/0"}],
        }
        conf = _conf_from_json(data)
        assert "[Interface]" in conf
        assert "PrivateKey = AAAA" in conf
        assert "Address = 10.0.0.1/24" in conf
        assert "[Peer]" in conf
        assert "PublicKey = BBBB" in conf
        assert "AllowedIPs = 0.0.0.0/0" in conf

    def test_full_config(self) -> None:
        data = {
            "interface": {"private_key": "KEY", "address": "10.0.0.2/24", "dns": "1.1.1.1"},
            "peers": [
                {
                    "public_key": "PUB",
                    "endpoint": "vpn.example.com:51820",
                    "allowed_ips": "0.0.0.0/0",
                    "persistent_keepalive": 25,
                }
            ],
        }
        conf = _conf_from_json(data)
        assert "DNS = 1.1.1.1" in conf
        assert "Endpoint = vpn.example.com:51820" in conf
        assert "PersistentKeepalive = 25" in conf

    def test_missing_interface(self) -> None:
        from aiohttp.web import HTTPBadRequest

        with pytest.raises(HTTPBadRequest):
            _conf_from_json({"peers": [{"public_key": "X", "allowed_ips": "0/0"}]})

    def test_missing_private_key(self) -> None:
        from aiohttp.web import HTTPBadRequest

        with pytest.raises(HTTPBadRequest):
            _conf_from_json(
                {"interface": {"address": "10.0.0.1/24"}, "peers": [{"public_key": "X", "allowed_ips": "0/0"}]}
            )

    def test_missing_peers(self) -> None:
        from aiohttp.web import HTTPBadRequest

        with pytest.raises(HTTPBadRequest):
            _conf_from_json({"interface": {"private_key": "K", "address": "10.0.0.1/24"}})

    def test_empty_peers(self) -> None:
        from aiohttp.web import HTTPBadRequest

        with pytest.raises(HTTPBadRequest):
            _conf_from_json({"interface": {"private_key": "K", "address": "10.0.0.1/24"}, "peers": []})

    def test_peer_missing_public_key(self) -> None:
        from aiohttp.web import HTTPBadRequest

        with pytest.raises(HTTPBadRequest):
            _conf_from_json(
                {
                    "interface": {"private_key": "K", "address": "10.0.0.1/24"},
                    "peers": [{"allowed_ips": "0/0"}],
                }
            )


# ---------------------------------------------------------------------------
# _validate_conf
# ---------------------------------------------------------------------------


class TestValidateConf:
    def test_valid(self) -> None:
        conf = "[Interface]\nPrivateKey = X\nAddress = 10.0.0.1/24\n\n[Peer]\nPublicKey = Y\nAllowedIPs = 0/0\n"
        _validate_conf(conf)  # Should not raise

    def test_missing_interface(self) -> None:
        from aiohttp.web import HTTPBadRequest

        with pytest.raises(HTTPBadRequest):
            _validate_conf("[Peer]\nPublicKey = Y\nAllowedIPs = 0/0\n")

    def test_missing_peer(self) -> None:
        from aiohttp.web import HTTPBadRequest

        with pytest.raises(HTTPBadRequest):
            _validate_conf("[Interface]\nPrivateKey = X\n")

    def test_missing_private_key(self) -> None:
        from aiohttp.web import HTTPBadRequest

        with pytest.raises(HTTPBadRequest):
            _validate_conf("[Interface]\nAddress = 10.0.0.1/24\n\n[Peer]\nPublicKey = Y\n")


# ---------------------------------------------------------------------------
# _parse_wg_dump / humanizers
# ---------------------------------------------------------------------------


class TestParseWgDump:
    # interface: priv pub listen_port fwmark
    # peer:      pub psk endpoint allowed_ips handshake rx tx keepalive
    def test_full_output(self) -> None:
        output = "PRIV\tAAAA\t51820\toff\nBBBB\t(none)\t1.2.3.4:51820\t10.0.0.0/24\t1894\t1260\t4669\t25\n"
        result = _parse_wg_dump(output, now=2000)
        assert result["interface"]["public_key"] == "AAAA"
        assert result["interface"]["listening_port"] == 51820
        assert len(result["peers"]) == 1
        peer = result["peers"][0]
        assert peer["public_key"] == "BBBB"
        assert peer["endpoint"] == "1.2.3.4:51820"
        assert peer["allowed_ips"] == "10.0.0.0/24"
        assert peer["latest_handshake_secs"] == 106
        assert peer["latest_handshake"] == "1m46s"
        assert peer["transfer_rx_bytes"] == 1260
        assert peer["transfer_tx_bytes"] == 4669
        assert peer["transfer_rx"] == "1.23 KiB"
        assert peer["transfer_tx"] == "4.56 KiB"

    def test_never_handshaked(self) -> None:
        output = "PRIV\tAAAA\t51820\toff\nBBBB\t(none)\t(none)\t10.0.0.0/24\t0\t0\t0\toff\n"
        result = _parse_wg_dump(output, now=2000)
        peer = result["peers"][0]
        assert peer["latest_handshake_secs"] is None
        assert peer["latest_handshake"] == "never"
        assert peer["endpoint"] == ""

    def test_no_peers(self) -> None:
        result = _parse_wg_dump("PRIV\tAAAA\t51820\toff\n", now=2000)
        assert result["interface"]["public_key"] == "AAAA"
        assert result["peers"] == []

    def test_empty_output(self) -> None:
        result = _parse_wg_dump("", now=2000)
        assert result["interface"] == {}
        assert result["peers"] == []


class TestHumanizers:
    def test_bytes(self) -> None:
        assert _humanize_bytes(0) == "0 B"
        assert _humanize_bytes(512) == "512 B"
        assert _humanize_bytes(1260) == "1.23 KiB"
        assert _humanize_bytes(5 * 1024 * 1024) == "5.00 MiB"

    def test_age(self) -> None:
        assert _humanize_age(5) == "5s"
        assert _humanize_age(106) == "1m46s"
        assert _humanize_age(3700) == "1h1m"
        assert _humanize_age(90000) == "1d1h"


# ---------------------------------------------------------------------------
# HTTP endpoint tests
# ---------------------------------------------------------------------------


def _mock_subprocess(returncode: int = 0, stdout: str = "", stderr: str = "") -> AsyncMock:
    """Create a mock for asyncio.create_subprocess_exec."""
    mock_proc = AsyncMock()
    mock_proc.returncode = returncode
    mock_proc.communicate = AsyncMock(return_value=(stdout.encode(), stderr.encode()))
    mock_create = AsyncMock(return_value=mock_proc)
    return mock_create


class TestPostConfig:
    @pytest.mark.usefixtures("_wg_config_dir")
    async def test_raw_conf(self, client, auth_headers, _wg_config_dir) -> None:
        conf = "[Interface]\nPrivateKey = X\nAddress = 10.0.0.1/24\n\n[Peer]\nPublicKey = Y\nAllowedIPs = 0/0\n"
        resp = await client.post(
            "/v1/wireguard/config?tunnel=wg0",
            data=conf,
            headers={**auth_headers, "Content-Type": "text/plain"},
        )
        assert resp.status == 200
        body = await resp.json()
        assert body["status"] == "configured"
        assert body["tunnel"] == "wg0"
        # Persisted to the source-of-truth dir (not just /etc/wireguard).
        persisted = _wg_config_dir / "wg0.conf"
        assert persisted.exists()
        assert "PrivateKey = X" in persisted.read_text()
        assert (persisted.stat().st_mode & 0o077) == 0  # 0600 — key not group/world readable

    @pytest.mark.usefixtures("_wg_config_dir")
    async def test_json_conf(self, client, auth_headers, _wg_config_dir) -> None:
        data = {
            "tunnel_name": "vpn1",
            "interface": {"private_key": "K", "address": "10.0.0.1/24"},
            "peers": [{"public_key": "P", "allowed_ips": "0.0.0.0/0"}],
        }
        resp = await client.post(
            "/v1/wireguard/config",
            json=data,
            headers=auth_headers,
        )
        assert resp.status == 200
        body = await resp.json()
        assert body["tunnel"] == "vpn1"
        assert (_wg_config_dir / "vpn1.conf").exists()

    async def test_empty_body(self, client, auth_headers) -> None:
        resp = await client.post(
            "/v1/wireguard/config",
            data=b"",
            headers={**auth_headers, "Content-Type": "text/plain"},
        )
        assert resp.status == 400

    async def test_invalid_tunnel(self, client, auth_headers) -> None:
        resp = await client.post(
            "/v1/wireguard/config?tunnel=../etc",
            data=b"[Interface]\nPrivateKey=X\n[Peer]\nPublicKey=Y\n",
            headers={**auth_headers, "Content-Type": "text/plain"},
        )
        assert resp.status == 400

    async def test_requires_auth(self, client) -> None:
        resp = await client.post("/v1/wireguard/config")
        assert resp.status == 401


@pytest.mark.usefixtures("_wg_config_dir")
class TestPostStart:
    @staticmethod
    def _persist(_wg_config_dir, tunnel: str = "wg0") -> None:
        _wg_config_dir.mkdir(parents=True, exist_ok=True)
        (_wg_config_dir / f"{tunnel}.conf").write_text(
            "[Interface]\nPrivateKey = X\nAddress = 10.0.0.1/24\n\n[Peer]\nPublicKey = Y\nAllowedIPs = 0/0\n"
        )

    @staticmethod
    def _persist_with_hostname(_wg_config_dir, tunnel: str = "wg0") -> None:
        _wg_config_dir.mkdir(parents=True, exist_ok=True)
        (_wg_config_dir / f"{tunnel}.conf").write_text(
            "[Interface]\nPrivateKey = X\nAddress = 10.0.0.1/24\n"
            "[Peer]\nPublicKey = Y\nEndpoint = vpn.example.com:51820\nAllowedIPs = 0/0\n"
        )

    async def test_start_success(self, client, auth_headers, _wg_config_dir) -> None:
        self._persist(_wg_config_dir)
        with (
            patch("companion.routes.wireguard._is_interface_up", return_value=False),
            patch("companion.routes.wireguard._run_wg_cmd", return_value=(0, "", "")),
            patch("companion.routes.wireguard._resolve_endpoint_hostnames", AsyncMock(return_value=[])),
            patch("companion.routes.wireguard.wg_monitor.start_monitor"),
        ):
            resp = await client.post("/v1/wireguard/start?tunnel=wg0", headers=auth_headers)
        assert resp.status == 200
        body = await resp.json()
        assert body["status"] == "started"

    async def test_start_missing_config(self, client, auth_headers) -> None:
        """No persistent config → 404, not a confusing wg-quick 500."""
        with patch("companion.routes.wireguard._is_interface_up", return_value=False):
            resp = await client.post("/v1/wireguard/start?tunnel=wg0", headers=auth_headers)
        assert resp.status == 404

    async def test_start_already_up(self, client, auth_headers) -> None:
        with patch("companion.routes.wireguard._is_interface_up", return_value=True):
            resp = await client.post("/v1/wireguard/start?tunnel=wg0", headers=auth_headers)
        assert resp.status == 409

    async def test_start_failure(self, client, auth_headers, _wg_config_dir) -> None:
        self._persist(_wg_config_dir)
        with (
            patch("companion.routes.wireguard._is_interface_up", return_value=False),
            patch("companion.routes.wireguard._resolve_endpoint_hostnames", AsyncMock(return_value=[])),
            patch("companion.routes.wireguard._run_wg_cmd", return_value=(1, "", "error")),
        ):
            resp = await client.post("/v1/wireguard/start?tunnel=wg0", headers=auth_headers)
        assert resp.status == 500

    async def test_start_dns_failure_returns_502(self, client, auth_headers, _wg_config_dir) -> None:
        self._persist_with_hostname(_wg_config_dir)
        with (
            patch("companion.routes.wireguard._is_interface_up", return_value=False),
            patch(
                "companion.routes.wireguard._resolve_endpoint_hostnames",
                AsyncMock(return_value=["vpn.example.com"]),
            ),
            patch("companion.routes.wireguard._run_wg_cmd", return_value=(0, "", "")) as run_cmd,
        ):
            resp = await client.post("/v1/wireguard/start?tunnel=wg0", headers=auth_headers)
        assert resp.status == 502
        text = await resp.text()
        assert "vpn.example.com" in text
        # wg-quick must NOT be called when DNS fails
        assert not any("wg-quick" in str(c.args) for c in run_cmd.call_args_list)

    async def test_start_monitor_is_started(self, client, auth_headers, _wg_config_dir) -> None:
        self._persist_with_hostname(_wg_config_dir)
        with (
            patch("companion.routes.wireguard._is_interface_up", return_value=False),
            patch("companion.routes.wireguard._resolve_endpoint_hostnames", AsyncMock(return_value=[])),
            patch("companion.routes.wireguard._run_wg_cmd", return_value=(0, "", "")),
            patch("companion.routes.wireguard.wg_monitor.start_monitor") as start_mock,
        ):
            resp = await client.post("/v1/wireguard/start?tunnel=wg0", headers=auth_headers)
        assert resp.status == 200
        start_mock.assert_called_once()
        assert start_mock.call_args.args[0] == "wg0"


class TestPostStop:
    async def test_stop_success(self, client, auth_headers) -> None:
        with (
            patch("companion.routes.wireguard._is_interface_up", return_value=True),
            patch("companion.routes.wireguard._run_wg_cmd", return_value=(0, "", "")),
            patch("companion.routes.wireguard.wg_monitor.stop_monitor") as stop_mock,
        ):
            resp = await client.post("/v1/wireguard/stop?tunnel=wg0", headers=auth_headers)
        assert resp.status == 200
        body = await resp.json()
        assert body["status"] == "stopped"
        stop_mock.assert_called_once_with("wg0")

    async def test_stop_not_running(self, client, auth_headers) -> None:
        with patch("companion.routes.wireguard._is_interface_up", return_value=False):
            resp = await client.post("/v1/wireguard/stop?tunnel=wg0", headers=auth_headers)
        assert resp.status == 409


class TestGetStatus:
    async def test_status_active(self, client, auth_headers) -> None:
        # `wg show <tunnel> dump`: interface row then one peer row.
        wg_output = "PRIV\tAAAA\t51820\toff\nBBBB\t(none)\t1.2.3.4:51820\t10.0.0.0/24\t0\t1260\t4669\toff\n"

        async def _mock_run(*args: str, timeout: int = 30) -> tuple[int, str, str]:
            return (0, wg_output, "")

        with (
            patch("companion.routes.wireguard._is_interface_up", return_value=True),
            patch("companion.routes.wireguard._run_wg_cmd", side_effect=_mock_run),
        ):
            resp = await client.get("/v1/wireguard/status?tunnel=wg0", headers=auth_headers)
        assert resp.status == 200
        body = await resp.json()
        assert body["state"] == "active"
        assert body["tunnel"] == "wg0"
        assert body["interface"]["public_key"] == "AAAA"
        assert len(body["peers"]) == 1
        assert body["peers"][0]["transfer_rx"] == "1.23 KiB"
        # No monitor is running for this tunnel in the test.
        assert body["monitor"] == {"running": False}
        assert "auto_enable" not in body

    async def test_status_inactive(self, client, auth_headers) -> None:
        with patch("companion.routes.wireguard._is_interface_up", return_value=False):
            resp = await client.get("/v1/wireguard/status?tunnel=wg0", headers=auth_headers)
        assert resp.status == 200
        body = await resp.json()
        assert body["state"] == "inactive"


# Already in normalized form (no inter-section blank line) so it round-trips
# byte-for-byte through save_config's normalizer.
_CONF = "[Interface]\nPrivateKey = X\nAddress = 10.0.0.1/24\n[Peer]\nPublicKey = Y\nAllowedIPs = 0/0\n"


class TestNormalizeConf:
    # The exact value an HA add-on options text field produced from a pasted
    # multi-line config — all newlines (and spaces) stripped.
    COLLAPSED = (
        "[Interface]PrivateKey=yLaRKvrkz+DhPait/rk5OgOV2RGeikMkX/dbK8gxiHo=Address=10.6.0.2/24"
        "[Peer]PublicKey=FE5OhQCNLLxF1OdDBIDMf5ktc8sEFngHoxy2o5iMhxs="
        "Endpoint=home.kippings.de:51826AllowedIPs=10.6.0.0/24PersistentKeepalive=25"
    )

    def test_reconstructs_collapsed_paste(self) -> None:
        out = _normalize_conf(self.COLLAPSED)
        _validate_conf(out)  # must not raise
        assert out.splitlines() == [
            "[Interface]",
            "PrivateKey = yLaRKvrkz+DhPait/rk5OgOV2RGeikMkX/dbK8gxiHo=",
            "Address = 10.6.0.2/24",
            "[Peer]",
            "PublicKey = FE5OhQCNLLxF1OdDBIDMf5ktc8sEFngHoxy2o5iMhxs=",
            "Endpoint = home.kippings.de:51826",
            "AllowedIPs = 10.6.0.0/24",
            "PersistentKeepalive = 25",
        ]

    def test_idempotent_on_well_formed(self) -> None:
        well_formed = (
            "[Interface]\nPrivateKey = aaa=\nAddress = 10.0.0.2/24\n\n"
            "[Peer]\nPublicKey = bbb=\nAllowedIPs = 0.0.0.0/0\n"
        )
        once = _normalize_conf(well_formed)
        assert _normalize_conf(once) == once

    def test_splits_when_value_ends_in_letter(self) -> None:
        # Endpoint hostname ending in a letter, abutting the next key.
        collapsed = (
            "[Interface]PrivateKey=k=Address=10.0.0.2/24[Peer]PublicKey=p=Endpoint=vpn.example.comAllowedIPs=0.0.0.0/0"
        )
        out = _normalize_conf(collapsed)
        assert "Endpoint = vpn.example.com" in out
        assert "AllowedIPs = 0.0.0.0/0" in out

    def test_preserves_base64_key_values(self) -> None:
        out = _normalize_conf(self.COLLAPSED)
        # Key material (incl. '+' '/' '=') must survive verbatim.
        assert "PrivateKey = yLaRKvrkz+DhPait/rk5OgOV2RGeikMkX/dbK8gxiHo=" in out

    def test_save_config_normalizes(self, tmp_path, monkeypatch) -> None:
        """A collapsed paste pushed through save_config lands as a valid file."""
        persist = tmp_path / "persist"
        monkeypatch.setattr("companion.wg._PERSIST_DIR", persist)
        monkeypatch.setattr("companion.wg._WG_CONFIG_DIR", tmp_path / "runtime")
        save_config("wg0", self.COLLAPSED)
        written = (persist / "wg0.conf").read_text()
        _validate_conf(written)
        assert "\nAddress = 10.6.0.2/24\n" in written


class TestSaveAndMaterialize:
    def test_save_persists_and_materializes(self, tmp_path, monkeypatch) -> None:
        persist = tmp_path / "persist"
        runtime = tmp_path / "runtime"
        monkeypatch.setattr("companion.wg._PERSIST_DIR", persist)
        monkeypatch.setattr("companion.wg._WG_CONFIG_DIR", runtime)

        save_config("wg0", _CONF)
        # canonical persistent copy + materialized runtime copy, both 0600
        assert (persist / "wg0.conf").read_text() == _CONF
        assert (runtime / "wg0.conf").read_text() == _CONF
        assert (persist / "wg0.conf").stat().st_mode & 0o077 == 0
        assert (runtime / "wg0.conf").stat().st_mode & 0o077 == 0

    def test_save_rejects_invalid(self, tmp_path, monkeypatch) -> None:
        from aiohttp.web import HTTPBadRequest

        monkeypatch.setattr("companion.wg._PERSIST_DIR", tmp_path / "persist")
        monkeypatch.setattr("companion.wg._WG_CONFIG_DIR", tmp_path / "runtime")
        with pytest.raises(HTTPBadRequest):
            save_config("wg0", "not a wg config")

    def test_materialize_regenerates_after_runtime_wiped(self, tmp_path, monkeypatch) -> None:
        """The whole point: a wiped /etc/wireguard is rebuilt from the persistent copy."""
        persist = tmp_path / "persist"
        runtime = tmp_path / "runtime"
        monkeypatch.setattr("companion.wg._PERSIST_DIR", persist)
        monkeypatch.setattr("companion.wg._WG_CONFIG_DIR", runtime)

        save_config("wg0", _CONF)
        # simulate a restart wiping the ephemeral runtime dir
        (runtime / "wg0.conf").unlink()
        assert materialize("wg0") is True
        assert (runtime / "wg0.conf").read_text() == _CONF

    def test_materialize_missing_returns_false(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setattr("companion.wg._PERSIST_DIR", tmp_path / "persist")
        monkeypatch.setattr("companion.wg._WG_CONFIG_DIR", tmp_path / "runtime")
        assert materialize("wg0") is False


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def _wg_config_dir(tmp_path, monkeypatch):
    """Redirect both the persistent (source-of-truth) and runtime WG dirs to
    temp paths so writes don't escape the sandbox. Returns the persistent dir."""
    persist = tmp_path / "persist"
    runtime = tmp_path / "runtime"
    monkeypatch.setattr("companion.wg._PERSIST_DIR", persist)
    monkeypatch.setattr("companion.wg._WG_CONFIG_DIR", runtime)
    return persist


# ---------------------------------------------------------------------------
# _run_wg_cmd — timeout must reap the child process (no leak)
# ---------------------------------------------------------------------------


async def test_run_wg_cmd_kills_process_on_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    """On timeout, the subprocess must be killed and reaped, then 504 raised."""
    import asyncio

    from aiohttp import web

    from companion import wg

    killed = {"kill": False, "wait": False}

    class _FakeProc:
        returncode = None

        async def communicate(self) -> tuple[bytes, bytes]:  # pragma: no cover - never completes
            await asyncio.sleep(60)
            return b"", b""

        def kill(self) -> None:
            killed["kill"] = True

        async def wait(self) -> int:
            killed["wait"] = True
            return 0

    async def _fake_exec(*_args: object, **_kwargs: object) -> _FakeProc:
        return _FakeProc()

    async def _fake_wait_for(coro: object, timeout: float) -> object:
        # Discard the pending communicate() coroutine and simulate a timeout.
        coro.close()  # type: ignore[attr-defined]
        raise TimeoutError

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_exec)
    monkeypatch.setattr(asyncio, "wait_for", _fake_wait_for)

    with pytest.raises(web.HTTPGatewayTimeout):
        await wg._run_wg_cmd("wg", "show", "wg0", timeout=1)

    assert killed["kill"] is True, "timed-out subprocess was not killed"
    assert killed["wait"] is True, "killed subprocess was not reaped"
