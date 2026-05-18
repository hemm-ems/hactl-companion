"""Tests for CLI argument parsing."""

from __future__ import annotations

import pytest

from companion.__main__ import _parse_args


def test_version_flag(capsys: pytest.CaptureFixture[str]) -> None:
    """--version should print version and exit."""
    with pytest.raises(SystemExit, match="0"):
        _parse_args(["--version"])
    captured = capsys.readouterr()
    assert "hactl-companion" in captured.out
    assert "0.3.0" in captured.out


def test_help_flag(capsys: pytest.CaptureFixture[str]) -> None:
    """--help should print help and exit."""
    with pytest.raises(SystemExit, match="0"):
        _parse_args(["--help"])
    captured = capsys.readouterr()
    assert "hactl-companion" in captured.out


def test_default_host_and_port() -> None:
    """Default host should be dual-stack and port should be 9100."""
    args = _parse_args([])
    assert args.host == "0.0.0.0,[::]"
    assert args.port == 9100


def test_custom_host_and_port() -> None:
    """Custom --host and --port should be accepted."""
    args = _parse_args(["--host", "0.0.0.0", "--port", "8080"])
    assert args.host == "0.0.0.0"
    assert args.port == 8080


def test_log_level_flag() -> None:
    """--log-level should set the log level."""
    args = _parse_args(["--log-level", "debug"])
    assert args.log_level == "debug"


def test_default_log_level() -> None:
    """Default log level should be info."""
    args = _parse_args([])
    assert args.log_level == "info"
