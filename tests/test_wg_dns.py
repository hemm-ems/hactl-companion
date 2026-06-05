"""Tests for WireGuard DNS helpers and peer-endpoint parser."""

from __future__ import annotations

import socket
from unittest.mock import AsyncMock, MagicMock, patch

from companion.wg import (
    _dns_lookup_ip,
    _flush_peer,
    _parse_hostname_peers,
    _PeerEndpoint,
    _resolve_endpoint_hostnames,
)

# ---------------------------------------------------------------------------
# _dns_lookup_ip
# ---------------------------------------------------------------------------

_GETADDRINFO_RESULT = [(socket.AF_INET, socket.SOCK_DGRAM, 0, "", ("1.2.3.4", 0))]


class TestDnsLookupIp:
    async def test_resolves_hostname(self) -> None:
        mock_loop = MagicMock()
        mock_loop.getaddrinfo = AsyncMock(return_value=_GETADDRINFO_RESULT)
        with patch("companion.wg.asyncio.get_running_loop", return_value=mock_loop):
            result = await _dns_lookup_ip("vpn.example.com")
        assert result == "1.2.3.4"

    async def test_returns_none_on_gaierror(self) -> None:
        mock_loop = MagicMock()
        mock_loop.getaddrinfo = AsyncMock(side_effect=socket.gaierror("no such host"))
        with patch("companion.wg.asyncio.get_running_loop", return_value=mock_loop):
            result = await _dns_lookup_ip("nxdomain.invalid")
        assert result is None

    async def test_returns_none_on_timeout(self) -> None:
        import asyncio

        mock_loop = MagicMock()
        # getaddrinfo hangs → wait_for raises TimeoutError
        mock_loop.getaddrinfo = AsyncMock(side_effect=asyncio.TimeoutError)
        with patch("companion.wg.asyncio.get_running_loop", return_value=mock_loop):
            result = await _dns_lookup_ip("slow.example.com", timeout=0.001)
        assert result is None

    async def test_returns_none_on_os_error(self) -> None:
        mock_loop = MagicMock()
        mock_loop.getaddrinfo = AsyncMock(side_effect=OSError("network down"))
        with patch("companion.wg.asyncio.get_running_loop", return_value=mock_loop):
            result = await _dns_lookup_ip("vpn.example.com")
        assert result is None


# ---------------------------------------------------------------------------
# _resolve_endpoint_hostnames
# ---------------------------------------------------------------------------

_CONF_WITH_HOSTNAME = (
    "[Interface]\nPrivateKey = K\nAddress = 10.0.0.1/24\n"
    "[Peer]\nPublicKey = P\nEndpoint = vpn.example.com:51820\nAllowedIPs = 0/0\n"
)
_CONF_WITH_IP = (
    "[Interface]\nPrivateKey = K\nAddress = 10.0.0.1/24\n"
    "[Peer]\nPublicKey = P\nEndpoint = 1.2.3.4:51820\nAllowedIPs = 0/0\n"
)
_CONF_NO_ENDPOINT = "[Interface]\nPrivateKey = K\nAddress = 10.0.0.1/24\n[Peer]\nPublicKey = P\nAllowedIPs = 0/0\n"
_CONF_IPV6 = (
    "[Interface]\nPrivateKey = K\nAddress = 10.0.0.1/24\n"
    "[Peer]\nPublicKey = P\nEndpoint = [::1]:51820\nAllowedIPs = 0/0\n"
)


