"""Prove Home Assistant actually reads a file before a route writes to it.

Every YAML-backed create route in this service picks its target file *by naming
convention* — ``template.yaml``, ``scripts.yaml``, ``automations.yaml``,
``<helper_domain>.yaml``. Convention is not wiring: HA only reads such a file if
``configuration.yaml`` carries a top-level key for the domain that ``!include``s
it. Without that key the write lands on disk, the route answers ``201 created``
and the entity never appears — the file HA never read (D46).

``routes/helpers.py`` has carried a private version of this check since the
helper domains shipped (HA's default onboarding config wires no helper domain,
so the failure was routine there). ``template``/``script``/``automation`` never
got it — the same rule, forgotten by routes #N. This module is that rule, once,
for everyone, and it is deliberately faithful to HA on two points:

* **Labelled domain keys.** HA's ``extract_domain_configs`` matches
  ``^<domain>(| .+)$``, so ``automation ui:`` and ``template legacy:`` are real,
  documented configurations. A guard that only looked for the bare ``domain:``
  key would refuse a wired instance — a guard that is wrong in the strict
  direction breaks working setups, which is how guards get deleted.
* **Follow the include, do not assume the name.** ``template: !include
  my_templates.yaml`` means HA reads *that* file; the conventional name is only
  the default. The whole route family resolves through here, so a create and the
  list/get/delete that follow it can never disagree about which file is real.

The check is intentionally answerable from ``configuration.yaml`` alone. Its
known blind spot is the ``homeassistant: packages:`` mechanism, where a domain
may be configured from inside a package file; such an instance is refused with
an explanatory message rather than written to on a guess. ``PUT /v1/config/file``
(path named by the caller, validated by HA's ``check_config`` — C-6) remains the
escape hatch for any layout this module cannot prove.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from aiohttp import web
from ruamel.yaml import YAML
from ruamel.yaml.error import YAMLError

from companion.pathguard import is_denied, is_within

yaml = YAML()
yaml.preserve_quotes = True

CONFIGURATION_FILE = "configuration.yaml"
PACKAGES_KEY = "packages"


class NotWiredError(Exception):
    """Home Assistant does not read the file this domain would be written to.

    Carries a message naming what was found and what to do about it; routes
    surface it verbatim in the C-8 error envelope.
    """


def _tag_of(node: Any) -> str | None:
    """Return the ruamel YAML tag (e.g. ``!include``) of a loaded node, or None."""
    tag = getattr(node, "tag", None)
    return getattr(tag, "value", None) if tag is not None else None


def _contained_path(base: str | Path, relative: str) -> Path:
    """Resolve ``relative`` under ``base``, refusing traversal and denied names.

    The include target comes from a user-authored ``configuration.yaml``, so it
    is untrusted input on the same footing as a query parameter (C-3).
    """
    base_path = Path(base).resolve()
    target = (base_path / relative).resolve()
    if not is_within(target, base_path):
        msg = f"'{relative}' is included from outside the config directory; refusing to write there"
        raise NotWiredError(msg)
    if is_denied(target.name):
        msg = f"access to {target.name} is denied"
        raise NotWiredError(msg)
    return target


def domain_keys(config: dict[str, Any], domain: str) -> list[str]:
    """Every key in ``config`` that HA treats as configuring ``domain``.

    Mirrors ``homeassistant.config.extract_domain_configs``: the bare key plus
    any ``<domain> <label>`` form. That HA really honours the labelled form is
    checked against a live instance by
    ``tests/integration/test_live.py::TestIncludeWiring::test_labelled_domain_key_is_live_config``,
    which wires an automation through ``automation <label>:``, reloads, and reads
    the resulting entity back out of HA.
    """
    pattern = re.compile(rf"^{re.escape(domain)}(| .+)$")
    return [key for key in config if isinstance(key, str) and pattern.match(key)]


def _has_packages(config: dict[str, Any]) -> bool:
    """True if ``homeassistant: packages:`` is present — this module's blind spot.

    A domain configured from inside a package file has no top-level key here, so
    the refusal below would otherwise tell the user to add an ``!include`` they
    may not want. Naming the mechanism turns a confusing refusal into an
    actionable one.
    """
    ha_section = config.get("homeassistant")
    return isinstance(ha_section, dict) and PACKAGES_KEY in ha_section


def _load_configuration(base: str | Path) -> dict[str, Any]:
    """Parse ``configuration.yaml``, or raise :class:`NotWiredError` saying why not.

    Every failure mode is folded into ``NotWiredError`` on purpose. This
    function sits under *read* routes too (via :func:`wired_target_or_default`),
    and an unparseable ``configuration.yaml`` must not turn ``GET
    /v1/config/scripts`` — which never needed to read it before — into a 500.
    Unwired and unreadable are the same fact from the caller's side: nothing
    here can prove Home Assistant reads the target file.
    """
    config_path = _contained_path(base, CONFIGURATION_FILE)
    if not config_path.is_file():
        msg = f"{CONFIGURATION_FILE} not found; cannot prove Home Assistant reads anything"
        raise NotWiredError(msg)
    try:
        with open(config_path, encoding="utf-8") as f:
            data = yaml.load(f)
    except (YAMLError, OSError, UnicodeDecodeError) as exc:
        msg = (
            f"{CONFIGURATION_FILE} could not be read as YAML ({exc.__class__.__name__}), so nothing "
            f"about it can be proven: {exc}"
        )
        raise NotWiredError(msg) from exc
    if not isinstance(data, dict):
        msg = f"{CONFIGURATION_FILE} is not a top-level mapping"
        raise NotWiredError(msg)
    return data


def wired_target(base: str | Path, domain: str, conventional_file: str) -> Path:
    """Return the file HA reads for ``domain``, or raise :class:`NotWiredError`.

    ``conventional_file`` is only used to build the advice in the failure
    message and to disambiguate a config that wires the domain from several
    labelled keys; the returned path always follows the ``!include`` actually
    present.
    """
    config = _load_configuration(base)
    keys = domain_keys(config, domain)
    if not keys:
        msg = (
            f"{CONFIGURATION_FILE} has no top-level '{domain}:' key, so Home Assistant never reads "
            f"{conventional_file} — the entry would be written and ignored. "
            f"Add '{domain}: !include {conventional_file}' first."
        )
        if _has_packages(config):
            msg += (
                f" (This config also uses 'homeassistant: {PACKAGES_KEY}:'. If {domain} is configured from "
                f"inside a package, this route cannot tell which package file a new entry belongs in — "
                f"write that file directly with PUT /v1/config/file.)"
            )
        raise NotWiredError(msg)

    default_target = (Path(base).resolve() / conventional_file).resolve()
    includes: list[tuple[str, Path]] = []
    rejected: list[str] = []

    for key in keys:
        value = config[key]
        tag = _tag_of(value)
        if tag == "!include":
            includes.append((key, _contained_path(base, str(value.value).strip())))
        elif tag is not None and tag.startswith("!include_dir_"):
            rejected.append(f"'{key}:' uses {tag}, which cannot be targeted for a single new entry")
        else:
            rejected.append(f"'{key}:' is defined inline, and appending to an inline mapping is not safe")

    if not includes:
        msg = (
            f"Home Assistant reads {domain} config, but not from a file this route can extend: "
            + "; ".join(rejected)
            + f". Write the file directly (PUT /v1/config/file) or add '{domain}: !include {conventional_file}'."
        )
        raise NotWiredError(msg)

    for _key, target in includes:
        if target == default_target:
            return target
    if len(includes) == 1:
        return includes[0][1]

    listed = ", ".join(f"{key} -> {target.name}" for key, target in includes)
    msg = (
        f"{CONFIGURATION_FILE} wires {domain} from several files ({listed}); "
        f"which one a new entry belongs in is ambiguous. Write the chosen file directly (PUT /v1/config/file)."
    )
    raise NotWiredError(msg)


def wired_target_or_default(base: str | Path, domain: str, conventional_file: str) -> Path:
    """The file HA reads for ``domain``, falling back to the conventional name.

    Read/update/delete paths use this: they act on entries that already exist,
    so refusing them would only strand a user who needs to inspect or clean up
    an unwired file. It matters that they resolve through the *same* function as
    the create path — a create that follows the ``!include`` while the list that
    follows it reads the conventional name would report the entry missing right
    after reporting it created.
    """
    try:
        return wired_target(base, domain, conventional_file)
    except NotWiredError:
        return (Path(base).resolve() / conventional_file).resolve()


def require_wired_target(base: str | Path, domain: str, conventional_file: str) -> Path:
    """:func:`wired_target`, raising HTTP 400 with the explanation (C-8 envelope).

    400 rather than a 201-with-a-warning-flag: a flag only prevents a false
    success for callers that decode it, and this project has already shipped the
    counter-example — hactl's Go structs silently dropped the ``reloaded`` field
    the companion sent and the spec documented (D45), turning an unconfirmed
    write back into a confident "created". A refusal cannot be dropped by a
    consumer that does not know about it, and it leaves no half-written file to
    clean up. Callers that genuinely want the bytes on disk have
    ``PUT /v1/config/file``, which says what it does.
    """
    try:
        return wired_target(base, domain, conventional_file)
    except NotWiredError as exc:
        raise web.HTTPBadRequest(text=str(exc)) from exc
