"""Graph planning — turn a flat list of `Node`s into ordered parallel groups.

Pure and backend-free: no execution, no gates, no I/O. `plan_groups` is the
whole public surface; the rest is grouping and topological sort.
"""

from __future__ import annotations

from collections.abc import Iterable

from agent_flow.flow_types import Node


def _group_membership(nodes: list[Node]) -> tuple[dict[str, list[Node]], list[str]]:
    """Bucket nodes by parallel_group (or their own name), preserving order."""
    groups: dict[str, list[Node]] = {}
    order: list[str] = []
    for n in nodes:
        key = n.parallel_group or n.name
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(n)
    return groups, order


def _group_dependencies(groups: dict[str, list[Node]], order: list[str], by_name: dict[str, Node]) -> dict[str, set[str]]:
    """Map each group to the groups it depends on. Raises on unknown deps."""
    group_of = {name: (n.parallel_group or n.name) for name, n in by_name.items()}
    group_deps: dict[str, set[str]] = {}
    for key in order:
        deps: set[str] = set()
        for n in groups[key]:
            for d in n.depends_on:
                if d not in by_name:
                    raise ValueError(f"node {n.name!r} depends on unknown node {d!r}")
                if group_of[d] != key:
                    deps.add(group_of[d])
        group_deps[key] = deps
    return group_deps


def _toposort(order: list[str], group_deps: dict[str, set[str]]) -> list[str]:
    """Kahn topological sort over groups, tie-broken by declaration order."""
    done: set[str] = set()
    planned: list[str] = []
    remaining = list(order)
    while remaining:
        ready = [k for k in remaining if group_deps[k] <= done]
        if not ready:
            raise ValueError(f"dependency cycle among groups: {remaining}")
        for k in ready:
            planned.append(k)
            done.add(k)
            remaining.remove(k)
    return planned


def _check_unique_names(nodes: list[Node]) -> None:
    """Raise unless every node name is unique — `Node.name` IS the node's id.

    Names key the whole engine: `by_name`, results, `depends_on`, `--only` /
    `--start-from`, and GoTo targets. A duplicate silently collapses those (the
    last definition wins, so an earlier node's `run` never executes while the
    later one runs once per duplicate) — a wrong result with no error. Caught
    here at build time, alongside cycles and unknown deps.
    """
    seen: set[str] = set()
    dupes: list[str] = []
    for n in nodes:
        if n.name in seen and n.name not in dupes:
            dupes.append(n.name)
        seen.add(n.name)
    if dupes:
        raise ValueError(f"duplicate node name(s): {sorted(dupes)} — every Node.name must be unique (it is the node's id)")


def plan_groups(nodes: Iterable[Node]) -> list[tuple[str, list[Node]]]:
    """Order nodes into execution groups honoring depends_on and parallel_group.

    Returns a list of (group_key, nodes) in a valid execution order: every
    group's dependencies appear in an earlier group. Nodes sharing a
    parallel_group run together; solo nodes are their own single-member group.

    Raises ValueError on a DUPLICATE node name, an unknown dependency, or a
    dependency cycle — all caught at BUILD time, before anything runs.
    """
    nodes = list(nodes)
    _check_unique_names(nodes)
    by_name = {n.name: n for n in nodes}
    groups, order = _group_membership(nodes)
    group_deps = _group_dependencies(groups, order, by_name)
    return [(key, groups[key]) for key in _toposort(order, group_deps)]