class TestResolveEndpointHostnames:
    async def test_hostname_fails_returns_it(self) -> None:
        with patch("companion.wg._dns_lookup_ip", AsyncMock(return_value=None)):
            result = await _resolve_endpoint_hostnames(_CONF_WITH_HOSTNAME)
        assert result == ["vpn.example.com"]

    async def test_hostname_resolves_returns_empty(self) -> None:
        with patch("companion.wg._dns_lookup_ip", AsyncMock(return_value="1.2.3.4")):
            result = await _resolve_endpoint_hostnames(_CONF_WITH_HOSTNAME)
        assert result == []

    async def test_ip_endpoint_is_skipped(self) -> None:
        lookup = AsyncMock(return_value=None)
        with patch("companion.wg._dns_lookup_ip", lookup):
            result = await _resolve_endpoint_hostnames(_CONF_WITH_IP)
        assert result == []
        lookup.assert_not_called()

    async def test_no_endpoint_returns_empty(self) -> None:
        lookup = AsyncMock(return_value=None)
        with patch("companion.wg._dns_lookup_ip", lookup):
            result = await _resolve_endpoint_hostnames(_CONF_NO_ENDPOINT)
        assert result == []
        lookup.assert_not_called()

    async def test_ipv6_literal_is_skipped(self) -> None:
        lookup = AsyncMock(return_value=None)
        with patch("companion.wg._dns_lookup_ip", lookup):
            result = await _resolve_endpoint_hostnames(_CONF_IPV6)
        assert result == []
        lookup.assert_not_called()

    async def test_multiple_peers_partial_failure(self) -> None:
        conf = (
            "[Interface]\nPrivateKey = K\nAddress = 10.0.0.1/24\n"
            "[Peer]\nPublicKey = P1\nEndpoint = ok.example.com:51820\nAllowedIPs = 10.0.0.0/24\n"
            "[Peer]\nPublicKey = P2\nEndpoint = bad.example.com:51820\nAllowedIPs = 10.0.1.0/24\n"
        )

        async def lookup_side(host: str, **_: object) -> str | None:
            return "1.2.3.4" if host == "ok.example.com" else None

        with patch("companion.wg._dns_lookup_ip", side_effect=lookup_side):
            result = await _resolve_endpoint_hostnames(conf)
        assert result == ["bad.example.com"]


# ---------------------------------------------------------------------------
# _parse_hostname_peers / _flush_peer
# ---------------------------------------------------------------------------


class TestParseHostnamePeers:
    def test_single_hostname_peer(self) -> None:
        conf = (
            "[Interface]\nPrivateKey = K\nAddress = 10.0.0.1/24\n"
            "[Peer]\nPublicKey = PUB1\nEndpoint = home.kippings.de:51826\nAllowedIPs = 0/0\n"
        )
        peers = _parse_hostname_peers(conf)
        assert peers == [_PeerEndpoint(pubkey="PUB1", hostname="home.kippings.de", port=51826)]

    def test_ip_endpoint_is_skipped(self) -> None:
        peers = _parse_hostname_peers(_CONF_WITH_IP)
        assert peers == []

    def test_peer_without_endpoint_is_skipped(self) -> None:
        peers = _parse_hostname_peers(_CONF_NO_ENDPOINT)
        assert peers == []

    def test_ipv6_literal_is_skipped(self) -> None:
        peers = _parse_hostname_peers(_CONF_IPV6)
        assert peers == []

    def test_mixed_peers_only_hostname_returned(self) -> None:
        conf = (
            "[Interface]\nPrivateKey = K\nAddress = 10.0.0.1/24\n"
            "[Peer]\nPublicKey = P1\nEndpoint = 1.2.3.4:51820\nAllowedIPs = 10.0.0.0/24\n"
            "[Peer]\nPublicKey = P2\nEndpoint = home.example.com:51820\nAllowedIPs = 10.0.1.0/24\n"
        )
        peers = _parse_hostname_peers(conf)
        assert len(peers) == 1
        assert peers[0].pubkey == "P2"
        assert peers[0].hostname == "home.example.com"

    def test_flush_peer_ignores_missing_pubkey(self) -> None:
        out: list[_PeerEndpoint] = []
        _flush_peer({"Endpoint": "vpn.example.com:51820"}, out)
        assert out == []

    def test_flush_peer_ignores_missing_endpoint(self) -> None:
        out: list[_PeerEndpoint] = []
        _flush_peer({"PublicKey": "PUB"}, out)
        assert out == []
