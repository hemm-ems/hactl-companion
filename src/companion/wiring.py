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

from companion.pathguard import is_denied_path, is_within

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

    Every path this module hands out goes through here, the conventional-name
    fallbacks included. Those look like constants at the call sites that matter
    most (``automations.yaml``), but the helper routes build theirs from the
    ``?domain=`` query parameter — so "it is a literal" was true of some callers
    and not of the class, which is the shape of every C-3 hole this project has
    had.
    """
    base_path = Path(base).resolve()
    target = (base_path / relative).resolve()
    if not is_within(target, base_path):
        msg = f"'{relative}' is included from outside the config directory; refusing to write there"
        raise NotWiredError(msg)
    if is_denied_path(target, base_path):
        msg = f"access to '{relative}' is denied"
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

    default_target = _contained_path(base, conventional_file)
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

    .. deprecated::
        Returns ONE file, which is why live-fire #105 exists: three of the four
        ways a domain reaches HA are not one file, and for each of them this
        falls back to a conventional name that does not exist. Reads resolve
        through :func:`readable_domain_files` now. This remains only for the
        create path's "which single file may I append to" question, where one
        answer is the right shape.
    """
    try:
        return wired_target(base, domain, conventional_file)
    except NotWiredError:
        return _contained_path(base, conventional_file)


# ---------------------------------------------------------------------------
# Reading: where a domain's entries actually ARE
# ---------------------------------------------------------------------------
#
# Live-fire #105. The read routes resolved through the CREATE path's question —
# "which single file may I safely append to?" — and inherited its refusals. For
# an inline domain, a directory include, or a package, that question has no
# answer, so the read fell back to the conventional `<domain>.yaml`, which on
# such an instance does not exist. `GET /v1/config/helpers` returned nothing for
# a domain holding three helpers and `GET /v1/config/helper` 404'd under a
# message listing files the helper is not in.
#
# The read question is a different one and has a different shape: not "which
# file may I write" but "which files hold entries". Answering it needs HA's own
# rules, read from `annotatedyaml.loader` rather than assumed:
#
#   * every `!include_dir_*` walks its directory RECURSIVELY (`os.walk`),
#     matching `*.yaml`, skipping dot-prefixed names and `secrets.yaml`;
#   * `!include_dir_merge_named` and `!include_dir_merge_list` merge each file's
#     own top-level structure — so a member file's root IS the domain's config;
#   * `!include_dir_named` and `!include_dir_list` do NOT. There the entry's
#     identity comes from the FILE (its name, or its position), not from
#     anything written in the document. That is a third entry model and it is
#     declared debt below rather than guessed at.

#: The directory includes whose member files carry the domain's own structure.
MERGE_DIR_TAGS = frozenset({"!include_dir_merge_named", "!include_dir_merge_list"})

#: The directory includes this module does not resolve, and why. A domain wired
#: through one of these reads as unwired rather than as wrong — the entry ids
#: would have to be invented from filenames, and an invented id is what #104 was.
UNRESOLVED_DIR_TAGS = {
    "!include_dir_named": "entry ids would come from the file NAMES, not from the documents",
    "!include_dir_list": "each file is one entry with no id of its own",
}


class DomainFile:
    """One file holding config for a domain, and how to reach it inside that file.

    ``key_path`` is empty when the file's root IS the domain's config — an
    ``!include`` target, or a member of a merging directory include. It is
    non-empty when the config sits under keys, which is what an inline domain
    (``configuration.yaml``) and a package file look like.

    That distinction is the whole reason this type exists rather than a bare
    ``Path``. Reading is the same either way; WRITING is not. ``surgical`` splices
    an entry into a document whose root is the domain's mapping, so a file with a
    ``key_path`` can be read and must not be spliced — and the write routes share
    their loader with the read routes, so without this a widened read would have
    quietly pointed ``DELETE`` at ``configuration.yaml``.
    """

    __slots__ = ("key_path", "path")

    def __init__(self, path: Path, key_path: tuple[str, ...] = ()) -> None:
        self.path = path
        self.key_path = key_path

    @property
    def writable(self) -> bool:
        """Whether a single entry can be spliced into this file."""
        return not self.key_path

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, DomainFile):
            return NotImplemented
        return self.path == other.path and self.key_path == other.key_path

    def __hash__(self) -> int:
        return hash((self.path, self.key_path))

    def __repr__(self) -> str:
        return f"DomainFile({self.path.name!r}, {self.key_path!r})"


