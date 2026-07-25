"""Small general-purpose utilities (no library-domain concepts).

Currently: `{param}` template expansion, and resolving the run directory
(including a per-platform temp default when the consumer specifies none).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any


def resolve_template(value: str, params: dict[str, Any], *, strict: bool = False) -> str:
    """Expand `{placeholders}` in a string from `params`.

    The single substitution helper used across the library so `{product_key}` etc.
    resolve the same way everywhere.

    strict=False (default): a missing placeholder is left literal (no KeyError),
      so an unrelated `{...}` in free text (prompt prose, a regex, an optional
      param) degrades gracefully rather than crashing the run. Used for inputs,
      instructions, context/agent-dir, and gate paths.

    strict=True: a missing placeholder raises KeyError. Use for values where a
      half-substituted result is always a bug — notably run_dir and other PATHS,
      which must fully resolve (a directory literally named "{product_key}" is
      never intended).
    """
    if not value:
        return value
    if strict:
        return value.format(**params)
    try:
        return value.format(**params)
    except KeyError, IndexError:
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
