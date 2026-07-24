"""Run configuration — a unified way to supply run settings + params.

A pipeline run needs two kinds of input:

  - GENERIC run settings the library understands: runtime, run_dir, agent_dir,
    instructions, llm_concurrency, show_events.
  - DOMAIN params (arbitrary, consumer-defined): e.g. product_key, repos_root —
    the library attaches NO meaning to these; they are threaded to
    pipeline(**params) and used for {name} templating in inputs/context/paths.

Both can come from a YAML config file (generic keys at top level, a `params:`
section for the domain values) OR from CLI flags. This module is the pure core:
loading/merging config. The reusable Typer command lives in cli.run_cli.

Example config (run.yml):

    runtime: opencode
    run_dir: "{repos_root}/{product_key}/output"
    agent_dir: /work/pipelines/tech-assessment
    llm_concurrency: 2
    instructions: |
      Experimental code-graph support is available; use it alongside RAG.
    params:
      product_key: my-product
      repos_root: /tmp/repos

There is deliberately no "product" (or any domain) concept here — `params` is an
open bag.
"""

from __future__ import annotations

from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any

# The generic run settings the library knows (everything else in a config file's
# top level that isn't one of these, and isn't `params`, is rejected as a typo).
_GENERIC_KEYS = ("runtime", "run_dir", "agent_dir", "instructions", "instructions_file", "llm_concurrency", "show_events")


@dataclass
class RunConfig:
    """Resolved run configuration: generic settings + an open `params` bag."""

    runtime: str = "opencode"
    run_dir: str = ""  # empty -> a fresh temp dir is created at run time
    agent_dir: str = ""
    instructions: str = ""
    instructions_file: str = ""
    llm_concurrency: int | None = None
    show_events: bool = False
    params: dict[str, Any] = field(default_factory=dict)

    def resolved_instructions(self) -> str:
        """The run-wide brief: instructions_file content if given, else instructions."""
        if self.instructions_file:
            return Path(self.instructions_file).read_text()
        return self.instructions


def load_run_config(path: str | Path) -> RunConfig:
    """Load a RunConfig from a YAML file (generic keys + a `params:` section).

    Unknown top-level keys (not a generic key and not `params`) raise ValueError —
    a mis-typed setting should fail loudly, not be silently ignored.
    """
    import yaml

    data = yaml.safe_load(Path(path).read_text()) or {}
    if not isinstance(data, dict):
        raise ValueError(f"run config {path} must be a mapping at the top level")
    return _from_dict(data, source=str(path))


def _from_dict(data: dict[str, Any], *, source: str) -> RunConfig:
    params = data.get("params", {}) or {}
    if not isinstance(params, dict):
        raise ValueError(f"{source}: `params` must be a mapping")
    unknown = set(data) - set(_GENERIC_KEYS) - {"params"}
    if unknown:
        raise ValueError(f"{source}: unknown config keys {sorted(unknown)} (allowed: {list(_GENERIC_KEYS)} + params)")
    known = {k: data[k] for k in _GENERIC_KEYS if k in data}
    return RunConfig(params=dict(params), **known)


def parse_params(items: list[str] | None) -> dict[str, str]:
    """Parse repeatable `KEY=VALUE` CLI strings into a params dict.

    Values stay strings (they feed {name} templating). `KEY=` yields "". A
    missing `=` raises ValueError (so `--param foo` fails loudly).
    """
    out: dict[str, str] = {}
    for item in items or []:
        if "=" not in item:
            raise ValueError(f"--param must be KEY=VALUE, got {item!r}")
        key, _, value = item.partition("=")
        key = key.strip()
        if not key:
            raise ValueError(f"--param has empty key: {item!r}")
        out[key] = value
    return out


def merge(base: RunConfig, *, cli_overrides: dict[str, Any], cli_params: dict[str, str]) -> RunConfig:
    """Merge CLI over a file-loaded config. Precedence: CLI > file > default.

    cli_overrides: generic settings explicitly set on the CLI (None = not set, so
        the file/default wins). cli_params: --param KEY=VALUE (override file params
        per key).
    """
    merged = RunConfig(**{f.name: getattr(base, f.name) for f in fields(base)})
    merged.params = {**base.params, **cli_params}
    for key, val in cli_overrides.items():
        if val is not None:
            setattr(merged, key, val)
    return merged
