# Contributing to agent-flow

Thank you for considering contributing to agent-flow! This document outlines the
process for contributing and the conventions that keep the project consistent.

## Code of Conduct

By participating in this project, you agree to maintain a respectful and
inclusive environment for everyone.

## Getting Started

### Prerequisites

- Python 3.14+
- [`uv`](https://docs.astral.sh/uv/) package manager
- [`task`](https://taskfile.dev/) (go-task)
- For real (non-mock) runs: `opencode` on `PATH`, configured with model access

### Setting up the development environment

```bash
git clone <your-fork-url>
cd agent-flow
task install       # editable install with dev tools (ruff, pytest, pydantic, jsonschema, typer, rich, pyyaml)
```

## Development workflow

### Branch naming

Use descriptive branch names with prefixes:

- `feat/` — new features
- `fix/` — bug fixes
- `docs/` — documentation changes
- `refactor/` — code refactoring
- `test/` — tests only

Example: `feat/claude-code-runner`

### Coding standards

- Ruff for linting and formatting, rules `E, F, B, I, C90`, line length **150**.
- McCabe max complexity **10** — refactor complex functions into helpers.
- Type-annotate public functions.
- No bare `except Exception` — catch concrete types (note: `except A, B:` without
  `as` is valid Python 3.14 per PEP 758; do not "fix" it to parentheses).
- Keep the **core** dependency-light. Core deps (always installed): `anyio`,
  `pydantic`, `pydantic-settings`, `pyyaml`, `jsonschema`, `python-dotenv`,
  `loguru`. Optional extras:
  `[prefect]` (the Prefect backend) and `[cli]` (`typer` + `rich`). An optional
  dep must be **lazy-imported at its entry point**, never at module-import time —
  `prefect` only inside `backends/prefect.py`, `rich`/`typer` only inside `cli/`.
  Guard the entry-point import with `utils.require_extra(...)` so a missing extra
  fails with an actionable "install agent-flow[...]" message. The
  `test_prefect_isolation` guard proves core + runners + InProcessBackend import and
  run with `prefect` blocked.
- Respect the layering: the engine (Tier 3) must not import the runtime core
  (Tier 1); they meet only through a node's `run` callable (`node_builder/` is the
  one bridge). See `docs/design/index.md`.

### Testing

```bash
task test              # unit tests (fast, no subprocess)
task test:all          # unit + integration (mock subprocess; opencode e2e skipped)
task test:opencode     # opt-in real-opencode e2e (needs opencode + creds; run OUTSIDE an opencode session)
```

Run real-opencode examples/e2e from a normal shell **outside** an opencode
session — a nested opencode raises `UnknownError`.

### The local loop

```bash
task fct     # format + check (lint) + test — run before every commit
task verify  # CI gate: lint + format check, read-only (no fixes)
```

## Documentation

Design docs and consumer docs are [OKF](https://github.com/GoogleCloudPlatform/knowledge-catalog/blob/main/okf/SPEC.md)
bundles under `docs/`:

- `docs/usage/` — task-oriented consumer guides (start at `index.md`)
- `docs/design/` — architecture + one concept per file

When you change behavior, update the relevant doc and keep every OKF file's
`type` frontmatter intact. Verify code snippets actually run.

## Pull request process

1. Update docs and the README for any behavior/API change.
2. Run `task fct` — lint, format, and tests must pass.
3. Submit a PR with a clear description of the change.
4. Address review feedback.

## Commit messages

Short, clear, category-prefixed (conventional-commits style):

```
<type>(<optional scope>): <description>
```

Types: `feat`, `fix`, `docs`, `refactor`, `test`, `chore`.
Example: `feat(cli): add --config run-file support`.

## Reporting bugs

Include: a clear title, steps to reproduce, expected vs. actual behavior, and
environment details (OS, Python version, opencode version if relevant).

## Feature requests

Provide a clear description, the motivation, and any implementation thoughts.

## License

By contributing, you agree that your contributions will be licensed under the
Apache License 2.0 (see `LICENSE`).
