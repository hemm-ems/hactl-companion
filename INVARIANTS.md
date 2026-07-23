# Invariants

Cross-cutting rules every route must satisfy. The 2026-07 review showed the
companion's bug class is not "feature misunderstood" but "route #N forgot a
rule routes #1..N-1 follow" — per-feature tests cannot catch that, so each
rule below is enforced by a test that **quantifies over the whole route
table** (`ENDPOINT_META`, proven complete against the registered routes by
`test_openapi.py`), or by a named example test where quantification is not
meaningful.

**Discipline:** a rule without an enforcing test does not get added here.
When behavior changes intentionally, the test and this file change in the
same PR. `tests/test_invariants.py` carries two canaries that force this: the
auth-exemption set is pinned, and every mutating route must be classified
(file-writing probe or service-call reason) before the suite passes.

## C-1 — Auth on every route

Every endpoint except `/v1/health` and `/v1/status` rejects requests with a
missing or wrong bearer token (401). A client-supplied `X-Ingress-Path`
header never bypasses auth on its own — only requests provably originating
from the Supervisor ingress proxy IP (`INGRESS_PROXY_IPS`) may skip the
token.

- Enforced by: `tests/test_invariants.py` (missing/wrong/spoofed, all routes)
- Edge cases: `tests/test_auth.py` (trusted-proxy bypass, exemption behavior)

## C-2 — Fail closed without a configured token

With `SUPERVISOR_TOKEN` unset there is nothing to authenticate against:
every credential — including an empty bearer — gets 503, never a pass.

- Enforced by: `tests/test_auth.py::test_auth_empty_server_token_fails_closed`

## C-3 — Path containment

Every config file path resolves inside the configured base directory
(`Path.is_relative_to`, not prefix matching); `secrets.yaml` is never
readable or writable regardless of location.

- Enforced by: `tests/test_pathguard.py`,
  `tests/test_config_write.py::test_write_path_traversal_rejected`,
  `tests/test_config_write.py::test_write_secrets_denied`

## C-4 — Dry-run is the default wherever it is offered

Every endpoint that declares a `dry_run` parameter (query or request body)
defaults it to `true` in the spec **and** in the handler: a request that
omits `dry_run` must not modify any file on disk. Scope note: only the
update endpoints (`PUT` on file/template/script/automation) and
`POST /v1/ref/replace` offer dry-run; creates and deletes apply immediately
and are covered by C-5.

- Enforced by: `tests/test_invariants.py::test_every_declared_dry_run_defaults_true`
  (spec), `::test_omitted_dry_run_never_touches_disk` (behavior),
  `::test_dry_run_gated_classification_matches_spec` (spec↔behavior agreement)

## C-5 — Backup before mutate

Any applied change to a pre-existing config file first copies the prior
content into `.hactl_backups/` next to the file (bounded retention, see
`companion/backups.py`).

- Enforced by: `tests/test_invariants.py::test_applied_write_backs_up_every_modified_file`
  (all file-mutating routes), `tests/test_config_write.py::test_apply_creates_backup`

## C-6 — Validate or roll back on applied config writes

An applied `PUT /v1/config/file` runs HA `check_config`; when validation
fails or is unavailable, the prior state is restored (a brand-new file is
removed — there is no backup to restore).

- Enforced by: `tests/test_config_write.py::test_apply_validation_failure_restores`,
  `::test_apply_new_file_validation_failure_removes_file`,
  `::test_apply_validation_unavailable_rolls_back`

## C-7 — The spec is generated and complete

`openapi/companion-v1.yaml` is generated from `ENDPOINT_META` (`make spec`)
and never hand-edited. Every registered route has a spec entry and vice
versa; responses carry no undocumented fields; every query parameter a
handler reads is declared.

- Enforced by: `tests/test_openapi.py` (route↔spec completeness, committed
  YAML matches generator), `tests/test_spec_conformance.py` (field-granular
  responses, query params)

## C-8 — Errors are JSON envelopes

All 4xx/5xx responses are `{"error": {"code", "message"}}` with a JSON
content type, so hactl never has to scrape HTML or plain text.

- Enforced by: `tests/test_auth.py::test_error_responses_use_json_envelope`

## C-9 — The related-graph is reconciled against HA's own answer

`/v1/related/entity` computes relationships from the on-disk `.storage`
snapshot and config files — it does **not** ask HA. For any behaviour HA can
answer itself the expected value must be derived from HA at test time (TC-1),
never from a hand-authored fixture (a fixture and the code can be wrong
together — the original `related_fixture` failure class). So for a live
entity, the automation relations the endpoint reports must be a **superset** of
the automations HA's own `search/related` command reports for that entity —
companion may report *more* (e.g. Jinja-template references HA's static search
misses, the reason its boundary-aware matcher exists) but never *fewer* — and
it must invent no automation relation for an entity HA relates to nothing. This
is a named-example invariant (the one route where HA is a reachable oracle);
the hand-authored `related_fixture` remains valid only as a unit-test *input*.

- Enforced by: `tests/integration/test_related.py::TestRelatedEntityHAOracle::test_related_reconciles_with_ha_search_related`
  (computes HA's `search/related` answer live and reconciles the companion's
  against it; the fixture-shape/​auth Docker test `TestRelatedEntity` is not an
  oracle and does not satisfy this)

---

Client-side counterparts (retry idempotency, CLI confirm gate, vendored-spec
drift) live in the hactl repo's `INVARIANTS.md`.
