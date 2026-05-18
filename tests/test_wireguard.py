"""Unit tests for WireGuard route module."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from companion.routes.wireguard import (
    _conf_from_json,
    _parse_wg_show,
    _validate_conf,
    _validate_tunnel,
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
# _parse_wg_show
# ---------------------------------------------------------------------------


class TestParseWgShow:
    def test_full_output(self) -> None:
        output = (
            "interface: wg0\n"
            "  public key: AAAA\n"
            "  listening port: 51820\n"
            "\n"
            "peer: BBBB\n"
            "  endpoint: 1.2.3.4:51820\n"
            "  allowed ips: 10.0.0.0/24\n"
            "  latest handshake: 42 seconds ago\n"
            "  transfer: 1.23 KiB received, 4.56 KiB sent\n"
        )
        result = _parse_wg_show(output)
        assert result["interface"]["public_key"] == "AAAA"
        assert result["interface"]["listening_port"] == 51820
        assert len(result["peers"]) == 1
        peer = result["peers"][0]
        assert peer["public_key"] == "BBBB"
        assert peer["endpoint"] == "1.2.3.4:51820"
        assert peer["allowed_ips"] == "10.0.0.0/24"
        assert "42 seconds ago" in peer["latest_handshake"]

    def test_no_peers(self) -> None:
        output = "interface: wg0\n  public key: AAAA\n  listening port: 51820\n"
        result = _parse_wg_show(output)
        assert result["interface"]["public_key"] == "AAAA"
        assert result["peers"] == []

    def test_empty_output(self) -> None:
        result = _parse_wg_show("")
        assert result["interface"] == {}
        assert result["peers"] == []


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


class TestPostStart:
    async def test_start_success(self, client, auth_headers) -> None:
        with (
            patch("companion.routes.wireguard._is_interface_up", return_value=False),
            patch("companion.routes.wireguard._run_wg_cmd", return_value=(0, "", "")),
        ):
            resp = await client.post("/v1/wireguard/start?tunnel=wg0", headers=auth_headers)
        assert resp.status == 200
        body = await resp.json()
        assert body["status"] == "started"
        assert body["auto_enable"] is False

    async def test_start_with_auto_enable(self, client, auth_headers) -> None:
        with (
            patch("companion.routes.wireguard._is_interface_up", return_value=False),
            patch("companion.routes.wireguard._run_wg_cmd", return_value=(0, "", "")),
            patch("companion.routes.wireguard._enable_auto_start") as mock_enable,
        ):
            resp = await client.post("/v1/wireguard/start?tunnel=wg0&auto_enable=true", headers=auth_headers)
        assert resp.status == 200
        body = await resp.json()
        assert body["auto_enable"] is True
        mock_enable.assert_called_once_with("wg0")

    async def test_start_already_up(self, client, auth_headers) -> None:
        with patch("companion.routes.wireguard._is_interface_up", return_value=True):
            resp = await client.post("/v1/wireguard/start?tunnel=wg0", headers=auth_headers)
        assert resp.status == 409

    async def test_start_failure(self, client, auth_headers) -> None:
        with (
            patch("companion.routes.wireguard._is_interface_up", return_value=False),
            patch("companion.routes.wireguard._run_wg_cmd", return_value=(1, "", "error")),
        ):
            resp = await client.post("/v1/wireguard/start?tunnel=wg0", headers=auth_headers)
        assert resp.status == 500


class TestPostStop:
    async def test_stop_success(self, client, auth_headers) -> None:
        with (
            patch("companion.routes.wireguard._is_interface_up", return_value=True),
            patch("companion.routes.wireguard._run_wg_cmd", return_value=(0, "", "")),
        ):
            resp = await client.post("/v1/wireguard/stop?tunnel=wg0", headers=auth_headers)
        assert resp.status == 200
        body = await resp.json()
        assert body["status"] == "stopped"

    async def test_stop_not_running(self, client, auth_headers) -> None:
        with patch("companion.routes.wireguard._is_interface_up", return_value=False):
            resp = await client.post("/v1/wireguard/stop?tunnel=wg0", headers=auth_headers)
        assert resp.status == 409


class TestGetStatus:
    async def test_status_active(self, client, auth_headers) -> None:
        wg_output = (
            "interface: wg0\n  public key: AAAA\n  listening port: 51820\n"
            "peer: BBBB\n  endpoint: 1.2.3.4:51820\n  allowed ips: 10.0.0.0/24\n"
        )

        async def _mock_run(*args: str, timeout: int = 30) -> tuple[int, str, str]:
            if args[1] == "show":
                return (0, wg_output, "")
            return (1, "", "not enabled")  # systemctl is-enabled

        with (
            patch("companion.routes.wireguard._is_interface_up", return_value=True),
            patch("companion.routes.wireguard._run_wg_cmd", side_effect=_mock_run),
            patch("companion.routes.wireguard._is_auto_enabled", return_value=False),
        ):
            resp = await client.get("/v1/wireguard/status?tunnel=wg0", headers=auth_headers)
        assert resp.status == 200
        body = await resp.json()
        assert body["state"] == "active"
        assert body["tunnel"] == "wg0"
        assert body["interface"]["public_key"] == "AAAA"
        assert len(body["peers"]) == 1
        assert body["auto_enable"] is False

    async def test_status_inactive(self, client, auth_headers) -> None:
        with patch("companion.routes.wireguard._is_interface_up", return_value=False):
            resp = await client.get("/v1/wireguard/status?tunnel=wg0", headers=auth_headers)
        assert resp.status == 200
        body = await resp.json()
        assert body["state"] == "inactive"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def _wg_config_dir(tmp_path, monkeypatch) -> None:
    """Redirect WG config dir to temp path for safe writes."""
    monkeypatch.setattr("companion.routes.wireguard._WG_CONFIG_DIR", tmp_path)
    return tmp_path
