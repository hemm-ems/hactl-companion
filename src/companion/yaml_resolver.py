"""YAML !include resolver for Home Assistant configuration files."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ruamel.yaml import YAML

from companion.pathguard import is_denied, is_within


class CircularIncludeError(Exception):
    """Raised when a circular !include is detected."""


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
        """Resolve a single !include-family tag."""
        if tag == "!include":
            return self._include_file(context_dir / value.strip(), visited)
        if tag == "!include_dir_named":
            return self._include_dir_named(context_dir / value.strip(), visited)
        if tag == "!include_dir_list":
            return self._include_dir_list(context_dir / value.strip(), visited)
        if tag == "!include_dir_merge_list":
            return self._include_dir_merge_list(context_dir / value.strip(), visited)
        if tag == "!include_dir_merge_named":
            return self._include_dir_merge_named(context_dir / value.strip(), visited)
        # An unresolved tag keeps its directive text. Returning the bare value
        # made `!secret home_lat` render as the string "home_lat" — a fabricated
        # value indistinguishable from a real one — and any future include-family
        # tag would silently degrade the same way rather than being visibly
        # unresolved. Secrets are never read here; see is_denied/_check_path.
        return f"{tag} {value}".strip()

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
