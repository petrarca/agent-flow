"""Small general-purpose utilities (no library-domain concepts).

Currently: `{param}` template expansion, resolving the run directory (including a
per-platform temp default when the consumer specifies none), and a friendly
optional-dependency guard.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any


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


def resolve_run_dir(run_dir: str | None, *, name: str = "run") -> Path:
    """Resolve a run_dir to an absolute Path, defaulting to a temp subdirectory.

    When run_dir is empty/None the caller specified nothing, so a unique
    directory is created under `<temp-base>/agent-flow/` (grouped and findable —
    never littering the cwd or a fixed 'work/'), where <temp-base> is
    `default_temp_base()`. The name + a UTC timestamp make it human-readable,
    e.g. /tmp/agent-flow/tech-assessment-20260723T131500Z-a1b2. Otherwise the
    given path is resolved.
    """
    import tempfile
    from datetime import datetime, timezone

    if run_dir:
        return Path(run_dir).resolve()
    base = default_temp_base() / "agent-flow"
    base.mkdir(parents=True, exist_ok=True)
    slug = "".join(c if (c.isalnum() or c in "-_") else "-" for c in name) or "run"
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return Path(tempfile.mkdtemp(prefix=f"{slug}-{stamp}-", dir=base))
