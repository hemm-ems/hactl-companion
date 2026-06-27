"""Tests for the add-on entrypoint (__main__.main) startup ordering/wiring."""

from __future__ import annotations

import logging
import os
from contextlib import contextmanager
from unittest.mock import MagicMock, patch

from companion import __main__, wg_supervisor

_RESULT = wg_supervisor.ReconcileResult(tunnel="wg0", conf_text="[Interface]\n")


@contextmanager
def _patched(reconcile):
    with (
        patch.dict(os.environ, {"SUPERVISOR_TOKEN": "tok"}),
        patch("companion.__main__.wg_supervisor.load_options", return_value=MagicMock()),
        patch("companion.__main__.wg_supervisor.reconcile", side_effect=reconcile),
        patch("companion.__main__.wg_supervisor.register_startup_tasks") as reg,
        patch("companion.__main__.web.run_app") as run_app,
    ):
        yield reg, run_app


def test_banner_logged_before_reconcile(caplog) -> None:
    banner_seen_at_reconcile: list[bool] = []

    async def fake_reconcile(opts, **kwargs):
        banner_seen_at_reconcile.append(any("starting (port" in r.getMessage() for r in caplog.records))
        return _RESULT

    with caplog.at_level(logging.INFO, logger="companion"), _patched(fake_reconcile):
        __main__.main(["--port", "9100"])

    assert banner_seen_at_reconcile == [True], "banner must be logged before reconcile runs"


def test_dropped_lines_absent_and_registers_startup(caplog) -> None:
    async def fake_reconcile(opts, **kwargs):
        return _RESULT

    with caplog.at_level(logging.INFO, logger="companion"), _patched(fake_reconcile) as (reg, _run_app):
        __main__.main(["--port", "9100"])

    msgs = [r.getMessage() for r in caplog.records if r.name == "companion"]
    joined = "\n".join(msgs)
    assert any("starting (port 9100)" in m for m in msgs)
    assert any("supervisor token:" in m for m in msgs)
    # Dropped lines must not appear.
    assert "config path:" not in joined
    assert "ingress requests bypass" not in joined
    # Startup tasks registered with the reconcile result's tunnel.
    reg.assert_called_once()
    assert reg.call_args.args[1] == "wg0"
