"""YAML !include resolver for Home Assistant configuration files."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ruamel.yaml import YAML
from ruamel.yaml.representer import RoundTripRepresenter

from companion.pathguard import is_denied, is_within


class CircularIncludeError(Exception):
    """Raised when a circular !include is detected."""


class UnknownIncludeTagError(Exception):
    """Raised for an include-family tag this resolver does not implement.

    An include tag exists to pull file content into the document. If we do not
    implement one, the content it names is simply absent from every answer built
    on the resolved tree — and absence reads as "there is nothing there". That is
    how ``!include_dir_merge_list`` made a whole split automation directory
    invisible to ``ent related``, ``ref scan`` and ``config file`` (one of the
    five root causes of hactl#81) while every test stayed green. Degrading was
    the bug; failing loudly is the fix.
    """


# Every include-family tag Home Assistant defines, and which this resolver
# implements. Enumerated rather than prefix-matched so that a *new* HA include
# tag is an unknown tag (a loud error) instead of quietly matching nothing.
INCLUDE_TAGS: frozenset[str] = frozenset(
    {
        "!include",
        "!include_dir_list",
        "!include_dir_merge_list",
        "!include_dir_named",
        "!include_dir_merge_named",
    }
)

# HA tags that are known, carry no file content, and are deliberately left
# unresolved — each for a reason recorded here rather than by omission:
#   !secret   — the value lives in secrets.yaml, which this service must never
#               read (C-3). The directive text names the KEY, which is the whole
#               truth we are allowed to tell.
#   !env_var  — resolving it would substitute the *companion container's*
#               environment, not Home Assistant's, and invent a value that
#               differs from the running config.
#   !input    — a blueprint placeholder; it only has a value once HA
#               instantiates the blueprint, which happens nowhere near here.
# Preserving the directive is truthful for all three: nothing is hidden, and the
# rendered text says exactly what the config says.
#
# INCLUDE_TAGS | PRESERVED_TAGS is HA's *entire* YAML tag vocabulary. Asked of a
# live instance rather than assumed, by
# tests/integration/test_live.py::TestIncludeWiring::
# test_home_assistant_refuses_any_tag_outside_its_vocabulary: writing
# `probe_key: !my_custom_thing x` into configuration.yaml makes HA's own
# check_config answer
#     invalid | Error loading /config/configuration.yaml: could not determine a
#               constructor for the tag '!my_custom_thing'
# and the same for a plausible-but-nonexistent include tag
# (`!include_dir_merge_flat`). HA's loader has a closed constructor set; there is
# no such thing as a working HA config carrying a tag outside these eight.
PRESERVED_TAGS: frozenset[str] = frozenset({"!secret", "!env_var", "!input"})


class PreservedTag(str):
    """A tag left unresolved, carried through the tree as its directive text.

    A ``str`` subclass because that text IS the truthful rendering, and because
    everything downstream of the resolver reads a leaf as text — the reference
    walkers in :mod:`companion.routes.related` test leaves with
    ``isinstance(value, str)``, and a bare object would have gone unseen there.

    The tag and its argument are kept beside the text for one reason: the
    **dump**. Preserving the directive as a plain string was only half of the
    job, because to YAML a string beginning with ``!`` is not a tag — it is a
    string that must be quoted to stay a string. So the emitter quoted it, and
    the file came back out saying something else::

        entity_id: !input button_entity      # what the blueprint says
        entity_id: '!input button_entity'    # what resolved mode rendered

    The second is a working blueprint turned into one that triggers on an
    entity literally named ``!input button_entity``, and it is still valid YAML
    so nothing complains (finding #20). The same happened to
    ``Authorization: !secret tibber_token``. The registered representer below
    puts the tag back where the resolver said it was.

    The second attribute is deliberately ``argument`` and not ``value``:
    :meth:`YamlResolver._walk_and_resolve` recognises a ruamel tagged node by
    ``hasattr(node, "tag") and hasattr(node, "value")``, and a PreservedTag that
    answered to both would be re-resolved as if it had just been parsed.
    """

    tag: str
    argument: str

    def __new__(cls, tag: str, argument: str) -> PreservedTag:
        arg = argument.strip()
        obj = super().__new__(cls, f"{tag} {arg}".strip())
        obj.tag = tag
        obj.argument = arg
        return obj


def _represent_preserved_tag(representer: RoundTripRepresenter, data: PreservedTag) -> Any:
    """Emit a :class:`PreservedTag` as the tag it came from, not as a string."""
    return representer.represent_scalar(data.tag, data.argument)


# Registered on the round-trip representer itself, at import, once. Only this
# module can mint a PreservedTag, so the scope is exactly right: wherever one
# reaches a dumper — this resolver's, ``routes/config.py``'s, ``surgical.py``'s
# — it has to come out as a tag. A per-instance registration would have left
# whichever YAML() nobody thought about still quoting it.
RoundTripRepresenter.add_representer(PreservedTag, _represent_preserved_tag)


def claims_to_include(tag: str) -> bool:
    """True if ``tag`` advertises that it pulls in content from elsewhere.

    Drawn on the name because the name is all an unimplemented tag gives us, and
    HA names this family by prefix (``!include``, ``!include_dir_*``). The line
    is deliberately narrow: a tag has to say "include" to be treated as
    content-bearing. Wrong in the permissive direction re-creates the bug that
    hid a whole automation directory behind a tag nobody implemented.

    The realistic case this exists for is *forward* compatibility, not exotic
    user configs: today's HA refuses to load any tag outside
    ``INCLUDE_TAGS | PRESERVED_TAGS`` (see the note there), so an unknown tag
    can only reach us from an HA newer than this build. That is exactly when
    silently resolving it to nothing would be most convincing and most wrong.
    """
    return tag.lstrip("!").lower().startswith("include")


class YamlResolver:
    """Resolves HA YAML !include directives, returning complete content."""

    def __init__(self, base_path: str | Path) -> None:
        self._base = Path(base_path).resolve()
        self._yaml = YAML()
        self._yaml.preserve_quotes = True

    def _check_path(self, path: Path) -> None:
        """Validate path is within base and not denied."""
        resolved = path.resolve()
        if not is_within(resolved, self._base):
            msg = f"Path traversal not allowed: {path}"
            raise ValueError(msg)
        if is_denied(resolved.name):
            msg = f"Access to {resolved.name} is denied"
            raise PermissionError(msg)

    def load(self, rel_path: str, *, resolve: bool = True) -> Any:
        """Load a YAML file, optionally resolving !include directives."""
        target = (self._base / rel_path).resolve()
        self._check_path(target)
        if not target.is_file():
            msg = f"File not found: {rel_path}"
            raise FileNotFoundError(msg)

        if not resolve:
            return self._yaml.load(target)

        visited: set[str] = set()
        return self._resolve_file(target, visited)

    def _resolve_file(self, path: Path, visited: set[str]) -> Any:
        """Load and recursively resolve a single YAML file."""
        key = str(path.resolve())
        if key in visited:
            msg = f"Circular include detected: {path}"
            raise CircularIncludeError(msg)
        visited.add(key)

        self._check_path(path)
        content = path.read_text(encoding="utf-8")
        data = self._resolve_includes(content, path.parent, visited)
        visited.discard(key)
        return data

    def _resolve_includes(self, content: str, context_dir: Path, visited: set[str]) -> Any:
        """Parse YAML content and resolve any !include-family tags within it."""
        data = self._yaml.load(content) if content.strip() else None
        if data is None:
            return data
        return self._walk_and_resolve(data, context_dir, visited)

    def _walk_and_resolve(self, node: Any, context_dir: Path, visited: set[str]) -> Any:
        """Walk a parsed YAML tree and resolve any tagged include values."""
        if isinstance(node, dict):
            resolved: dict[str, Any] = {}
            for k, v in node.items():
                resolved[k] = self._walk_and_resolve(v, context_dir, visited)
            return resolved
        if isinstance(node, list):
            return [self._walk_and_resolve(item, context_dir, visited) for item in node]
        # Check for tagged scalar (ruamel.yaml tagged values)
        if hasattr(node, "tag") and hasattr(node, "value"):
            tag = node.tag.value if hasattr(node.tag, "value") else str(node.tag)
            value = str(node.value) if hasattr(node, "value") else str(node)
            return self._resolve_tag(tag, value, context_dir, visited)
        return node

    def _resolve_tag(self, tag: str, value: str, context_dir: Path, visited: set[str]) -> Any:
        """Resolve a single tagged scalar, on one of three tracks.

        The tag decides which, and the boundary between the tracks is the whole
        point of this function:

        1. **A known include tag** (:data:`INCLUDE_TAGS`) is resolved — the file
           or directory it names is read and inlined.
        2. **An unknown tag that claims to include content** — anything else in
           the ``!include*`` family, e.g. a tag HA adds after this release — is a
           **hard error**. Preserving it would leave the content it names missing
           from the answer with nothing to say so, and a caller cannot tell an
           empty directory from a directory we never opened. That is exactly the
           `!include_dir_merge_list` failure, and the next one would be silent
           the same way.
        3. **Any other tag keeps its directive text.** Chiefly the known
           value-carrying tags (:data:`PRESERVED_TAGS`). Preserving is truthful
           for them: they name no file, so nothing is hidden by leaving them
           alone. Note this is *preserve*, not unwrap — returning the bare value
           made ``!secret home_lat`` render as the string ``home_lat``, the
           secret's key standing where its value belongs, indistinguishable from
           a real setting.

           A tag outside all three sets lands here too, and that is deliberate
           even though HA itself would refuse to load such a config (see
           :data:`PRESERVED_TAGS`). Reading a broken config is precisely when
           somebody needs this service: HA has already failed, and a second
           refusal on top of HA's would remove the only view they have left.
           Nothing is concealed by rendering it — the directive is printed as
           written, and the missing-content problem that justifies track 2 does
           not arise for a tag that names no file.

           The result is a :class:`PreservedTag`, which reads as its directive
           text everywhere and re-emits as a tag when the tree is dumped. A
           plain string here was finding #20: preserved correctly, quoted on
           the way out, and so changed in meaning by the rendering.
        """
        if tag in INCLUDE_TAGS:
            path = context_dir / value.strip()
            if tag == "!include":
                return self._include_file(path, visited)
            if tag == "!include_dir_named":
                return self._include_dir_named(path, visited)
            if tag == "!include_dir_list":
                return self._include_dir_list(path, visited)
            if tag == "!include_dir_merge_list":
                return self._include_dir_merge_list(path, visited)
            return self._include_dir_merge_named(path, visited)

        if claims_to_include(tag):
            msg = (
                f"Unsupported include directive {tag!r} (at {value.strip()!r}): this resolver knows "
                f"{', '.join(sorted(INCLUDE_TAGS))}. Everything {tag} would have pulled in is missing "
                f"from this answer, so the answer is not shown. Read the file unresolved "
                f"(resolve=false) to see it as written, and please report the tag."
            )
            raise UnknownIncludeTagError(msg)

        return PreservedTag(tag, value)

    def _include_file(self, path: Path, visited: set[str]) -> Any:
        """Resolve !include <path> — inline file content."""
        resolved = path.resolve()
        self._check_path(resolved)
        if not resolved.is_file():
            msg = f"Included file not found: {path}"
            raise FileNotFoundError(msg)
        return self._resolve_file(resolved, visited)

    def _include_dir_named(self, dir_path: Path, visited: set[str]) -> dict[str, Any]:
        """Resolve !include_dir_named <dir> — files become named dict entries."""
        resolved = dir_path.resolve()
        self._check_path(resolved)
        if not resolved.is_dir():
            return {}
        result: dict[str, Any] = {}
        for f in sorted(resolved.iterdir()):
            if f.is_file() and f.suffix in (".yaml", ".yml") and not is_denied(f.name):
                name = f.stem
                content = self._resolve_file(f, visited)
                result[name] = content
        return result

    def _include_dir_list(self, dir_path: Path, visited: set[str]) -> list[Any]:
        """Resolve !include_dir_list <dir> — files become list items."""
        resolved = dir_path.resolve()
        self._check_path(resolved)
        if not resolved.is_dir():
            return []
        result: list[Any] = []
        for f in sorted(resolved.iterdir()):
            if f.is_file() and f.suffix in (".yaml", ".yml") and not is_denied(f.name):
                content = self._resolve_file(f, visited)
                result.append(content)
        return result

    def _include_dir_merge_list(self, dir_path: Path, visited: set[str]) -> list[Any]:
        """Resolve !include_dir_merge_list <dir> — concatenate list files.

        This is the standard tag for a split automations/ or scripts/ directory,
        where each file holds a list and the lists are joined into one. Until it
        was implemented the tag fell through to the unknown-tag branch and
        resolved to the bare directory string, so every automation in a split
        layout was invisible to `ent related`, `ref scan` and `config file`.
        """
        resolved = dir_path.resolve()
        self._check_path(resolved)
        if not resolved.is_dir():
            return []
        result: list[Any] = []
        for f in sorted(resolved.iterdir()):
            if f.is_file() and f.suffix in (".yaml", ".yml") and not is_denied(f.name):
                content = self._resolve_file(f, visited)
                if isinstance(content, list):
                    result.extend(content)
                elif content is not None:
                    result.append(content)
        return result

    def _include_dir_merge_named(self, dir_path: Path, visited: set[str]) -> dict[str, Any]:
        """Resolve !include_dir_merge_named <dir> — deep merge named files."""
        resolved = dir_path.resolve()
        self._check_path(resolved)
        if not resolved.is_dir():
            return {}
        result: dict[str, Any] = {}
        for f in sorted(resolved.iterdir()):
            if f.is_file() and f.suffix in (".yaml", ".yml") and not is_denied(f.name):
                content = self._resolve_file(f, visited)
                if isinstance(content, dict):
                    result = self._deep_merge(result, content)
        return result

    @staticmethod
    def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
        """Deep merge two dicts, override wins on conflict."""
        merged = dict(base)
        for k, v in override.items():
            if k in merged and isinstance(merged[k], dict) and isinstance(v, dict):
                merged[k] = YamlResolver._deep_merge(merged[k], v)
            else:
                merged[k] = v
        return merged

    def dump_to_string(self, data: Any) -> str:
        """Serialize data back to YAML string."""
        from io import StringIO

        stream = StringIO()
        self._yaml.dump(data, stream)
        return stream.getvalue()

    def save(self, rel_path: str, data: Any) -> None:
        """Round-trip dump ``data`` back to ``rel_path``, within base only.

        Uses the resolver's own YAML instance (``preserve_quotes=True``) so the
        formatting, comments and quote style of untouched nodes survive. Path is
        re-validated through ``_check_path``, so writes outside base or to a
        denied file (``secrets.yaml``) raise rather than escaping the config dir.
        """
        target = (self._base / rel_path).resolve()
        self._check_path(target)
        with target.open("w", encoding="utf-8") as stream:
            self._yaml.dump(data, stream)
