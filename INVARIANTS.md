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

## C-10 — A create proves Home Assistant reads the file first

A route that creates a **new** definition in a file it chose by naming
convention (`template.yaml`, `scripts.yaml`, `automations.yaml`,
`<helper_domain>.yaml`) must first establish that `configuration.yaml` carries
a top-level key for that domain which `!include`s it, and must write to the
file the include actually names. Without that key HA never reads the file: the
write succeeds, the route answers `201 created`, and the entity never appears
(D46). Such a create is refused with 400 and writes nothing.

The domain key is matched the way HA matches it — `^<domain>(| .+)$`, so
`automation ui:` counts (`homeassistant.config.extract_domain_configs`).
Read/update/delete are deliberately **not** guarded: they act on entries that
already exist, and refusing them would strand a user cleaning up a file HA
ignores. They resolve the target through the same function, so a create and the
list that follows it can never disagree about which file is real. Config
layouts this cannot prove (`homeassistant: packages:`, `!include_dir_*`,
several candidate files) are refused with the reason named; `PUT
/v1/config/file` remains the escape hatch (C-6 validates the result).

- Enforced by: `tests/test_invariants.py::test_create_refuses_when_configuration_does_not_include_the_file`
  (sweeps every create route in the `FILE_WRITES` probe table) and
  `tests/test_invariants.py::test_every_file_write_declares_a_wiring_stance`
  (canary: a new file-writing route must declare `wiring` or a
  `no_wiring_reason`), `tests/test_wiring.py` (resolution and refusal rules),
  `tests/integration/test_live.py::TestIncludeWiring::test_create_refuses_until_the_include_exists`
  (both directions against real HA) and
  `::test_labelled_domain_key_is_live_config` (HA is asked whether a labelled
  domain key really is config, and whether removing only the include really
  unloads the entity — the file on disk unchanged)

## C-11 — An include tag this build does not implement is an error, never a shrug

`yaml_resolver.INCLUDE_TAGS` enumerates every include-family tag the resolver
implements. A tag outside it that still claims to include content (anything
matching `!include*`) raises `UnknownIncludeTagError`, surfaced as 400 by
`server.unsupported_include_middleware` so every current and future route that
touches the config graph inherits it. Degrading is the bug: whatever the tag
names is then simply absent from the resolved tree, and a caller cannot tell an
empty directory from one that was never opened — that is how
`!include_dir_merge_list` made a whole split automation directory invisible to
`ent related`, `ref scan` and `config file` while every test stayed green.

`INCLUDE_TAGS | PRESERVED_TAGS` (`!secret`, `!env_var`, `!input`) is HA's entire
YAML vocabulary, so an unknown tag means an HA newer than this build, not an
exotic config. The other two tracks are deliberate: value-carrying tags keep
their directive text (`!secret home_lat` renders as `!secret home_lat`, never
the bare `home_lat`), and `resolve=false` stays available for any file the
resolver refuses.

- Enforced by: `tests/test_resolver.py::test_unknown_include_tag_is_refused_not_degraded`,
  `::test_known_include_tags_still_resolve` (a guard that rejects everything is
  not a guard), `::test_unknown_include_tag_still_readable_unresolved`,
  `::test_preserved_tags_are_not_include_family` and
  `::test_known_include_tags_are_exactly_has_include_vocabulary` (canaries),
  `tests/test_refscan.py::test_scan_refuses_unknown_include_tag` and
  `::test_scan_still_follows_every_known_include_dir_tag`, and
  `tests/integration/test_live.py::TestIncludeWiring::test_unknown_include_tag_is_refused_by_a_live_route`
  plus `::test_home_assistant_refuses_any_tag_outside_its_vocabulary` (HA's own
  loader is asked to confirm the vocabulary is closed)

## C-12 — The response contract is field-level, on every route, both ways

Path-and-method presence is not a contract (TC-5). `test_openapi.py` proves
every route has a spec entry; that is compatible with a handler quietly adding,
renaming or dropping a response field. D45 is the proof it is not theoretical —
hactl's Go structs silently dropped the `reloaded` field the companion sent and
the spec documented, across four copies of the contract, because every
"contract" test checked paths.

So for **every** entry in `ENDPOINT_META`:

1. the route is driven against a real handler, and its response must validate
   against the spec's `response_schema` and carry **no undocumented field**,
   recursively (producer → spec);
2. every field the spec **documents** must be produced by at least one of that
   route's probes, or carry a written reason in `UNOBSERVED_FIELDS`
   (spec → producer). This is also what makes a probe non-vacuous: an empty
   `{}` response satisfies (1) and fails (2);
3. the covered set is **derived from `ENDPOINT_META`**, never hand-listed. A
   new route must be given a probe or an explicit exemption, and the suite is
   red until it is. A hand-maintained list of *covered* routes would drift
   silently (TC-7); a list of *exemptions* cannot, because the canary computes
   the complement.

Exemptions must be loud and enumerated, never silent. `UNDRIVEN` is currently
**empty** — all 37 routes are drivable in the unit tier, WireGuard included
(its `wg`/`wg-quick` calls and monitor registry are substituted; the field
contract on a real WireGuard stack is additionally checked by the `test-wg`
tier). Both exemption tables are kept honest from both sides: an entry naming a
route or field that no longer exists, or a field the probes now produce, fails
as stale.

