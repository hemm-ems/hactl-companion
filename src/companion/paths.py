"""Filesystem locations that depend on how Supervisor mounts volumes.

The add-on maps ``homeassistant_config``, which Supervisor (since 2023.09)
mounts at ``/homeassistant`` inside the container — ``/config`` is reserved
for the add-on's *own* config folder. Local dev and the docker-compose
integration stack still mount the HA config at ``/config``, so detection
falls back to that when ``/homeassistant`` is absent.
"""

from __future__ import annotations

import os
from pathlib import Path

_HA_MOUNT = Path("/homeassistant")
_LEGACY_MOUNT = Path("/config")


def config_base(ha_mount: Path = _HA_MOUNT, legacy_mount: Path = _LEGACY_MOUNT) -> Path:
    """The Home Assistant config directory as seen from inside this container.

    Override with the ``COMPANION_CONFIG_BASE`` env var; otherwise prefer the
    Supervisor mount, falling back to the legacy/dev location.
    """
    env = os.environ.get("COMPANION_CONFIG_BASE")
    if env:
        return Path(env)
    if ha_mount.is_dir():
        return ha_mount
    return legacy_mount


def hactl_dir(base: Path | None = None) -> Path:
    """Persistent hactl state dir inside the HA config volume."""
    return (base if base is not None else config_base()) / "hactl"
