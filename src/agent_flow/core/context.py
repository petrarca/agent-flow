"""Context ingestion — read named files and inject their CONTENT into a prompt.

The failure mode this solves: telling an agent to "read the security rules"
does not reliably make it read them. Injecting the rules' *content* directly
into the prompt does. So a consumer names context sources (rules, standards,
`AGENTS.md`-style files) and the engine reads them and concatenates their
content into the prompt as delimited blocks — the agent physically has the
rules in front of it, not a pointer to go fetch.

This produces a plain string block that is concatenated alongside the inline
instruction strings (run_instructions / per-node instructions). It is
additive: files and inline text end up in the same prompt.

Sources may be file paths or globs, and may template run params via `{name}`
(e.g. "{run_dir}/rules/security.md", "rules/{product_key}.md"). A source that
resolves to nothing (missing file, empty glob) is warned about and skipped —
never a crash — since a rules file being absent should degrade, not abort.
"""

from __future__ import annotations

import glob
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any


def read_context_blocks(
    sources: Iterable[str] | None,
    *,
    params: dict[str, Any],
    run_dir: Path,
    warn: Callable[[str], None] = lambda _m: None,
) -> str:
    """Read each source (path or glob) and return one delimited content block.

    Args:
        sources: file paths or globs; each may use `{name}` templates resolved
            against params (plus `{run_dir}`). None or empty -> "".
        params: run params for templating.
        run_dir: run directory; exposed as `{run_dir}` and used to resolve
            relative source paths.
        warn: called with a message for a source that matched no file.

    Returns:
        The concatenated blocks (each headed by its source path), or "" if there
        was nothing to inject.
    """
    if not sources:
        return ""
    tmpl = {**params, "run_dir": str(run_dir)}
    blocks: list[str] = []
    for src in sources:
        resolved = _template(src, tmpl)
        paths = _expand(resolved, run_dir)
        if not paths:
            warn(f"context source matched no file: {resolved}")
            continue
        for p in paths:
            try:
                text = p.read_text()
            except OSError as exc:
                warn(f"context source unreadable ({p}): {exc}")
                continue
            blocks.append(f"### Context: {p.name}\n\n{text.strip()}")
    return "\n\n".join(blocks)


def _template(src: str, tmpl: dict[str, Any]) -> str:
    try:
        return src.format(**tmpl)
    except KeyError, IndexError:
        return src


def _expand(resolved: str, run_dir: Path) -> list[Path]:
    """Expand a (possibly relative, possibly glob) source to concrete files.

    Relative sources resolve against run_dir. Globs are supported; a plain path
    is returned only if it exists. Results are sorted for deterministic order.
    """
    base = resolved if Path(resolved).is_absolute() else str(run_dir / resolved)
    if any(ch in resolved for ch in "*?["):
        return sorted(Path(m) for m in glob.glob(base) if Path(m).is_file())
    p = Path(base)
    return [p] if p.is_file() else []
