"""Read-only endpoint answering the question every create route asks itself.

``companion.wiring`` decides whether Home Assistant reads the file a create
would write to (C-10). Until now that answer existed only *inside* the create:
a client could not learn it without attempting the write. So hactl's dry run —
which is required to fail exactly where ``--confirm`` fails (hactl INVARIANTS
H-2) — printed "would create helper" on an instance where every confirmed
create was a deterministic 400, because `input_boolean:` is defined inline in
`configuration.yaml`. Eight domains, eight confident previews, eight failures.

The alternative was for hactl to parse `configuration.yaml` itself and
re-derive the rule in Go. That is six rules (labelled domain keys, follow the
include, `!include_dir_*`, the packages blind spot, multi-include ambiguity,
path containment) in a second language, with a second set of refusal texts —
the four-copy contract drift this project has already paid for once. Exposing
the existing probe keeps one implementation and one wording: the ``reason``
below is the same string the create route raises as its 400.

Deliberately *not* a ``dry_run`` parameter on ``POST /v1/config/helper``: C-4
requires every endpoint declaring ``dry_run`` to default it to ``true``, which
would turn every existing client's create into a no-op. A separate GET has no
default to get wrong, and it generalises — the same probe serves the
``template``/``script``/``automation`` previews, which have the identical gap.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from aiohttp import web

from companion.routes.automations import AUTOMATION_DOMAIN, AUTOMATIONS_FILE
from companion.routes.helpers import ALLOWED_DOMAINS, yaml_file_for_domain
from companion.routes.scripts import SCRIPT_DOMAIN, SCRIPTS_FILE
from companion.routes.templates import TEMPLATE_DOMAIN, TEMPLATE_FILE
from companion.wiring import NotWiredError, wired_target


@dataclass
class RouteDef:
    method: str
    path: str
    handler: object


#: Domain -> the file a create writes to when ``configuration.yaml`` names no
#: other. Derived from the create routes' own constants rather than restated,
#: so a route that changes its conventional file cannot leave this probe
#: predicting the old one.
CONVENTIONAL_FILES: dict[str, str] = {
    AUTOMATION_DOMAIN: AUTOMATIONS_FILE,
    SCRIPT_DOMAIN: SCRIPTS_FILE,
    TEMPLATE_DOMAIN: TEMPLATE_FILE,
    **{domain: yaml_file_for_domain(domain) for domain in ALLOWED_DOMAINS},
}


def _relative(target: Path, base: str) -> str:
    """The target as the caller's config directory sees it, never a container path."""
    try:
        return str(target.relative_to(Path(base).resolve()))
    except ValueError:  # pragma: no cover — wired_target guarantees containment (C-3)
        return target.name


async def get_wiring(request: web.Request) -> web.Response:
    """GET /v1/config/wiring?domain=<domain> — would a create for this domain work?

    ``wired: true`` carries ``file``: the config-relative path a new entry would
    be written to. ``wired: false`` carries ``reason``: the exact message the
    create route answers 400 with. Both are 200 — "this instance is not wired
    for that domain" is an answer to the question, not a failure to answer it,
    and a client must not have to scrape an error envelope to preview a write.
    """
    base = request.app["config_base_path"]
    domain = request.query.get("domain", "")
    if not domain:
        raise web.HTTPBadRequest(text="Missing domain parameter")

    conventional = CONVENTIONAL_FILES.get(domain)
    if conventional is None:
        raise web.HTTPBadRequest(
            text=(
                f"No create route writes '{domain}' config, so there is no layout to probe. "
                f"Known domains: {', '.join(sorted(CONVENTIONAL_FILES))}."
            )
        )

    try:
        target = wired_target(base, domain, conventional)
    except NotWiredError as exc:
        return web.json_response({"domain": domain, "wired": False, "reason": str(exc)})

    return web.json_response({"domain": domain, "wired": True, "file": _relative(target, base)})


routes: list[RouteDef] = [
    RouteDef("GET", "/v1/config/wiring", get_wiring),
]
