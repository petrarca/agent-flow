"""Guard: the core primitives + pure DAG logic + LocalBackend stay Prefect-free.

This is the HIGH-value guarantee behind the FlowBackend seam (#4): a consumer on
a different orchestrator (Airflow, Temporal, a bespoke loop) must be able to use
agent-flow WITHOUT pulling in Prefect, and an everyday local run must not need it
either. These tests fail loudly if a future change makes any of that import
prefect at module load, or introduces an eager import cycle.
"""

import ast
import builtins
import collections
import importlib
import pathlib
import sys

import pytest

# Modules that MUST import (and run, for the backend) with prefect unavailable.
_CORE_MODULES = [
    "agent_flow.core",
    "agent_flow.core.agent_runtime",
    "agent_flow.runners",
    "agent_flow.backends",
    "agent_flow.backends.local",
    "agent_flow.engine",
]


@pytest.fixture
def prefect_blocked(monkeypatch):
    """Make any `import prefect` raise ImportError, and drop cached prefect modules."""
    real_import = builtins.__import__

    def guard(name, *args, **kwargs):
        if name == "prefect" or name.startswith("prefect."):
            raise ImportError("prefect blocked by isolation test")
        return real_import(name, *args, **kwargs)

    for m in [m for m in sys.modules if m == "prefect" or m.startswith("prefect.")]:
        monkeypatch.delitem(sys.modules, m, raising=False)
    monkeypatch.setattr(builtins, "__import__", guard)
    return guard


def test_core_and_backend_import_without_prefect(prefect_blocked, monkeypatch):
    # Force a fresh import of each module under the block.
    for m in _CORE_MODULES:
        monkeypatch.delitem(sys.modules, m, raising=False)
    for m in _CORE_MODULES:
        importlib.import_module(m)  # must not raise


def test_local_backend_runs_a_flow_without_prefect(prefect_blocked, monkeypatch, tmp_path):
    for m in _CORE_MODULES:
        monkeypatch.delitem(sys.modules, m, raising=False)
    from agent_flow.engine import Node, build_flow

    def mk(nm):
        def run(_ctx):
            return {"ran": nm}

        return run

    # solo -> a 2-node parallel group; exercises run_group's both branches.
    nodes = [
        Node(name="a", run=mk("a")),
        Node(name="p1", run=mk("p1"), parallel_group="workers", depends_on=["a"]),
        Node(name="p2", run=mk("p2"), parallel_group="workers", depends_on=["a"]),
    ]
    result = build_flow(nodes, name="iso", backend="local")(run_dir=str(tmp_path))
    assert {n: oc.status for n, oc in result.items()} == {"a": "ok", "p1": "ok", "p2": "ok"}


def test_pure_dag_helpers_import_without_prefect(prefect_blocked, monkeypatch):
    monkeypatch.delitem(sys.modules, "agent_flow.engine", raising=False)
    from agent_flow.engine import _resolve_entry, _walk, interpret, plan_groups  # noqa: F401


def _module_name(path, src):
    parts = list(path.relative_to(src).with_suffix("").parts)
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def _eager_agent_flow_imports(tree):
    """Top-level (module-level) agent_flow.* imports in an AST — lazy ones excluded."""
    found = set()
    for node in tree.body:  # TOP-LEVEL statements only
        if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("agent_flow"):
            found.add(node.module)
        elif isinstance(node, ast.Import):
            found |= {a.name for a in node.names if a.name.startswith("agent_flow")}
        elif isinstance(node, ast.Try):  # top-level try/except import guards
            for sub in ast.walk(node):
                if isinstance(sub, ast.ImportFrom) and sub.module and sub.module.startswith("agent_flow"):
                    found.add(sub.module)
    return found


def _build_eager_graph():
    root = pathlib.Path(__file__).resolve().parents[2] / "src" / "agent_flow"
    src = root.parent
    graph = collections.defaultdict(set)
    for p in root.rglob("*.py"):
        graph[_module_name(p, src)] = _eager_agent_flow_imports(ast.parse(p.read_text()))
    return graph


def _find_cycles(graph):
    color = collections.defaultdict(int)
    cycles = []

    def dfs(u, stack):
        color[u] = 1
        stack.append(u)
        for v in graph.get(u, ()):
            if color[v] == 1:
                cycles.append(stack[stack.index(v) :] + [v])
            elif color[v] == 0 and v in graph:
                dfs(v, stack)
        color[u] = 2
        stack.pop()

    for n in list(graph):
        if color[n] == 0:
            dfs(n, [])
    return cycles


def test_no_eager_import_cycles():
    """No module-level (eager) import cycle in the agent_flow.* graph.

    Function-local (lazy) imports are allowed and excluded — only top-level
    imports can deadlock at package init.
    """
    cycles = _find_cycles(_build_eager_graph())
    assert not cycles, f"eager import cycles: {cycles}"
