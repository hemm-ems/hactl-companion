"""``GET /v1/ref/grep`` against a real Home Assistant (hactl #115, AC-8).

The unit tier proves the matcher on hand-written trees. What only a live stack
can prove is that the files searched are the files Home Assistant reads: the
probe automation below is written into HA's own default ``automations.yaml``
(wired by onboarding's ``configuration.yaml``), HA's ``check_config`` accepts
it on the way in (C-6), and HA is then asked whether it actually loaded the
automation. Only after HA has confirmed it is config does the search have to
find it.

Self-contained: the probe owns ``automations.yaml`` for the duration of the
class and puts the original back, and every literal carries a ``grepprobe``
prefix so nothing else in the stack can collide with it.
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from typing import Any

import pytest
import requests

PROBE_ID = "grepprobe_automation"
TEMPLATE = "{{ is_state('sensor.grepprobe_balkony_p_ac', 'on') }}"
CAP_KEYS = 1001  # one past the route's 1000-row cap

_PROBE = (
    f"- id: {PROBE_ID}\n"
    "  alias: Küche Licht grepprobe\n"
    "  description: Schaltet das KÜCHENLICHT\n"
    "  triggers:\n"
    "    - trigger: state\n"
    "      entity_id:\n"
    "        - sensor.grepprobe_wp_vl\n"
    "        - sensor.grepprobe_wp_rl\n"
    "        - sensor.grepprobe_wp_xx\n"
    "  conditions:\n"
    "    - condition: template\n"
    f'      value_template: "{TEMPLATE}"\n'
    "  variables:\n" + "".join(f"    grepcap_{i:05d}: {i}\n" for i in range(CAP_KEYS)) + "  actions:\n"
    '    - delay: "00:00:01"\n'
)

# The everyday reason a walk is partial: `secrets.yaml` exists on every
# onboarded instance and the path guard never opens it. A second automation
# pulls it in with `!include`, which HA itself accepts.
_SECRETS_INCLUDE = (
    "- id: grepprobe_secrets\n"
    "  alias: grepprobe secrets\n"
    "  triggers:\n"
    "    - trigger: state\n"
    "      entity_id: sensor.grepprobe_secret_side\n"
    "  variables: !include secrets.yaml\n"
    "  actions:\n"
    '    - delay: "00:00:01"\n'
)


def _read(companion_url: str, auth_headers: dict[str, str], path: str) -> str | None:
    r = requests.get(
        f"{companion_url}/v1/config/file",
        params={"path": path, "resolve": "false"},
        headers=auth_headers,
        timeout=15,
    )
    if r.status_code == 404:
        return None
    assert r.status_code == 200, r.text
    return str(r.json()["content"])


def _write(companion_url: str, auth_headers: dict[str, str], path: str, content: str) -> None:
    r = requests.put(
        f"{companion_url}/v1/config/file",
        params={"path": path, "dry_run": "false"},
        data=content.encode("utf-8"),
        headers={**auth_headers, "Content-Type": "text/plain; charset=utf-8"},
        timeout=120,
    )
    assert r.status_code == 200, f"HA refused the probe config: {r.text}"
    assert r.json()["validated"] is True, r.text


def _ha_loaded_automation(ha_url: str, ha_token: str, automation_id: str, timeout: float = 60) -> bool:
    """Whether HA has an automation entity whose config id is ``automation_id``."""
    headers = {"Authorization": f"Bearer {ha_token}"}
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        r = requests.get(f"{ha_url}/api/states", headers=headers, timeout=15)
        r.raise_for_status()
        if any(
            s["entity_id"].startswith("automation.") and s.get("attributes", {}).get("id") == automation_id
            for s in r.json()
        ):
            return True
        time.sleep(1)
    return False


def _grep(companion_url: str, auth_headers: dict[str, str], **params: str) -> dict[str, Any]:
    r = requests.get(f"{companion_url}/v1/ref/grep", params=params, headers=auth_headers, timeout=60)
    assert r.status_code == 200, r.text
    body: dict[str, Any] = r.json()
    return body


def _hits_in(body: dict[str, Any], location: str) -> list[dict[str, Any]]:
    return [h for h in body["hits"] if h["location"] == location]


@pytest.fixture(scope="class")
def grep_probe(companion_url: str, ha_token: str, ha_url: str, _ha_ready: None) -> Iterator[dict[str, str]]:
    headers = {"Authorization": f"Bearer {ha_token}"}
    original = _read(companion_url, headers, "automations.yaml")
    assert _read(companion_url, headers, "configuration.yaml") is not None
    try:
        _write(companion_url, headers, "automations.yaml", _PROBE + _SECRETS_INCLUDE)
        r = requests.post(f"{companion_url}/v1/ha/reload/automation", headers=headers, timeout=60)
        assert r.status_code == 200, r.text
        assert _ha_loaded_automation(ha_url, ha_token, PROBE_ID), (
            "HA did not load the probe automation, so automations.yaml is not proven to be config HA reads"
        )
        assert _ha_loaded_automation(ha_url, ha_token, "grepprobe_secrets"), (
            "HA did not load the automation that !includes secrets.yaml"
        )
        yield headers
    finally:
        if original is not None and original.strip():
            _write(companion_url, headers, "automations.yaml", original)
        else:
            _write(companion_url, headers, "automations.yaml", "[]\n")
        requests.post(f"{companion_url}/v1/ha/reload/automation", headers=headers, timeout=60)


@pytest.mark.usefixtures("grep_probe")
class TestRefGrepLive:
    def test_substring_inside_a_template_body(self, companion_url: str, grep_probe: dict[str, str]) -> None:
        body = _grep(companion_url, grep_probe, term="grepprobe_balkony")

        assert body["hits"] == [
            {
                "location": "automations.yaml",
                "path": "[0].conditions[0].value_template",
                "key": "value_template",
                "kind": "value",
                "matched_value": TEMPLATE,
            }
        ]
        assert body["truncated"] is False

    @pytest.mark.parametrize("term", ["küche licht", "KÜCHE LICHT", "Küche Licht"])
    def test_case_insensitive_non_ascii(self, companion_url: str, grep_probe: dict[str, str], term: str) -> None:
        body = _grep(companion_url, grep_probe, term=term)

        assert [(h["path"], h["matched_value"]) for h in _hits_in(body, "automations.yaml")] == [
            ("[0].alias", "Küche Licht grepprobe"),
        ]

    def test_case_sensitive_opt_in(self, companion_url: str, grep_probe: dict[str, str]) -> None:
        loose = _grep(companion_url, grep_probe, term="küche")
        assert [h["path"] for h in _hits_in(loose, "automations.yaml")] == ["[0].alias", "[0].description"]

        exact = _grep(companion_url, grep_probe, term="KÜCHE", case_sensitive="true")
        assert [h["path"] for h in _hits_in(exact, "automations.yaml")] == ["[0].description"]

    def test_regex(self, companion_url: str, grep_probe: dict[str, str]) -> None:
        body = _grep(companion_url, grep_probe, term=r"grepprobe_wp_(vl|rl)$", regex="true")

        assert [(h["path"], h["matched_value"]) for h in body["hits"]] == [
            ("[0].triggers[0].entity_id[0]", "sensor.grepprobe_wp_vl"),
            ("[0].triggers[0].entity_id[1]", "sensor.grepprobe_wp_rl"),
        ]

    def test_invalid_regex_is_400(self, companion_url: str, grep_probe: dict[str, str]) -> None:
        r = requests.get(
            f"{companion_url}/v1/ref/grep", params={"term": "(", "regex": "true"}, headers=grep_probe, timeout=30
        )
        assert r.status_code == 400
        assert r.json()["error"]["message"].startswith("Invalid regex: ")

    def test_skipped_secrets_are_named(self, companion_url: str, grep_probe: dict[str, str]) -> None:
        """HA reads `secrets.yaml` through the `!include`; the companion never does, and says so."""
        body = _grep(companion_url, grep_probe, term="grepprobe")

        assert {"location": "secrets.yaml", "reason": "unreadable"} in body.get("skipped", []), body.get("skipped")

    def test_cap_is_stated_with_exact_counts(self, companion_url: str, grep_probe: dict[str, str]) -> None:
        body = _grep(companion_url, grep_probe, term="grepcap_")

        assert body["total"] == CAP_KEYS
        assert body["truncated"] is True
        assert len(body["hits"]) == 1000
        assert {h["kind"] for h in body["hits"]} == {"key"}
        assert body["sources"] == [{"location": "automations.yaml", "key_hits": CAP_KEYS, "value_hits": 0}]

    def test_ref_scan_and_replace_unchanged(self, companion_url: str, grep_probe: dict[str, str]) -> None:
        """AC-7 on the live stack: the exact-id routes still answer whole-token only, in their old shape.

        The byte-exact pins live in the unit tier (``tests/test_refgrep.py``), on a
        config this suite owns. Here the graph is HA's own onboarding config, whose
        partial-walk record (``themes`` missing, ``secrets.yaml`` refused) belongs to
        the HA release, so the record is checked against ``ref grep``'s walk of the
        same graph and the rest of the body field for field, in its old key order.
        """
        walked = _grep(companion_url, grep_probe, term="grepprobe").get("skipped", [])

        scan = requests.get(
            f"{companion_url}/v1/ref/scan",
            params={"target": "sensor.grepprobe_balkony_p_ac"},
            headers=grep_probe,
            timeout=30,
        )
        assert scan.status_code == 200, scan.text
        body = scan.json()
        assert list(body)[:2] == ["target", "hits"]
        assert set(body) <= {"target", "hits", "skipped"}
        assert body["target"] == "sensor.grepprobe_balkony_p_ac"
        assert body["hits"] == [
            {
                "location": "automations.yaml",
                "path": "[0].conditions[0].value_template",
                "matched_value": "sensor.grepprobe_balkony_p_ac",
            }
        ]
        # ref scan's raw-text prefilter drops a file that cannot hold the target
        # unread and unrecorded (secrets.yaml here), exactly as before.
        assert all(entry in walked for entry in body.get("skipped", []))
        assert {"location": "secrets.yaml", "reason": "unreadable"} not in body.get("skipped", [])

        partial = requests.get(
            f"{companion_url}/v1/ref/scan", params={"target": "grepprobe_balkony"}, headers=grep_probe, timeout=30
        )
        assert partial.status_code == 200
        assert partial.json()["hits"] == [], "ref scan must stay whole-token"

        replace = requests.post(
            f"{companion_url}/v1/ref/replace",
            json={"old": "sensor.grepprobe_wp_vl", "new": "sensor.grepprobe_wp_new", "dry_run": True},
            headers=grep_probe,
            timeout=30,
        )
        assert replace.status_code == 200, replace.text
        body = replace.json()
        assert list(body) == ["status", "changes", "skipped"]
        assert body["status"] == "dry_run"
        assert body["changes"] == [
            {
                "location": "automations.yaml",
                "path": "[0].triggers[0].entity_id[0]",
                "before": "sensor.grepprobe_wp_vl",
                "after": "sensor.grepprobe_wp_new",
            }
        ]
        # No prefilter on the write path: it walks exactly what ref grep walks.
        assert body["skipped"] == walked
