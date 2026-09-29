"""`!include_dir_*` file selection, asked of Home Assistant rather than assumed.

The resolver used to list an include directory one level deep. What HA itself
reads from such a directory — subdirectories, hidden entries, `.yml` files, a
file that does not hold the expected shape — was never measured. This module
measures it: a probe tree is wired into configuration.yaml, HA reloads it, and
the set of entities HA actually loaded is the oracle. The companion must resolve
the same tree to exactly that set. A fixture we wrote would only prove we still
agree with ourselves.
"""

from __future__ import annotations

import subprocess
import time
from io import StringIO
from typing import Any

import pytest
import requests
from ruamel.yaml import YAML

from tests.integration.test_live import _entity_is_live, _read_config_file, _write_config_file

COMPOSE_FILE = "docker-compose.integration.yaml"

AUTO_DIR = "hactl_incdir_auto"
SCRIPT_DIR = "hactl_incdir_script"
VIEW_FILE = "hactl_incdir_view.yaml"


def _automation(slug: str) -> str:
    return f"- id: {slug}\n  alias: {slug}\n  trigger: []\n  action: []\n"


# Every probe is one automation whose only job is to be loaded or not.
# path (relative to AUTO_DIR) -> (file content, automation slug)
AUTO_PROBES: dict[str, tuple[str, str]] = {
    "a_top.yaml": (_automation("hactl_incdir_top"), "hactl_incdir_top"),
    "sub/b_nested.yaml": (_automation("hactl_incdir_nested"), "hactl_incdir_nested"),
    "sub/deeper/c_deep.yaml": (_automation("hactl_incdir_deep"), "hactl_incdir_deep"),
    ".hidden_dir/d_in_hidden.yaml": (_automation("hactl_incdir_hidden_dir"), "hactl_incdir_hidden_dir"),
    ".e_hidden_file.yaml": (_automation("hactl_incdir_hidden_file"), "hactl_incdir_hidden_file"),
    "f_ext.yml": (_automation("hactl_incdir_yml"), "hactl_incdir_yml"),
    # A single automation as a mapping, not a list: merge_list's shape rule.
    "g_mapping.yaml": (
        "id: hactl_incdir_mapping\nalias: hactl_incdir_mapping\ntrigger: []\naction: []\n",
        "hactl_incdir_mapping",
    ),
}

# merge_named: the same script key in two files, one of them nested. HA's merge
# is either shallow (the later file's value replaces the whole entry) or deep
# (the entries are combined) — the friendly name tells which.
SCRIPT_KEY = "hactl_incdir_script"
SCRIPT_PROBES: dict[str, str] = {
    "a_first.yaml": f"{SCRIPT_KEY}:\n  alias: Merged Alias From First\n  sequence: []\n",
    "sub/b_second.yaml": f"{SCRIPT_KEY}:\n  sequence: []\n  mode: queued\n",
}

WIRING = (
    f"\nautomation hactlincdir: !include_dir_merge_list {AUTO_DIR}\n"
    f"script hactlincdir: !include_dir_merge_named {SCRIPT_DIR}\n"
)


def _seed(files: dict[str, str]) -> None:
    """Write files into the shared /config volume, dotfiles and subdirectories included.

    Through the container rather than the companion API: the probe tree needs
    hidden entries, which the companion's path guard rightly refuses to write.
    """
    script = "from pathlib import Path\n" + "".join(
        f"p = Path('/config') / {path!r}\np.parent.mkdir(parents=True, exist_ok=True)\np.write_text({body!r})\n"
        for path, body in files.items()
    )
    subprocess.run(
        ["docker", "compose", "-f", COMPOSE_FILE, "exec", "-T", "companion", "python3", "-"],
        input=script,
        text=True,
        check=True,
        timeout=60,
    )


def _remove(*dirs: str) -> None:
    script = "import shutil\n" + "".join(f"shutil.rmtree('/config/{d}', ignore_errors=True)\n" for d in dirs)
    subprocess.run(
        ["docker", "compose", "-f", COMPOSE_FILE, "exec", "-T", "companion", "python3", "-"],
        input=script,
        text=True,
        check=True,
        timeout=60,
    )


