# AGENTS.md

Norms for everyone working on hactl-companion — human or model.

> Filing a bug or a PR? Read [CONTRIBUTING.md](CONTRIBUTING.md) first. This file is about
> *working on the code*.

## Tests

`pytest` is the canonical test run. Run `make lint` (ruff) after changing code, fix, and test
again.

## Working Principles

**Plan before acting.** No change without a plan. Draft, review, then implement.

**Read before writing.** Read the concept, existing code, and tests first. No assumptions about
code you haven't seen.

**Done = green tests.** A feature without tests is unfinished. A milestone without passing tests
is not done.

**No speculative fixes.** Reproduce the bug first, then fix it. Guessing is not debugging.

**Security is not optional.** No secrets in the repo. Respect the Supervisor/HA permission model.

**Never use the `ha` CLI inside the container.** Mutations go through the core API / Supervisor
proxy and must reload HA after writes.

**Manage context.** Use subagents for long tasks. Use intermediate files to store knowledge.
