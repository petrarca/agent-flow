"""Small general-purpose utilities (no library-domain concepts).

Currently: `{param}` template expansion, resolving the run directory (including a
per-platform temp default when the consumer specifies none), the duration-name
lookup (over the vocabulary in `const`), and a friendly optional-dependency guard.

Depends only on the pure `const` leaf, so both the engine (tier 3) and the
runners (tier 1) may use these helpers without creating a cycle. The duration
NUMBERS live in `const`; the LOOKUP behaviour lives here.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from agent_flow.const import DEFAULT_DURATIONS

if TYPE_CHECKING:
    from upath import UPath


def duration_table(durations: dict[str, int] | None) -> dict[str, int]:
    """The run's duration vocabulary: the shipped names, overlaid by the run's own.

    Merged (not replaced) so a run config that retunes one name — `{long: 900}` —
    keeps `short`/`normal` working instead of silently emptying the vocabulary.
    """
    return {**DEFAULT_DURATIONS, **(durations or {})}


def resolve_duration(node: str, name: str, durations: dict[str, int] | None) -> int:
    """Map a declared duration NAME to seconds; an unknown name is a hard error.

    Deliberately not a fallback: a typo'd duration must fail loudly, naming the
    vocabulary it could have used. Silent degradation to a default is the exact
    failure mode the flow/run split exists to remove.
    """
    table = duration_table(durations)
    if name not in table:
        known = ", ".join(sorted(table))
        raise ValueError(f"node {node!r}: unknown duration {name!r} (known: {known}) — define it in the run config's `durations:` map")
    return int(table[name])


def require_extra(module: str, extra: str, feature: str):
    """Import `module`, or raise a clear 'install petrarca-agent-flow[extra]' error.

    Optional dependencies (prefect, typer, rich) are grouped into install extras.
    A consumer who uses a feature without its extra should get an actionable
    message, not a bare ModuleNotFoundError. Usage:

        typer = require_extra("typer", "cli", "the run_cli command")

    Returns the imported module.
    """
    import importlib

    try:
        return importlib.import_module(module)
    except ImportError as exc:
        raise ImportError(
            f"{feature} requires the optional '{module}' dependency. Install it with: pip install 'petrarca-agent-flow[{extra}]'"
        ) from exc


def resolve_template(value: str, params: dict[str, Any], *, strict: bool = False) -> str:
    """Expand `{placeholders}` in a string from `params`.

    The single substitution helper used across the library so `{product_key}` etc.
    resolve the same way everywhere.

    strict=False (default): the value is returned UNCHANGED whenever it cannot be
      fully expanded, so an unrelated `{...}` in free text degrades gracefully
      rather than crashing the run. Used for inputs, instructions, context /
      agent-dir, and gate paths — all of which routinely carry text the library
      did not author: prose, JSON/code fragments, regex quantifiers (`\\d{2,3}`),
      and an agent's own `reason` surfaced by a gate as a re-run instruction.
      Every str.format failure mode is treated as "not a template":
        - KeyError      — `{missing}`
        - IndexError    — `{}` / `{0}` with no positional args
        - ValueError    — an unbalanced or malformed brace (`a } b`, `{x:!!!}`)
        - AttributeError— `{p.nope}`
      (A single stray `}` in an agent's reason used to abort the whole run.)

    strict=True: any of those raises. Use for values where a half-substituted
      result is always a bug — notably run_dir and other PATHS, which must fully
      resolve (a directory literally named "{product_key}" is never intended).
    """
    if not value:
        return value
    if strict:
        return value.format(**params)
    try:
        return value.format(**params)
    except KeyError, IndexError, ValueError, AttributeError:
        return value


def default_temp_base() -> Path:
    """The conventional per-platform temp base for run dirs.

    macOS/Linux: `/tmp` (shallow and familiar — avoids macOS's deep per-user
    `/var/folders/.../T`). Windows: the OS temp dir (there is no `/tmp`). This is
    used only when the consumer does NOT pass an explicit run_dir. Temp by nature
    is EPHEMERAL (the OS may purge it) — pass an explicit run_dir for output you
    need to keep.
    """
    import sys
    import tempfile

    if sys.platform == "win32":
        return Path(tempfile.gettempdir())
    return Path("/tmp")  # POSIX: shallow, always present (macOS: symlink to /private/tmp)


def resolve_run_dir(run_dir: str | None, *, name: str = "run", in_memory: bool = False) -> Path | UPath:
    """Resolve a run_dir to an absolute path, defaulting to a temp subdirectory.

    Returns a `Path` for a local run_dir and a `UPath` for anything carrying a
    non-local scheme (`memory://…`, and any other fsspec protocol) — the two are
    interchangeable for the operations the library performs (`/`, `mkdir`,
    `write_text`/`read_text`, `exists`, `stat`), so callers need not branch.

    Resolution:
      - An EXPLICIT run_dir always wins. A `memory://…` (or other scheme) is kept
        as a UPath; a bare/local path is resolved to an absolute `Path`.
      - No run_dir + `in_memory=True` -> a unique `memory://run-<slug>-<uuid>/`
        root (hermetic, no disk). The mock path defaults here so a mock run is a
        unit test with no `tmp_path`.
      - No run_dir + `in_memory=False` -> a unique dir under `<temp>/agent-flow/`
        (grouped and findable, never the cwd), `name` + a UTC timestamp making it
        human-readable, e.g. /tmp/agent-flow/tech-assessment-20260723T131500Z-a1b2.
    """
    import tempfile
    import uuid
    from datetime import datetime, timezone

    # Local import: `utils` is a leaf every module pulls in, so importing upath
    # (and fsspec behind it) is deferred to the one function that needs it.
    from upath import UPath

    slug = "".join(c if (c.isalnum() or c in "-_") else "-" for c in name) or "run"

    if run_dir:
        u = UPath(run_dir)
        # A non-local scheme (notably memory://) stays a UPath; a bare/local path
        # resolves to an absolute Path like before.
        if u.protocol and u.protocol != "file":
            return u
        return Path(run_dir).resolve()

    if in_memory:
        # A unique netloc per run IS the isolation boundary: the fsspec memory
        # store is process-global, so distinct netlocs are distinct subtrees.
        return UPath(f"memory://{slug}-{uuid.uuid4().hex[:8]}")

    base = default_temp_base() / "agent-flow"
    base.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return Path(tempfile.mkdtemp(prefix=f"{slug}-{stamp}-", dir=base))


def find_marker_dir(marker: str, start: Path | None = None) -> Path | None:
    """Walk `start` (default cwd) and its ancestors for a directory named `marker`.

    Same instinct as git finding `.git`: the first ancestor that CONTAINS a
    `marker` directory is returned (the ancestor itself, not the marker). Returns
    None if none is found up to the filesystem root. Used by a runner to locate
    its agent-definitions convention (opencode's `.opencode/`) so the common case
    needs no explicit agent_dir.
    """
    here = (start or Path.cwd()).resolve()
    for directory in (here, *here.parents):
        if (directory / marker).is_dir():
            return directory
    return None
