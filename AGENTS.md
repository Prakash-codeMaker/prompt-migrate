# AGENTS.md — AI coding agent instructions for PromptMigrate

**This file is the source of truth for AI agent instructions.** The rules are mandatory for every AI coding agent (Claude Code, Codex, Cursor, Copilot, Gemini, Hermes or any other) and every change. The tool-specific files `CLAUDE.md`, `CODEX.md`, `GEMINI.md`, `HERMES.md` and `.github/copilot-instructions.md` only point to this file (plus tool-specific orientation such as where plans are saved) and never add to or override a rule; to change a rule, edit this file.

**PromptMigrate** is an open-source, Alembic-inspired, local-first, stdlib-only Python (≥ 3.10) CLI for **AI prompt and agent configuration migrations**: safe ordered revisions, environment state tracking, reversible changes, integrity/drift checks, and eventually policy-gated immutable release activation. **Do not redefine it as** a prompt editor, LLM workflow orchestrator, SaaS dashboard, observability tool or generic AI agent system.

## Critical rules

Breaking one of these does the most damage. Details are under Invariants.

- Fail closed on drift, corrupt history, unexpected state or partial writes; never accept them silently.
- Never change the revision format or checksum semantics without a new explicit format version.
- Never commit a secret, credential or token-shaped literal.
- The runtime stays stdlib-only.

## Setup and commands

Run from the repository root. `pyproject.toml` sets `pythonpath = ["src"]` for pytest, so the editable install is optional for tests.

```bash
python -m pip install -e ".[dev]"                        # dev install: pytest, hypothesis, pytest-cov, mypy
python -m pytest -q                                      # whole suite (required before every commit)
python -m pytest tests/unit                              # one suite: unit, integration, contract, security
python -m pytest tests/test_core.py::PromptMigrateTests::test_state_drift_detected   # one test
python -m pytest -k restamp                              # tests matching a name
python -m pytest --cov=promptmigrate                     # coverage (local only, no percentage gate)
python -m mypy src                                       # lenient static type check (config in pyproject.toml)
PYTHONPATH=src python -m unittest discover -s tests -v   # the original 7 unittest tests
PYTHONPATH=src python -m promptmigrate --project <dir> history   # CLI from source
docker build -f Dockerfile.ci --build-arg PYTHON_VERSION=3.12 -t promptmigrate-ci . && docker run --rm promptmigrate-ci   # Linux CI job, locally
```

