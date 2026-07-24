"""Environment loading for the orchestrator.

The runner spawns opencode (and opencode spawns its MCP servers) as
subprocesses. Those inherit the Python process's `os.environ`. A bare
`source .env` in a shell only sets *shell* variables — it does NOT export them
into the process environment — so subprocesses would not see them.

The reliable fix: the library itself loads a `.env` file into `os.environ` at
startup via python-dotenv. From then on every subprocess (and its MCP-server
children) inherits those variables automatically through normal Popen
inheritance. No `export`, no `set -a` required.

Precedence: variables already present in the real environment (exported in the
shell or set by CI) are NOT overwritten by the file (`override=False`), so
explicit env wins over the file — the correct precedence.
"""

from __future__ import annotations

import os
from pathlib import Path


def load_env(env_file: str | os.PathLike | None = ".env") -> list[str]:
    """Load an env file into os.environ (real env wins; file does not override).

    Args:
        env_file: path to the env file. Default ".env" in the current directory.
            If None or the file does not exist, this is a no-op.

    Returns:
        The names of variables the file contributed (i.e. keys it set that were
        not already in os.environ) — useful for logging what the run picked up.
    """
    if env_file is None:
        return []
    path = Path(env_file)
    if not path.exists():
        return []

    from dotenv import dotenv_values

    added: list[str] = []
    for key, value in dotenv_values(path).items():
        if value is None:
            continue
        if key not in os.environ:  # real env / already-set wins
            os.environ[key] = value
            added.append(key)
    return added
