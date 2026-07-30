"""Architecture fitness functions: dependency DIRECTION and total acyclicity.

`test_prefect_isolation.py` guards the EAGER import graph — the one that can
deadlock at package init — and deliberately ignores function-local imports,
which is correct for what it claims. A cycle broken by a deferred import does
not deadlock, but it is still two modules that cannot be understood or changed
independently, and that guard passes on it by construction.

The tests here cover what that leaves open:

  - `test_no_import_cycles_including_deferred` walks EVERY import, at any nesting
    depth, so a function-local import cannot hide a cycle.
  - `test_layer_direction` asserts the dependency arrows point one way. Absence
    of cycles is not the same claim: A -> B is acyclic whichever way it runs, and
    only one of the two directions is the architecture.
  - `test_forbidden_edges` names the rules rank cannot express — `engine -> core`
    is a downward edge and exactly what the tier rule forbids.
"""

from __future__ import annotations

import ast
import collections
import pathlib

import pytest

pytestmark = pytest.mark.unit

SRC = pathlib.Path(__file__).resolve().parents[2] / "src" / "agent_flow"

# The layering, low to high. A unit may import anything BELOW it and nothing
# above. Same-rank edges are allowed (a cycle among them is caught separately).
#
# Read it as: the library <-> agent contract at the bottom, the runtime adapters
# above it, the Tier-1 API above those, the orchestration layer above that, and
# the execution backends plus the CLI on top.
LAYERS: list[set[str]] = [
    # Leaves: no agent_flow imports at all. `gates` is here because a gate is the
    # consumer's hook — it decides flow control from what it is handed, and needs
    # nothing from the runtime to do so.
    {"const", "logging_setup", "_version", "protocol", "gates", "run_context"},
    {"flow_types", "run_config", "utils"},  # the DAG vocabulary + run settings
    {"runners", "backends"},  # the two execution seams
    {"core", "preflight"},  # Tier-1: run_agent, and the pre-run checks
    {"node_builder"},  # the Tier-3 <-> Tier-1 bridge
    {"registry"},  # name -> impl, incl. the shipped renderers
    {"engine"},  # Tier-3: build_flow, the walk, backend resolution
    {"flowdef"},  # serializable FlowDef -> compiled flow (calls build_flow)
    {"cli"},  # presentation
]
RANK = {unit: i for i, layer in enumerate(LAYERS) for unit in layer}

# Edges forbidden REGARDLESS of rank — the rules the project states in prose.
# Rank alone cannot express these: `engine -> core` is a downward edge and still
# exactly what the tier rule forbids. "*" means "any agent_flow package".
FORBIDDEN: list[tuple[str, str, str]] = [
    ("engine", "core", "Tier 3 must not import Tier 1 — they meet only through a node's `run` callable"),
    ("engine", "runners", "Tier 3 must not import a runtime adapter — a node's `run` owns that choice"),
    ("engine", "node_builder", "the engine must not know how a node is built, only that it has a `run`"),
    ("backends", "engine", "a backend depends on the flow vocabulary, never on the engine it is swappable for"),
    ("protocol", "*", "protocol is the leaf both core and runners depend on; it may import nothing"),
    ("flow_types", "engine", "the flow vocabulary must not depend on the engine that consumes it"),
    ("gates", "*", "a gate decides from what it is handed; keeping it a leaf keeps the tier rule true of Tier 3 generally"),
]


def _unit(path: pathlib.Path) -> str:
    """The top-level package or module a file belongs to ('__init__' = the facade)."""
    parts = list(path.relative_to(SRC).with_suffix("").parts)
    return "__init__" if parts == ["__init__"] else parts[0]


def _all_imports(tree: ast.AST) -> set[str]:
    """Every agent_flow import in the file — module-level AND function-local."""
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("agent_flow"):
            found.add(node.module)
        elif isinstance(node, ast.Import):
            found |= {a.name for a in node.names if a.name.startswith("agent_flow")}
    return found


