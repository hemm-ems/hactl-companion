"""Tests for the in-memory log ring buffer and the /v1/logs endpoint."""

from __future__ import annotations

import logging
import time

from companion import logbuffer


class TestRingBuffer:
    def _emit(
        self,
        handler: logbuffer.RingBufferHandler,
        name: str,
        level: int,
        msg: str,
        ts: float | None = None,
    ) -> None:
        record = logging.LogRecord(name, level, __file__, 0, msg, None, None)
        if ts is not None:
            record.created = ts
        handler.emit(record)

    def test_capacity_is_bounded(self) -> None:
        h = logbuffer.RingBufferHandler(capacity=3)
        for i in range(5):
            self._emit(h, "companion.wg", logging.INFO, f"msg {i}")
        msgs = [e.message for e in h.snapshot()]
        assert msgs == ["msg 2", "msg 3", "msg 4"]

    def test_message_is_formatted_eagerly(self) -> None:
        h = logbuffer.RingBufferHandler()
        record = logging.LogRecord("companion.wg", logging.WARNING, __file__, 0, "hello %s", ("world",), None)
        h.emit(record)
        assert h.snapshot()[0].message == "hello world"

    def test_component_alias_filter(self) -> None:
        h = logbuffer.RingBufferHandler()
        self._emit(h, "companion.wg_monitor", logging.INFO, "wg one")
        self._emit(h, "companion.routes.wireguard", logging.INFO, "wg two")
        self._emit(h, "companion.access", logging.INFO, "req")
        self._emit(h, "companion.routes.ha", logging.INFO, "ha")
        msgs = [e.message for e in h.snapshot(component="wireguard")]
        assert msgs == ["wg one", "wg two"]

    def test_component_substring_filter(self) -> None:
        h = logbuffer.RingBufferHandler()
        self._emit(h, "companion.routes.ha", logging.INFO, "ha one")
        self._emit(h, "companion.wg", logging.INFO, "wg one")
        assert [e.message for e in h.snapshot(component="routes.ha")] == ["ha one"]

    def test_level_filter(self) -> None:
        h = logbuffer.RingBufferHandler()
        self._emit(h, "companion.wg", logging.INFO, "info")
        self._emit(h, "companion.wg", logging.WARNING, "warn")
        self._emit(h, "companion.wg", logging.ERROR, "err")
        assert [e.message for e in h.snapshot(level="warning")] == ["warn", "err"]

    def test_since_filter(self) -> None:
        h = logbuffer.RingBufferHandler()
        now = time.time()
        self._emit(h, "companion.wg", logging.INFO, "old", ts=now - 600)
        self._emit(h, "companion.wg", logging.INFO, "new", ts=now - 10)
        assert [e.message for e in h.snapshot(since=now - 60)] == ["new"]

    def test_limit_keeps_most_recent(self) -> None:
        h = logbuffer.RingBufferHandler()
        for i in range(5):
            self._emit(h, "companion.wg", logging.INFO, f"m{i}")
        assert [e.message for e in h.snapshot(limit=2)] == ["m3", "m4"]


class TestLogsEndpoint:
    async def test_returns_buffered_entries(self, client, auth_headers) -> None:
        handler = logbuffer.install()
        logging.getLogger("companion.wg_monitor").warning("stale handshake on wg0")
        try:
            resp = await client.get("/v1/logs?component=wireguard", headers=auth_headers)
            assert resp.status == 200
            body = await resp.json()
            messages = [e["message"] for e in body["entries"]]
            assert "stale handshake on wg0" in messages
        finally:
            logging.getLogger().removeHandler(handler)
            logbuffer._handler = None

    async def test_requires_auth(self, client) -> None:
        resp = await client.get("/v1/logs")
        assert resp.status == 401

    async def test_invalid_since_is_400(self, client, auth_headers) -> None:
        resp = await client.get("/v1/logs?since=nonsense", headers=auth_headers)
        assert resp.status == 400


class TestAccessLog:
    async def test_single_access_line_per_request(self, client, auth_headers, caplog) -> None:
        # The structured access_log_middleware must emit exactly one line per
        # request; aiohttp's built-in access logger (disabled via run_app
        # access_log=None in __main__) would otherwise duplicate it.
        with caplog.at_level(logging.DEBUG, logger="companion.access"):
            await client.get("/v1/health", headers=auth_headers)
        access_records = [r for r in caplog.records if r.name == "companion.access"]
        assert len(access_records) == 1
        assert "GET /v1/health" in access_records[0].message

    async def test_health_access_logged_at_debug(self, client, auth_headers, caplog) -> None:
        # Auth-exempt health/status pings are high-frequency noise → DEBUG, so
        # they don't drown real API calls in the add-on log.
        with caplog.at_level(logging.DEBUG, logger="companion.access"):
            await client.get("/v1/health", headers=auth_headers)
        rec = next(r for r in caplog.records if r.name == "companion.access")
        assert rec.levelno == logging.DEBUG
