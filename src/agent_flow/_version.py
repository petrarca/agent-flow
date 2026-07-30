"""The installed distribution version — its own leaf so nothing imports the facade.

`__version__` is re-exported from `agent_flow`, but a submodule that wants it
must not import the package root to get it: that is an upward dependency from a
layer to the facade above it, and it drags the whole library in for a string.
"""

from __future__ import annotations

DISTRIBUTION = "petrarca-agent-flow"


def resolve_version() -> str:
    """The installed distribution version (set by setuptools-scm at build time).

    Read from installed package metadata; falls back to "0+unknown" when the
    package is not installed (e.g. running from a bare source tree with no
    metadata). Single source for the `version` CLI command and any consumer.
    """
    from importlib.metadata import PackageNotFoundError, version

    try:
        return version(DISTRIBUTION)
    except PackageNotFoundError:
        return "0+unknown"


__version__ = resolve_version()
