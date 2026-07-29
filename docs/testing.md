# hactl-companion — Testing Guide

How hactl-companion is tested, what the tests prove, how to run them, and where the
gaps still are.

The companion is a small Python (aiohttp) sidecar that gives hactl YAML-level access to
a Home Assistant config directory — the things HA's REST/WS API cannot do. It touches
the filesystem, guards security-sensitive paths, and has to interoperate with a live HA.
So the suite is not only "does this endpoint work": most of it is machinery that makes a
rule hold for routes nobody has written yet.

---

## The shape of the suite

Three tiers, each with its own `make` target and its own CI job.

| Tier | Tests | Docker | Command | Time |
|---|---|---|---|---|
| Unit | **596** | no | `make test` | ~10s |
| Integration | **50** | yes (HA Core + companion) | `make test-int` | ~40s warm |
| WireGuard | **17** | yes (real WG server) | `make test-wg` | ~85s |

Counts are what the suite collects today, not a target.

---

## The part that matters: tests derived from the route table

The 2026-07 review found the companion's worst bugs — an auth spoof, write-gate holes —
were never a misunderstood feature. They were *route #N forgetting a rule that routes
#1..N-1 followed*. Example-based tests cannot catch that class, because the example for
the new route is the one nobody wrote.

So the cross-cutting rules are **quantified over `ENDPOINT_META`** — the same table that
generates the OpenAPI spec and that `test_openapi.py` proves complete against the
registered routes. Add a route, and it is covered by construction.

| Module | Tests | What it enforces |
|---|---|---|
| `test_invariants.py` | 133 | `INVARIANTS.md` C-1..C-12, every rule swept across the whole route table |
| `test_spec_conformance.py` | 49 | Field-level request/response contract, both directions (C-12) |
| `test_wiring.py` | 19 | C-10 — a create proves HA actually reads the file first |
| `test_openapi.py` | 17 | Spec is generated from code, complete, and validates |
| `test_versions.py` | 4 | One version across all four version-bearing files and the CHANGELOG |

The sharpest of these is a **classification canary**: a new *mutating* route fails the
suite until a human sorts it into `FILE_WRITES` (with a request probe) or
`SERVICE_ENDPOINTS` (with a stated reason). You cannot add a write path and quietly skip
the write rules — the failure is the point.

`INVARIANTS.md` at the repo root is the prose; this is the enforcement. Read them together.

---

## Layer 1: unit tests

`tests/`, everything except `tests/integration/`. They use `pytest-aiohttp`'s
`aiohttp_client` against a temp config dir — no Docker, no network, no HA.

```bash
make test     # uv run pytest tests/ --ignore=tests/integration -v --tb=short
```

Beyond the derived modules above, by concern:

| Concern | Modules | Tests |
|---|---|---|
| Routes & CRUD | `test_automations` `test_scripts` `test_templates` `test_helpers` `test_config` `test_config_write` `test_ha` `test_health` `test_status` `test_root` `test_logs` | 124 |
| WireGuard | `test_wireguard` `test_wg_dns` `test_wg_monitor` `test_wg_supervisor` | 110 |
| Reference scanning | `test_refscan` `test_refscan_routes` `test_refscan_skipped` `test_related` | 80 |
| Plumbing | `test_resolver` `test_core_api` `test_cli` `test_reload_error` `test_backups` `test_main` | 42 |
| Security | `test_auth` `test_pathguard` `test_paths` | 18 |

### Fixtures

`testdata/fixtures/` is copied into a fresh `tmp_path` per test, so write tests cannot
leak into each other:

```
configuration.yaml      # !include template.yaml + !include_dir_named packages
template.yaml           scripts.yaml           automations.yaml
counter.yaml            timer.yaml             input_boolean.yaml  input_number.yaml
home-assistant.log      packages/{energy,security}.yaml
```

`testdata/wireguard/` holds the WG server image and entrypoint used by the tunnel tier.

### Security is tested as a rule, not a case

Path traversal → 400, `secrets.yaml` (read/write/include/list) → 403, missing or invalid
token → 401, fail-closed when no token is configured, empty/invalid YAML refused before
anything is written. These are C-1, C-2, C-3 and C-8 in `test_invariants.py`, so they
apply to every route in the table rather than to the handful someone remembered.

---

## Layer 2: integration tests

`tests/integration/`, via `docker-compose.integration.yaml`: an official HA image and the
companion built from the local Dockerfile, sharing the `ha-config` volume on `ha-net`.
The companion sees HA's real `/config`, exactly as in production.

```bash
make test-int     # down -v → pytest → down -v, always torn down
```

`conftest.py` drives HA's onboarding headlessly — wait for `/api/onboarding`, create the
owner, exchange `auth_code` for a token, finish `core_config` and `analytics`, then mint a
long-lived token over WebSocket. Getting a token is itself the proof that HA is up and
`/config` is populated.