def _dir_members(base: str | Path, relative: str) -> list[Path]:
    """Every file a directory include reads, by HA's rules.

    ``annotatedyaml.loader._find_files``: ``os.walk`` (so recursive), ``*.yaml``,
    skipping anything whose name starts with a dot, and skipping ``secrets.yaml``.
    Sorted, so a listing is a function of the directory rather than of the
    filesystem's order.
    """
    try:
        root = _contained_path(base, relative)
    except NotWiredError:
        return []
    if not root.is_dir():
        return []
    found = [
        path
        for path in sorted(root.rglob("*.yaml"))
        if path.is_file()
        and path.name != "secrets.yaml"
        and not any(part.startswith(".") for part in path.relative_to(root).parts)
    ]
    return [path for path in found if _is_readable(base, path)]


def _is_readable(base: str | Path, path: Path) -> bool:
    """Whether the path guard permits reading this file at all."""
    base_path = Path(base).resolve()
    return is_within(path, base_path) and not is_denied_path(path, base_path)


def _package_files(base: str | Path, config: dict[str, Any], domain: str) -> list[DomainFile]:
    """Files reached through ``homeassistant: packages:`` that configure ``domain``.

    The fourth way a domain gets into Home Assistant, and the one with no
    top-level key in ``configuration.yaml`` at all — so nothing that reads only
    that file can find it. A package file's root is a mapping of DOMAINS, so the
    domain's config sits one key down, which is exactly what ``key_path``
    expresses.
    """
    ha_section = config.get("homeassistant")
    if not isinstance(ha_section, dict) or PACKAGES_KEY not in ha_section:
        return []

    value = ha_section[PACKAGES_KEY]
    tag = _tag_of(value)
    if tag is None or not tag.startswith("!include"):
        # Packages written out inline sit under
        # `homeassistant: packages: <name>: <domain>:`. Reading them is possible
        # and writing them is not; deferred with the rest of the write question
        # rather than half-modelled here.
        return []

    if tag == "!include":
        candidates = [_contained_path(base, str(value.value).strip())]
    else:
        candidates = _dir_members(base, str(value.value).strip())

    found: list[DomainFile] = []
    for path in candidates:
        package = _read_mapping(path)
        if package is None:
            continue
        found.extend(DomainFile(path, (key,)) for key in domain_keys(package, domain))
    return found


def _read_mapping(path: Path) -> dict[str, Any] | None:
    """Parse a YAML file that should be a mapping, or None if it is not readable.

    Unreadable is not an error here. This runs under GET routes over files the
    caller did not name, and one malformed package must not turn a listing into
    a 500 — the same argument :func:`_load_configuration` makes for
    ``configuration.yaml`` itself.
    """
    if not path.is_file():
        return None
    try:
        with open(path, encoding="utf-8") as handle:
            data = yaml.load(handle)
    except (YAMLError, OSError, UnicodeDecodeError):
        return None
    return data if isinstance(data, dict) else None


def readable_domain_files(base: str | Path, domain: str, conventional_file: str) -> list[DomainFile]:
    """Every file that can hold an entry for ``domain``, in the order HA reads them.

    All four wirings, because a read that knows one of them is a read that
    reports an empty instance as empty (live-fire #105):

    ===========================  ==========================================
    ``domain: !include f.yaml``  the file's root is the domain's config
    ``domain:`` written inline   ``configuration.yaml``, under that key
    ``!include_dir_merge_*``     every ``*.yaml`` under it, roots as above
    ``homeassistant: packages:`` each package file, under its domain key
    ===========================  ==========================================

    The conventional file is the answer only when nothing else is — an unwired
    instance, or a ``configuration.yaml`` that cannot be parsed. That keeps the
    previous behaviour for the case it was right for, which is the one where a
    file with the conventional name may well exist and be what the caller means.
    """
    try:
        config = _load_configuration(base)
    except NotWiredError:
        return [DomainFile(_contained_path(base, conventional_file))]

    found: list[DomainFile] = []
    for key in domain_keys(config, domain):
        value = config[key]
        tag = _tag_of(value)
        if tag == "!include":
            found.append(DomainFile(_contained_path(base, str(value.value).strip())))
        elif tag in MERGE_DIR_TAGS:
            found.extend(DomainFile(path) for path in _dir_members(base, str(value.value).strip()))
        elif tag in UNRESOLVED_DIR_TAGS:
            continue
        elif tag is None:
            found.append(DomainFile(_contained_path(base, CONFIGURATION_FILE), (key,)))

    found.extend(_package_files(base, config, domain))

    if not found:
        found.append(DomainFile(_contained_path(base, conventional_file)))
    # A file wired twice (a labelled key and the bare one naming the same
    # include) must not have its entries counted twice.
    seen: set[DomainFile] = set()
    unique: list[DomainFile] = []
    for entry in found:
        if entry not in seen:
            seen.add(entry)
            unique.append(entry)
    return unique


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