def _unit_graph() -> dict[str, set[str]]:
    """Top-level unit -> the units it depends on, counting deferred imports."""
    graph: dict[str, set[str]] = collections.defaultdict(set)
    for path in SRC.rglob("*.py"):
        src_unit = _unit(path)
        graph.setdefault(src_unit, set())
        for module in _all_imports(ast.parse(path.read_text())):
            parts = module.split(".")
            dst = parts[1] if len(parts) > 1 else "__init__"
            if dst != src_unit:
                graph[src_unit].add(dst)
    return graph


def _module_graph() -> dict[str, set[str]]:
    """Module -> modules, counting deferred imports. Unresolvable names walk up."""
    mods = {}
    for path in SRC.rglob("*.py"):
        parts = list(path.relative_to(SRC).with_suffix("").parts)
        if parts[-1] == "__init__":
            parts = parts[:-1]
        mods[".".join(["agent_flow", *parts])] = path
    graph: dict[str, set[str]] = {}
    for name, path in mods.items():
        deps = set()
        for module in _all_imports(ast.parse(path.read_text())):
            target = module
            while target and target not in mods:
                target = target.rsplit(".", 1)[0] if "." in target else None
            if target and target != name:
                deps.add(target)
        graph[name] = deps
    return graph


def _find_cycles(graph: dict[str, set[str]]) -> list[list[str]]:
    """Every import cycle in `graph`, as the path that closes each one."""
    state: dict[str, int] = collections.defaultdict(int)  # 0 unvisited, 1 on stack, 2 done
    cycles: list[list[str]] = []

    def visit(node: str, stack: list[str]) -> None:
        state[node] = 1
        stack.append(node)
        for dep in sorted(graph.get(node, ())):
            if state[dep] == 1:
                cycles.append(stack[stack.index(dep) :] + [dep])
            elif state[dep] == 0:
                visit(dep, stack)
        state[node] = 2
        stack.pop()

    for node in sorted(graph):
        if state[node] == 0:
            visit(node, [])
    return cycles


def test_no_import_cycles_including_deferred():
    """No cycle in the module graph, counting function-local imports too.

    A cycle broken by a deferred import does not deadlock, but it is still two
    modules that cannot be understood or changed independently — and the next
    change to either has to hold both sides in mind.
    """
    cycles = _find_cycles(_module_graph())
    assert not cycles, "import cycles (deferred imports included): " + "; ".join(" -> ".join(c) for c in cycles)


def test_every_unit_is_assigned_a_layer():
    """A new top-level package must be placed in LAYERS deliberately."""
    unplaced = {u for u in _unit_graph() if u not in RANK and u != "__init__"}
    assert not unplaced, f"top-level units missing from LAYERS: {sorted(unplaced)} — decide where they belong"


def test_layer_direction():
    """Dependencies point one way: a unit imports only its own layer or below.

    This is the rule the codebase states and, before this test existed, held only
    by inspection: the engine (Tier 3) must not reach into the runtime core
    (Tier 1) — they meet through a node's `run` callable. The same rule keeps
    `protocol` a leaf both `core` and `runners` can depend on, and keeps a
    backend depending on the flow vocabulary rather than on the engine it is
    swappable for.
    """
    violations = []
    for unit, deps in _unit_graph().items():
        if unit == "__init__":  # the facade sits above everything by definition
            continue
        for dep in deps:
            if dep == "__init__":
                violations.append(f"{unit} -> the agent_flow facade (a submodule must never import the package root)")
            elif RANK[dep] > RANK[unit]:
                violations.append(f"{unit} (L{RANK[unit]}) -> {dep} (L{RANK[dep]}) — upward import")
    assert not violations, "layer violations:\n  " + "\n  ".join(sorted(violations))


def test_forbidden_edges():
    """The named rules that rank alone cannot express.

    `engine -> core` is a DOWNWARD edge, so the layer test above accepts it — and
    it is precisely what the project's tier rule forbids. These pairs are checked
    by name for that reason.
    """
    graph = _unit_graph()
    violations = []
    for src, dst, reason in FORBIDDEN:
        deps = graph.get(src, set())
        hit = sorted(deps) if dst == "*" else ([dst] if dst in deps else [])
        violations += [f"{src} -> {d}: {reason}" for d in hit]
    assert not violations, "forbidden dependencies:\n  " + "\n  ".join(violations)