| Module | Tests | Covers |
|---|---|---|
| `test_live.py` | 43 | status, root, health, config read/write, `ref replace`, HA reload, automation/helper/script/template CRUD, startup + access logs, include wiring |
| `test_auth.py` | 5 | auth on a real container: no/wrong token → 401, health exempt, and a **client-supplied `X-Ingress-Path` does not bypass auth** — the 2026-07 spoof, pinned |
| `test_related.py` | 2 | the related-entity graph, reconciled against HA's own `search/related` (C-9) |

That last one is worth naming: the graph is not compared to a fixture we wrote, it is
compared to **HA's own answer**. A fixture only proves we still agree with ourselves.

---

## Layer 3: WireGuard

`make test-wg` uses a separate compose file to stand up a **real WireGuard server**
container and bring a real tunnel up — handshake, dyndns re-resolution, supervisor
restart behaviour. 17 tests, its own CI job, and a separate compose teardown.

---

## Gates and CI

`make lint` runs `check-markers` first, then `ruff check`, `ruff format --check`, and
`mypy`.

**`check-markers` is the spec-before-code gate.** A `[NEEDS ORACLE: ...]` marker records
an assumption about HA that has not been checked against a live instance. Markers are
fine on a branch; they may not merge. You clear one by probing a real HA and deleting it —
not by deleting the doubt. The ordering ritual itself lives in the workspace `AGENTS.md`.

CI (`.github/workflows/ci.yml`) runs on every push to `main` and every PR into it:

| Job | What it does |
|---|---|
| Lint | markers + ruff + format check + mypy |
| Unit Tests | the 596 |
| OpenAPI Contract | regenerates the spec from code and diffs it against the committed file |
| Docker Build | the image still builds |
| Integration Tests | the Docker tier, matrixed over HA **stable** and **prev** |
| WireGuard Integration | the tunnel tier |
| All Gates Green | aggregator — verifies every job above actually succeeded |

Plus CodeQL (`codeql.yml`) contributing `CodeQL` and `Analyze`.

**Branch protection on `main`** requires `All Gates Green`, `CodeQL` and `Analyze`, with
`strict: true` (branch must be current) and **`enforce_admins: true`** — direct pushes to
`main` do not work for anyone. Every change goes through a PR whose checks pass.

The spec is **generated**: edit `companion/openapi.py` and run `make spec`. Never
hand-edit `openapi/companion-v1.yaml` — `test_openapi.py` and the CI job both enforce it,
and hactl vendors a pinned copy of that file.

---

## Running locally

| Goal | Command | Docker | Time |
|---|---|---|---|
| Quick check | `make test` | no | ~10s |
| Lint + types + markers | `make lint` | no | ~5s |
| Integration suite | `make test-int` | yes | ~40s warm |
| WireGuard suite | `make test-wg` | yes | ~85s |
| Regenerate spec | `make spec` | no | ~1s |
| Format | `make fmt` | no | ~1s |
| Tear down strays | `make clean` | yes | — |

Prerequisites: Python 3.12+ and `uv`; Docker for the two container tiers.

**Troubleshooting**

- *`aiohttp_client` fixture missing* — `uv sync --extra dev`.
- *First integration run is slow* — it pulls the HA image (~1 GB). Pre-pull with
  `docker pull ghcr.io/home-assistant/home-assistant:stable`.
- *Compose fails instantly* — check `docker info` first; a stopped daemon reports as a
  build failure, not as "Docker is off".
- *Strays after an interrupted run* — `make clean` tears down both compose files.

---

## Honest gaps

Stated so this page is not read as exhaustive-by-omission.

- **No real Supervisor.** The stack is HA Core plus the companion; `conftest.py` says so
  outright and hands the companion a synthetic `SUPERVISOR_TOKEN`. Supervisor-proxy calls
  are exercised against Core directly, so add-on-install and Ingress specifics are proven
  only at the unit level.
- **No HA `dev` leg.** The matrix is `stable` and `prev`. hactl tests against `dev` too,
  so a breaking Core change lands there first, not here.
- **Scale is untested.** Fixtures are small. A config with hundreds of automations, or a
  very large single YAML, has no coverage — neither for correctness nor for time.
- **Backups are not atomic.** Concurrent writes are now tested for *corruption*
  (`test_concurrent_writes_no_corruption`, last-write-wins cleanly), but the
  backup-then-write sequence is still not a transaction.

Closed since this page was last honest (2026-05-18), recorded so nobody re-opens them:
concurrent writes are covered; every `!include_dir_*` variant is implemented and an
unknown tag is a hard error rather than a shrug (C-11); the `ha` CLI is gone from the
write path entirely — config validation goes through `POST /config/core/check_config` on
the core API.

---

## Quick reference

```bash
make test        # 596 unit tests, no Docker
make lint        # markers + ruff + format + mypy
make fmt         # auto-format
make spec        # regenerate the OpenAPI spec from code

make test-int    # 50 integration tests, HA Core + companion
make test-wg     # 17 WireGuard tests, real tunnel
make clean       # tear down both compose stacks
```

CI: Lint · Unit · OpenAPI Contract · Docker Build · Integration (stable, prev) ·
WireGuard · All Gates Green — plus CodeQL and Analyze.
