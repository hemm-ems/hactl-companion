"""In-memory ring buffer of recent log records, queryable over HTTP.

Add-on logs only ever reach the Supervisor journal (the add-on "Log" tab);
they never flow into Home Assistant's core logger, so hactl cannot retrieve
them through the normal log path. This module captures the companion's own log
records into a bounded deque and exposes a snapshot query, which the
``/v1/logs`` route serves over the same Ingress lifeline as everything else.
"""

from __future__ import annotations

import logging
from collections import deque
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterable

_DEFAULT_CAPACITY = 500

# Friendly component names → logger-name prefixes. "companion.wg" already covers
# companion.wg, companion.wg_monitor, and companion.wg_supervisor.
_COMPONENT_ALIASES: dict[str, tuple[str, ...]] = {
    "wireguard": ("companion.wg", "companion.routes.wireguard"),
    "wg": ("companion.wg", "companion.routes.wireguard"),
    "access": ("companion.access",),
}


@dataclass(frozen=True)
class LogEntry:
    ts: float  # epoch seconds
    level: str
    name: str
    message: str

    def as_dict(self) -> dict[str, object]:
        return {"ts": self.ts, "level": self.level, "name": self.name, "message": self.message}


class RingBufferHandler(logging.Handler):
    """Logging handler that keeps the most recent records in a bounded deque."""

    def __init__(self, capacity: int = _DEFAULT_CAPACITY) -> None:
        super().__init__()
        self._buf: deque[LogEntry] = deque(maxlen=capacity)

    def emit(self, record: logging.LogRecord) -> None:
        # Format eagerly: never retain the record's args (they may be mutated or
        # reference live objects).
        try:
            message = record.getMessage()
        except Exception:
            # A broken format string must never kill logging.
            message = record.msg if isinstance(record.msg, str) else repr(record.msg)
        self._buf.append(
            LogEntry(
                ts=record.created,
                level=record.levelname,
                name=record.name,
                message=message,
            )
        )

    def snapshot(
        self,
        *,
        component: str | None = None,
        level: str | None = None,
        since: float | None = None,
        limit: int | None = None,
    ) -> list[LogEntry]:
        """Return buffered entries (oldest first) matching the given filters.

        ``component`` is a friendly alias (e.g. "wireguard") or a substring of
        the logger name. ``level`` is a minimum severity. ``since`` is an epoch
        cutoff. ``limit`` keeps only the most recent N matches.
        """
        entries: Iterable[LogEntry] = tuple(self._buf)

        if component:
            prefixes = _COMPONENT_ALIASES.get(component.lower())
            if prefixes:
                entries = [e for e in entries if e.name.startswith(prefixes)]
            else:
                needle = component.lower()
                entries = [e for e in entries if needle in e.name.lower()]

        if level:
            threshold = logging.getLevelName(level.upper())
            if isinstance(threshold, int):
                entries = [e for e in entries if _level_value(e.level) >= threshold]

        if since is not None:
            entries = [e for e in entries if e.ts >= since]

        entries = list(entries)
        if limit is not None and limit >= 0:
            entries = entries[-limit:]
        return entries


def _level_value(name: str) -> int:
    value = logging.getLevelName(name.upper())
    return value if isinstance(value, int) else 0


# Module-level singleton, installed once at startup.
_handler: RingBufferHandler | None = None


def install(level: int = logging.INFO, capacity: int = _DEFAULT_CAPACITY) -> RingBufferHandler:
    """Attach the ring-buffer handler to the root logger (idempotent)."""
    global _handler
    if _handler is None:
        _handler = RingBufferHandler(capacity)
        _handler.setLevel(level)
        logging.getLogger().addHandler(_handler)
    return _handler


def get_handler() -> RingBufferHandler | None:
    """Return the installed handler, or None if install() was never called."""
    return _handler
