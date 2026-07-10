"""Shared query-parameter parsing helpers.

One place to interpret boolean query params so every route agrees on what
``?flag=...`` means (previously ``!= "false"`` in one route and
``in ("1","true","yes")`` in another).
"""

from __future__ import annotations

from aiohttp import web

_TRUE = frozenset({"1", "true", "yes", "on"})
_FALSE = frozenset({"0", "false", "no", "off"})


def parse_bool_param(request: web.Request, name: str, *, default: bool) -> bool:
    """Interpret a boolean query param.

    Absent -> ``default``. Recognized truthy/falsy strings map to their value.
    An unrecognized value falls back to ``default`` so a garbage flag can never
    flip a safe default (e.g. ``?dry_run=garbage`` keeps dry-run on).
    """
    raw = request.query.get(name)
    if raw is None:
        return default
    value = raw.strip().lower()
    if value in _TRUE:
        return True
    if value in _FALSE:
        return False
    return default