- Enforced by: `tests/test_spec_conformance.py::test_every_endpoint_is_conformance_classified`
  (the canary), `::test_route_response_conformance` (both directions, one case
  per route), and `::test_unobserved_field_exemptions_are_for_known_routes`

## C-13 — A create never writes into a block it did not create

Home Assistant's unit of rejection in `template.yaml` is the **top-level list
item**. When one entity in a block fails validation HA drops the entire item:
its valid siblings do not go stale, they leave the state machine and come back
as `unavailable` with `restored: true`. Entities in a *different* item are
untouched. Measured against a live HA in both directions — four entities across
four items all registered (so repeating a domain at the top level costs
nothing), then one bad `device_class` added to the first item took that item's
good sensor down while the second item's sensor kept its value; repeated with
`select` options given as a YAML list, same result.

So `POST /v1/config/template` appends every entry as its own new top-level item
and never extends an existing one — not even a state-based block already
declaring the same domain, which is what it used to do. On a real instance that
block is the user's: the first `sensor:` block held two production sensors, the
first `binary_sensor:` block the flat's occupancy sensor, and a single bad
payload filed next to them would have darked all of them. A per-entry block
bounds the damage of a bad entry to that entry. A tool-owned block per domain
was rejected for the same reason — smaller blast radius, still not one.

There is no pre-write validity gate instead: `POST /config/core/check_config`
answered `valid` for both poisoned files above while HA's own setup logged
`Invalid config for 'template' at template.yaml` for them — entity-level
template schema errors surface when the platform sets up, not at config check.
A gate built on it would pass exactly the payloads that cause the harm. The
integration test asserts that `valid`, so the day HA can answer earlier is the
day the suite says so.

Which block an entry lands in is this invariant; how few bytes the write
disturbs is **C-14**, and the create inherits it — the new item is spliced in
rather than re-dumped, so a file whose sequence indent differs from the dumper's
does not come back reformatted around an entry added at its end.

Scope: this is a named example, not a sweep. The other creates append their own
top-level item already (`automations.yaml`) or write a key into a mapping-rooted
file (`scripts.yaml`, the helper files), so no shared item exists for them to
widen.

- Enforced by: `tests/test_templates.py::test_bare_item_gets_its_own_block`,
  `::test_two_bare_items_of_one_domain_do_not_share_a_block`,
  `::test_bare_item_create_only_appends_bytes`,
  `::test_full_block_create_only_appends_bytes`,
  `::test_bare_item_leaves_a_trigger_block_byte_identical`,
  `::test_create_falls_back_to_a_whole_file_write_on_a_layout_the_splice_cannot_cover`,
  and `tests/integration/test_live.py::TestTemplateBlockIsolation::test_a_created_entry_survives_a_poisoned_neighbouring_block`
  (HA restarted in place: the created entry comes up, the good neighbour of the
  bad entry does not)
## C-14 — A single-entry write rewrites only that entry's bytes

`POST`/`PUT`/`DELETE` on `/v1/config/{automation,script,template,helper}` change
one entry, so they change one entry's lines in the file and leave every other
byte exactly as it was. Comments, blank lines, line folding, indentation and
quote style elsewhere in the file survive verbatim; only the touched entry gets
tool-normalized formatting.

The defect this closes was not a misunderstood feature. All four route families
wrote their file back with a whole-document `yaml.dump(data, f)`, so one
confirmed automation write came back having reformatted ~34 unrelated real
automations (live-fire 2026-07-30, P1 #4) — semantically lossless, which is why
every value-level test in the suite stayed green, and still a defect: it
clobbers hand-maintained formatting and makes `git diff` on a config repo
useless. **The check is therefore on bytes, never on parsed values.**

`companion/surgical.py` performs the write as a line splice and *verifies it*:
the spliced text is re-parsed and compared against the tree the route meant to
write, and any disagreement — an unparseable result, an anchor whose definition
lived in the replaced entry, a layout the span arithmetic does not cover — falls
back to the whole-file dump. A fallback is never silent: the response carries
`reformatted: true` (absent otherwise, the same shape `reload_error` uses), so a
caller keeping its config in git is told the difference between "your entry
changed" and "the file was rewritten".

A route that rewrites a whole file by nature (`PUT /v1/config/file`, whose
caller supplies the content; `POST /v1/ref/replace`, which rewrites scalars
scattered across a whole `!include` graph — leaf-granular splicing is #88)
declares a `whole_file_reason` instead. The canary enforces the choice.

- Enforced by: `tests/test_invariants.py::test_single_entry_write_leaves_every_other_byte_alone`
  (sweeps every route declaring `surgical`, and carries an anti-vacuity guard
  that re-runs the old whole-file writer on the same input and requires it to
  have damaged lines outside the edited region — so a fixture that is already in
  ruamel's canonical form cannot make the sweep pass against either writer) and
  `::test_every_file_write_declares_a_formatting_stance` (canary);
  `tests/test_surgical.py` (byte-level behaviour: comments between entries,
  block scalars, unicode, CRLF, indented sequences, and each fallback still
  producing correct content); `tests/integration/test_live.py::TestSurgicalWrites`
  (a real HA loads the spliced file, and the same edit through HA's own
  `/api/config/automation/config/<id>` is shown re-serializing the whole file —
  the boundary of what this service can fix)

---

Client-side counterparts (retry idempotency, CLI confirm gate, vendored-spec
drift) live in the hactl repo's `INVARIANTS.md`.