def _reload(ha_url: str, ha_token: str, domain: str) -> None:
    r = requests.post(
        f"{ha_url}/api/services/{domain}/reload",
        headers={"Authorization": f"Bearer {ha_token}"},
        json={},
        timeout=90,
    )
    assert r.status_code == 200, f"{domain}.reload failed: {r.status_code} {r.text}"
    time.sleep(2)


def _companion_view(companion_url: str, auth_headers: dict[str, str]) -> dict[str, Any]:
    """The companion's resolution of the same include lines HA just read."""
    r = requests.get(
        f"{companion_url}/v1/config/file",
        params={"path": VIEW_FILE, "resolve": "true"},
        headers=auth_headers,
        timeout=15,
    )
    assert r.status_code == 200, r.text
    data = YAML(typ="safe").load(StringIO(r.json()["content"]))
    assert isinstance(data, dict)
    return data


@pytest.fixture()
def probe_tree(companion_url: str, auth_headers: dict[str, str], ha_url: str, ha_token: str, _ha_ready: None) -> Any:
    original = _read_config_file(companion_url, auth_headers, "configuration.yaml")
    assert original is not None
    _seed({f"{AUTO_DIR}/{p}": body for p, (body, _) in AUTO_PROBES.items()})
    _seed({f"{SCRIPT_DIR}/{p}": body for p, body in SCRIPT_PROBES.items()})
    _seed({VIEW_FILE: WIRING.lstrip("\n")})
    try:
        r = _write_config_file(companion_url, auth_headers, "configuration.yaml", original.rstrip("\n") + WIRING)
        assert r.status_code == 200, f"HA rejected the probe wiring: {r.text}"
        _reload(ha_url, ha_token, "automation")
        _reload(ha_url, ha_token, "script")
        yield
    finally:
        _write_config_file(companion_url, auth_headers, "configuration.yaml", original)
        _remove(AUTO_DIR, SCRIPT_DIR)
        _seed({VIEW_FILE: "{}\n"})
        _reload(ha_url, ha_token, "automation")
        _reload(ha_url, ha_token, "script")


@pytest.mark.usefixtures("probe_tree")
class TestIncludeDirMatchesHomeAssistant:
    def test_merge_list_selects_the_files_home_assistant_reads(
        self, companion_url: str, auth_headers: dict[str, str], ha_url: str, ha_token: str
    ) -> None:
        """The companion resolves exactly the automations HA loaded — no more, no fewer."""
        ha_loaded = {
            slug for _, slug in AUTO_PROBES.values() if _entity_is_live(ha_url, ha_token, f"automation.{slug}")
        }
        assert "hactl_incdir_top" in ha_loaded, (
            f"HA did not even load the top-level probe; the wiring is broken, not the resolver: {ha_loaded}"
        )

        view = _companion_view(companion_url, auth_headers)
        companion_ids = {a["id"] for a in view["automation hactlincdir"] if isinstance(a, dict) and "id" in a}

        assert companion_ids == ha_loaded, (
            f"HA loaded {sorted(ha_loaded)} from !include_dir_merge_list; "
            f"the companion resolved {sorted(companion_ids)}"
        )

    def test_merge_named_merges_the_way_home_assistant_does(
        self, companion_url: str, auth_headers: dict[str, str], ha_url: str, ha_token: str
    ) -> None:
        """Nested file included, and the same key in two files combined as HA combines it."""
        r = requests.get(
            f"{ha_url}/api/states/script.{SCRIPT_KEY}",
            headers={"Authorization": f"Bearer {ha_token}"},
            timeout=15,
        )
        assert r.status_code == 200, f"HA did not load script.{SCRIPT_KEY} at all: {r.text}"
        ha_attrs = r.json()["attributes"]

        entry = _companion_view(companion_url, auth_headers)["script hactlincdir"][SCRIPT_KEY]

        assert entry.get("mode") == ha_attrs.get("mode"), (
            f"HA runs the script in mode {ha_attrs.get('mode')!r}; the companion resolved {entry!r}"
        )
        ha_kept_alias = ha_attrs.get("friendly_name") == "Merged Alias From First"
        assert ("alias" in entry) == ha_kept_alias, (
            f"HA's friendly_name is {ha_attrs.get('friendly_name')!r} (alias from the first file "
            f"{'kept' if ha_kept_alias else 'dropped'}); the companion resolved {entry!r}"
        )
