"""Architecture fitness functions: the layering is a test, not a convention.

The contracts themselves live in `pyproject.toml` under `[tool.importlinter]`
— layer order, the tier rule, the leaf packages, and acyclicity. This module
runs them, so a violation fails in `task fct` (the normal loop) rather than
after a push, and adds the one guard import-linter has no equivalent for.

Why import-linter rather than a hand-rolled graph walk. Its engine, grimp, sees
FUNCTION-LOCAL imports as well as module-level ones. That distinction is not
academic here: `test_prefect_isolation.py::test_no_eager_import_cycles` inspects
module-level imports only — correct for what it claims, since only those can
deadlock at package init — and three package cycles hid behind deferred imports
where it could not see them. A cycle broken by a function-local import does not
deadlock, but it is still two modules that cannot be changed independently.

Why in the test suite rather than a separate `task verify` step. Cycles and
layering are "atomic, triggered" fitness functions in the Building Evolutionary
Architectures taxonomy — the category that maps to unit tests — and ArchUnit,
the reference implementation of the idea, is deliberately JUnit tests rather
than a linter so violations surface in the developer's own loop. The usual
argument for splitting architecture analysis into its own stage is cost; here
the whole check is about 0.1s against a 15s suite, so that argument does not
apply.
"""

from __future__ import annotations

import ast
import pathlib
import subprocess
import sys

import pytest

pytestmark = pytest.mark.unit

REPO = pathlib.Path(__file__).resolve().parents[2]
SRC = REPO / "src" / "agent_flow"

# Every top-level unit must appear in the `layers` contract in pyproject.toml.
# import-linter checks the modules it is TOLD about; it cannot know that a newly
# added package was never placed. This is the guard for that: adding a top-level
# package fails here until someone decides where it belongs.
LAYERED_UNITS = {
    "cli",
    "flowdef",
    "engine",
    "registry",
    "node_builder",
    "core",
    "preflight",
    "runners",
    "backends",
    "flow_types",
    "run_config",
    "utils",
    "protocol",
    "gates",
    "const",
    "run_context",
    "errors",
}
# Deliberately outside the layer graph: the facade sits above everything, and
# these two are leaves that no contract needs to constrain.
UNLAYERED = {"__init__", "_version", "logging_setup"}


def test_import_contracts():
    """Run the contracts declared in pyproject.toml.

    On failure import-linter prints the offending import chain with file and
    line, so the assertion message is its own report rather than a summary.

    Invoked as a SUBPROCESS through the console script, for three reasons.

    `python -m importlinter.cli` exits 0 while printing nothing — the package has
    no `__main__` — so a test written that way passes forever without checking
    anything. The two assertions below make that class of silent pass impossible:
    the entry point must exist, and the run must actually report on contracts.

    The in-process alternative, `importlinter.cli.lint_imports()`, returns an int
    rather than exiting and is what the commonly-cited blog post uses, but it
    does `sys.path.insert(0, os.getcwd())` and never undoes it. That is correct
    for a CLI process and unwanted in a pytest process that keeps running.

    And `importlinter/api.py` — the module headed "public-facing Python
    functions" — exposes only `read_configuration`. The supported contract is the
    CLI, so that is what this depends on.
    """
    lint_imports = pathlib.Path(sys.executable).parent / "lint-imports"
    assert lint_imports.exists(), f"import-linter is not installed ({lint_imports}) — run `task install`"

    result = subprocess.run([str(lint_imports), "--no-cache"], cwd=REPO, capture_output=True, text=True)
    assert "Contracts:" in result.stdout, f"import-linter did not report on any contract:\n\n{result.stdout}\n{result.stderr}"
    assert result.returncode == 0, f"import-linter contracts broken:\n\n{result.stdout}\n{result.stderr}"


def test_every_top_level_unit_is_placed_in_a_layer():
    """A new top-level package must be placed in the layer contract deliberately.

    import-linter validates the modules its contracts name; nothing tells it a
    package exists but was never mentioned. Without this, a new top-level
    package would silently sit outside the architecture.
    """
    units = {"__init__" if p.name == "__init__.py" and p.parent == SRC else p.relative_to(SRC).parts[0] for p in SRC.rglob("*.py")}
    units = {u[:-3] if u.endswith(".py") else u for u in units}
    unplaced = units - LAYERED_UNITS - UNLAYERED
    assert not unplaced, f"top-level units missing from the layers contract in pyproject.toml: {sorted(unplaced)} — decide where they belong"


def test_contract_names_match_this_module():
    """The layer contract in pyproject.toml must cover exactly LAYERED_UNITS.

    Keeps the guard above honest: if someone adds a package to the contract but
    not here (or the reverse), the two drift and the guard stops guarding.
    """
    text = (REPO / "pyproject.toml").read_text()
    block = text.split('name = "Layered architecture"', 1)[1].split("[[tool.importlinter.contracts]]", 1)[0]
    named = {
        part.strip().removeprefix("agent_flow.")
        for line in block.splitlines()
        if '"agent_flow.' in line
        for part in line.strip().strip('",').split("|")
    }
    assert named == LAYERED_UNITS, (
        f"layer contract and LAYERED_UNITS disagree: only in contract {sorted(named - LAYERED_UNITS)}, only here {sorted(LAYERED_UNITS - named)}"
    )


# The two places that import by computed name. Both are indirection over a
# statically declared set, checked below, so the contracts still see every edge.
DYNAMIC_IMPORT_SITES = {
    "__init__.py": "the PEP 562 lazy facade — targets come from the _EXPORTS table",
    "utils.py": "require_extra() — guards an optional extra, called with literals",
}


def test_dynamic_imports_do_not_hide_dependencies():
    """Every import-by-computed-name resolves over a statically declared set.

    Static analysis is only as good as the imports it can see, so a computed
    import is a hole in the contracts above. Two exist, and neither is a hole:
    the facade resolves against a literal table, and `require_extra` is called
    with a literal at every site. This test fails on a third one, and on either
    of these two losing the property that makes it safe.
    """
    unexpected = []
    for path in SRC.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            if not isinstance(node, ast.Call):
                continue
            target = getattr(node.func, "attr", None) or getattr(node.func, "id", None)
            if target in {"import_module", "__import__"} and node.args and not isinstance(node.args[0], ast.Constant):
                if path.name not in DYNAMIC_IMPORT_SITES or path.parent not in (SRC,):
                    unexpected.append(f"{path.relative_to(REPO)}:{node.lineno}")
    assert not unexpected, "import by computed name escapes the architecture contracts: " + ", ".join(unexpected)

    # the facade's table must be entirely literal -> every target is knowable
    facade = ast.parse((SRC / "__init__.py").read_text())
    exports = next(n.value for n in ast.walk(facade) if isinstance(n, ast.AnnAssign) and getattr(n.target, "id", "") == "_EXPORTS")
    assert isinstance(exports, ast.Dict) and all(isinstance(v, ast.Constant) for v in exports.values), (
        "the facade's _EXPORTS table must map to literal module names"
    )

    # require_extra must be called with a literal module name everywhere
    non_literal = []
    for path in SRC.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            if (
                isinstance(node, ast.Call)
                and getattr(node.func, "id", None) == "require_extra"
                and node.args
                and not isinstance(node.args[0], ast.Constant)
            ):
                non_literal.append(f"{path.relative_to(REPO)}:{node.lineno}")
    assert not non_literal, "require_extra called with a computed module name: " + ", ".join(non_literal)