- PowerShell: set `$env:PYTHONPATH = 'src'` instead of the `PYTHONPATH=src` prefix.
- WSL or no usable `python`: prefix commands with `uv run --extra dev`, e.g. `uv run --extra dev python -m pytest -q`.
- mypy is the only static checker (#20). No linter or formatter is configured yet (issues #19 and #37). Don't add one outside those issues.

## Repository structure

- `src/promptmigrate/` — the package (ADR-022). Modules, by layer (`docs/LOW_LEVEL_DESIGN.md` §1):
  - `protocol/` — limits, strict JSON, `canonical`/`same`, name rules
  - `secretscan.py` — secret rules, allowlist, redaction; no I/O
  - `project.py` — workspace, drafts
  - `revisions.py` — parse, graph, replay; `store/` — environments, lock, SQLite
  - `draftdiff.py`; `plan.py` — target resolution for `upgrade`/`downgrade`/`plan`; `apply.py` — `_verify`, row+ledger writes; `output.py` — `Result`, text/JSON, exit codes
  - `commands/<verb>.py` — one module per CLI verb; `cli.py` — parse and dispatch
  - `core.py` — re-export facade; `errors.py` — typed errors
- `tests/` — suites `unit`, `integration`, `contract`, `security` (map in `docs/TESTING_STRATEGY.md`); fixtures in `tests/conftest.py`, helpers in `tests/pmtest.py`.
- `docs/` — start with `docs/HANDOFF.md`. Scope and status: `ROADMAP.md`. Decisions: `DECISIONS.md`. Open questions: `OPEN_QUESTIONS.md`.

Project state lives under `.promptmigrate/`:

- `drafts/<name>.json` — desired config.
- `versions/<12hex>_<slug>.json` — revisions; `changes: {name: {before, after}}` as full snapshots, `null` = absent.
- `environments.json` — declared envs, Git tracked; legacy projects are adopted on first mutation.
- `states/<env>.sqlite3` — gitignored; `applied_revisions` ledger + `prompts`; missing = base, incomplete = error. Runtime state is not implemented.

## Architecture boundaries

1. Imports only point down the layer order above; `tests/unit/test_module_layout.py` enforces it.
2. `load_revisions()` enforces one linear chain and replays it from `{}`.
3. `revision()` diffs drafts against the replayed source head, **never an env DB**.
4. `upgrade`/`downgrade` run `BEGIN IMMEDIATE` → `_verify()` (the ledger is an exact (id, checksum) prefix of history and `prompts` equals `replay(prefix)`, else drift) → `_apply_changes()` re-checks preconditions → rows and ledger commit together.
5. Checksums: v0 files (no `format_version`) hash raw bytes; v1 files hash `"v1:" + sha256(canonical(item))` (ADR-026). `restamp` re-stamps only v0 ledger rows whose file changed in line endings alone (ADR-027).
6. Downgrade deletes ledger rows; there is no audit log yet.

## Invariants

- Keep source history and applied env state distinct; fail closed on drift, corrupt history, unexpected state or partial writes.
- Config and ledger change in one local SQLite transaction, with no LLM/provider calls inside it. No remote writes disguised as transactional migration.
- Revisions are JSON only; no executable revision code.
- Never change the revision format or checksum semantics without an explicit format version. Applied revision integrity is versioned: for v0, any byte change (including whitespace or CRLF) blocks that env; for v1, formatting-only changes are allowed, but any change to the canonical parsed content blocks that env.
- `--env prod` is a local SQLite file; never claim real production deployment or production readiness from a green suite.
- No secrets, credentials or user data in revisions, logs or fixtures. Tests build token-shaped values at runtime from fragments (`"sk-" + "0" * 24`); never commit a token-shaped literal.
- Runtime stays stdlib-only; dev deps go in `[project.optional-dependencies] dev`. No AI-orchestration frameworks in core.
- `check` is an integrity check, not a prompt-behavior evaluation. Downgrade reverts configuration only, not external agent actions. Future activation = immutable release manifest + atomic pointer switch; never claim distributed ACID across remote APIs.
- No wholesale rewrites without regression coverage.

## Coding standards

Match the surrounding code. These conventions are followed across `src/`:

- `core.py` re-exports and defines nothing; keep its exported names and signatures stable.
- `cli.py` only parses and dispatches; each verb lives in `commands/<verb>.py` and returns an `output.Result`.
- Every module starts with a one-line docstring saying what it owns; modules with annotations add `from __future__ import annotations`. Public functions get a one-line docstring and full type hints.
- Import within the package by module (`from . import project, revisions`) and call through it (`project.read_drafts(...)`). Import names directly only when a test needs to patch them, and say so in a comment.
- Private helpers use a leading underscore (`_verify`). A trailing underscore avoids shadowing a module name (`revisions_`).
- User-facing failures raise a typed error from `errors.py` (a `MigrationError` subclass, mapped to an exit code in `output.py`); warnings subclass `PromptMigrateWarning`. Never raise bare `ValueError`/`RuntimeError` for them, and never `print` outside `output.py` and `cli.py`.
- Double quotes and f-strings; keep lines within about 120 characters.
- Comments explain *why* (an invariant, a platform quirk, a test hook), not *what*. Keep them sparse.

## Testing standards

- **Iron Law:** no production code without a watched-failing test. RED: write a minimal test and see it fail for the expected reason. GREEN: minimal code to pass. REFACTOR: keep it green, then run the **whole** suite. A test written after the code that passes at once proves nothing: delete the code and restart.
- Each PR starts with its full list of failing/characterization tests; earlier PRs' tests stay green. Known unfixed bugs are `@pytest.mark.xfail(strict=True)` with a reason naming the fixing PR.
- Use the `project` fixture and the `tests/pmtest.py` helpers, put each test in the suite folder it belongs to, and name it after the behavior it checks (`test_check_does_not_create_files`).
- In tests, patch the defining module, never `core` or `cli`.
- Tools: `pytest`, `hypothesis`, `pytest-cov`. The original 7 `unittest` tests in `tests/test_core.py` stay runnable unmodified. Coverage is measured locally with no percentage gate and never quoted as invented numbers.

## How to work

- **Discussing:** restate the goal, find architecture gaps and differentiators, critique assumptions with alternatives and trade-offs, propose ADRs and milestones. Change no code until asked.
- **Implementing:** inspect the repo and state the test baseline; pick a small scope from `docs/IMPLEMENTATION_PLAN.md` or the active spec; follow the Iron Law; report **actual** test results only; keep shipped and planned behavior clearly separate.
- **Reviews** use: **Verified / Planned / Risks–contradictions (severity) / Proposed ADR updates / Next 3 steps with test acceptance criteria.**

Working style, adapted from the Karpathy guidelines (`andrej-karpathy-skills`, MIT). For trivial tasks, use judgment.

- **Think before coding.** State your assumptions. If a request has several readings, list them instead of picking one silently. If something is unclear, stop and ask; if a simpler approach exists, say so.
- **Simplicity first.** Write the least code that solves the task: no unrequested features, configurability, single-use abstractions, or error handling for impossible cases.
- **Surgical changes.** Every changed line traces to the task. Don't reformat, refactor or "improve" adjacent code; mention unrelated dead code instead of deleting it. Remove only the imports, variables and functions your own change made unused.
- **Goal-driven execution.** Turn the task into a check you can verify (usually a failing test). For multi-step work, state a short plan with a check for each step, and loop until every check passes.

## Commits and pull requests

Before every commit:

- `CHANGELOG.md` ([Keep a Changelog](https://keepachangelog.com/en/1.0.0/)): add one line under `## [Unreleased]`, in the group `Added`, `Changed`, `Fixed`, `Removed`, `Testing`, `Documentation` or `Internal`, as `- **Name** — description (#N)`. Mark a CLI or file contract change **Contract:**.
- `docs/ROADMAP.md`: tick finished items and update the status lines and "First next step".
- Behavior changes also update `docs/CLI_SPEC.md`, `docs/MIGRATION_PROTOCOL.md` and `docs/DECISIONS.md` in the same PR.

Issues:

- One task per issue and one issue per PR. Link it in the PR body with `Closes #N`. If the work turns out to be two tasks, split the issue instead of widening the PR.
- Issues carry a `phase-N` label and milestone. Newcomers start from `good first issue`.
- `rfc` issues and Phase 2–3 issues need an accepted ADR in `docs/DECISIONS.md` before any implementation code.

Git:

- Branches: `<type>/<short-description>` (feat, fix, refactor, docs, test, chore, perf, build, ci).
- Conventional Commits, authored as the repository owner (repo-local git config). No AI attribution in commits or PRs: no AI `Co-Authored-By`, no "Generated by".
- `.gitattributes` forces LF; byte-exact fixtures need a `-text` rule.
- `docs/superpowers/` and `graphify-out/` are local-only; never commit or link to them.
- Changes reach `main` only through a GitHub pull request that passes the merge gate, merged with a merge commit; nobody pushes to `main` directly. AI agents don't push or open PRs unless asked.
- External contributors fork the repository and open a PR against `main`, filling in the PR template.

**Merge gate:** a PR merges only when all of these hold:

1. The `ci-pass` check of `.github/workflows/ci.yml` is green. It passes only when every PR job passed: the mypy `type-check` on Python 3.10, 3.12 and 3.13, Linux (Docker) on Python 3.10, 3.12 and 3.13 with the package smoke test, Windows on 3.10 and 3.13, and macOS on 3.13. The remaining Windows/macOS legs run weekly and on manual dispatch; a red weekly run is fixed before the next merge. A red or skipped job blocks the merge, and flaky tests are fixed, not re-run until they pass.
2. The branch is up to date with `main`, so CI ran against the code that will land.
3. Every review thread is resolved. A maintainer reviews each outside contribution before merging it. GitHub requires no approval while there is a single maintainer, because GitHub does not let an author approve their own PR; raise the required approvals to 1 when a second maintainer joins.
4. The PR description includes the test command, its real output and the OS/Python version it was run on. Claims about a platform are backed by that platform's CI job, never by a local run on another OS.

## Boundaries

- **Ask first:** changing the revision format, checksum semantics or state layout; adding a dev dependency (runtime dependencies are never allowed); cross-module refactors or rewrites; deleting or rewriting env state, revisions or ledger rows; anything that touches a remote (push, PR, issue, release).
- **Never:** commit secrets or token-shaped literals; hand-edit an applied revision to make a check pass; weaken a drift or integrity check to get a test green; skip hooks or force-push `main`; add AI attribution to commits or PRs.

## Current status (verified 2026-11-16)

- Shipped: `init`, `revision`, `history`, `diff`, `current`, `upgrade`, `downgrade`, `plan`, `check`, `restamp`, `validate`, `get`, `env add`, `env list`; `--output json`; exit codes 0–5.
- Windows 11 / Python 3.11.9: `python -m pytest -q` → `930 passed, 21 skipped, 33 warnings` (host symlink, case-insensitivity and POSIX-permission skips); unittest → `Ran 7 tests ... OK`. CI: Linux Docker on Python 3.10/3.12/3.13, native Windows 3.10/3.13 and macOS 3.13 on every PR and `main` push; the full Windows/macOS 3×3 matrix weekly.
- Not shipped (never claim otherwise): `eval`, `export`, `promote`, `reconcile`, cloud adapters, SDK, Postgres, RBAC, signed revisions, real production activation.

## References

- [`README.md`](README.md), [`CONTRIBUTING.md`](CONTRIBUTING.md), [`SECURITY.md`](SECURITY.md)
- [`docs/HANDOFF.md`](docs/HANDOFF.md) — design docs index
- [`docs/LOW_LEVEL_DESIGN.md`](docs/LOW_LEVEL_DESIGN.md) — module layers and contracts
- [`docs/MIGRATION_PROTOCOL.md`](docs/MIGRATION_PROTOCOL.md), [`docs/CLI_SPEC.md`](docs/CLI_SPEC.md)
- [`docs/DECISIONS.md`](docs/DECISIONS.md) — ADRs
- [`docs/TESTING_STRATEGY.md`](docs/TESTING_STRATEGY.md)
- [`docs/ROADMAP.md`](docs/ROADMAP.md), [`docs/IMPLEMENTATION_PLAN.md`](docs/IMPLEMENTATION_PLAN.md)
