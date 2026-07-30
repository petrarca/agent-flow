"""Walking the planned groups — entry-point resolution and backward jump-back.

Where `interpret` owns one node's own re-run budget, this module owns movement
BETWEEN groups: where a run starts (`--start-from` / `--only`), and how far back
a cross-node `GoTo` may send the walk, bounded per target.
"""

from __future__ import annotations

from agent_flow.flow_types import NodeOutcome


def _resolve_entry(start_from: str, only: str, by_name, group_index, node_group, logger) -> tuple[int, bool]:
    """Resolve the walk's forward entry from the two mutually exclusive knobs.

    Returns (start_index, single_group): `only` -> (that group, True) so the walk
    runs exactly one group; `start_from` -> (that group, False) so it runs forward
    to the end; neither -> (0, False). Setting both is an error (they conflict).
    """
    if only and start_from:
        raise ValueError("only and start_from are mutually exclusive (only runs a single group; start_from runs from a group to the end)")
    if only:
        return _resolve_only_index(only, by_name, group_index, node_group, logger), True
    return _resolve_start_index(start_from, by_name, group_index, node_group, logger), False


def _name_to_group_index(target: str, by_name, group_index, node_group, kind: str) -> int:
    """Translate a NODE or parallel-GROUP name to its GROUP index.

    Shared by start_from and only: a name is either a node (resolved to its
    containing group) or a parallel-group name (used directly). `kind` labels the
    error/log so the two callers read distinctly. Unknown name -> ValueError.
    """
    if target in by_name:
        return group_index[node_group[target]]
    if target in group_index:  # a parallel-group name
        return group_index[target]
    known = sorted(set(by_name) | set(group_index))
    raise ValueError(f"{kind}={target!r} is not a known node or group (known: {known})")


def _resolve_start_index(start_from: str, by_name, group_index, node_group, logger) -> int:
    """Translate a start_from NODE name to its GROUP index (0 when unset).

    Forward entry point: the walk begins at the group CONTAINING start_from and
    proceeds forward. Granularity is the GROUP, not the node — a parallel group is
    the indivisible unit of execution, so if start_from is one member of a parallel
    group, the WHOLE group runs (you cannot enter "in the middle" of a fan-out).
    We log the entry group's members so that is visible, not surprising.

    `start_from` accepts either a NODE name or a parallel-GROUP name (the name you
    passed as agent_node(parallel_group=...)). A group name is the natural way to
    enter a fan-out; a member node name resolves to the same group.

    Skipping upstream assumes those nodes' side-effects (files on disk) and
    exported params already exist — the CALLER's responsibility (see docs).
    Runtime-populated params fall back to their defaults when their producer is
    skipped. Unknown name -> error.
    """
    if not start_from:
        return 0
    start_index = _name_to_group_index(start_from, by_name, group_index, node_group, "start_from")
    entry = sorted(n for n in by_name if group_index[node_group[n]] == start_index)
    skipped = sorted(n for n in by_name if group_index[node_group[n]] < start_index)
    if len(entry) > 1:
        logger.info(f"start_from={start_from}: entering at PARALLEL group {entry} (all run), skipping {skipped}")
    else:
        logger.info(f"start_from={start_from}: entering at {entry}, skipping {skipped}")
    return start_index


def _resolve_only_index(only: str, by_name, group_index, node_group, logger) -> int:
    """Translate an `only` NODE/GROUP name to the single GROUP index to run.

    Complement to start_from: run EXACTLY that one group and stop (see _walk's
    single_group). Same GROUP granularity as start_from — if `only` names a member
    of a parallel group, the WHOLE group runs (a fan-out is indivisible), so we log
    the group's members. Everything else (upstream AND downstream) is skipped;
    their outputs/exported params are assumed to already exist (caller's contract,
    same as start_from). Unknown name -> error.
    """
    idx = _name_to_group_index(only, by_name, group_index, node_group, "only")
    members = sorted(n for n in by_name if group_index[node_group[n]] == idx)
    if len(members) > 1:
        logger.info(f"only={only}: running PARALLEL group {members} (all run), skipping everything else")
    else:
        logger.info(f"only={only}: running {members}, skipping everything else")
    return idx


async def _walk(
    planned,
    *,
    run_group,
    group_index,
    node_group,
    by_name,
    logger,
    start_index: int = 0,
    single_group: bool = False,
    pending_instructions: dict[str, str] | None = None,
) -> dict[str, NodeOutcome]:
    """Walk the planned groups, honoring bounded cross-node jump-backs (GoTo).

    Returns per-node NodeOutcome (status + duration_s), so callers can render
    both. On a re-run (jump-back), a node's later outcome replaces the earlier.

    `start_index` is the FORWARD entry point (default 0 = the first group): the
    walk begins at that group, skipping earlier ones. This is orthogonal to
    jump-back — it sets where the walk STARTS; jump-back mutates position DURING
    the run. Node->index translation is the caller's job (this stays mechanical).

    `single_group` (the `only` mode) runs EXACTLY the group at start_index and
    stops: no forward advance to later groups, and gate GoTo jump-backs are
    ignored (there is nothing downstream to resume into). It is the surgical
    complement to start_from's "from here to the end".
    """
    results: dict[str, NodeOutcome] = {}
    jumps: dict[str, int] = {}
    i = start_index
    while i < len(planned):
        _key, group = planned[i]
        logger.debug(f"walk: group[{i}] {_key!r} -> nodes {[getattr(n, 'name', n) for n in group]}")
        outcomes = await run_group(group)
        for n_name, oc in outcomes.items():
            results[n_name] = oc
        if single_group:
            break  # `only` mode: run one group, ignore jump-backs and forward advance
        target = _pick_jump_back(outcomes, node_group, group_index, i, jumps, by_name, logger)
        if target is not None:
            jumps[target] = jumps.get(target, 0) + 1
            # Deliver the jumping node's one-time instruction to the target's re-run.
            if pending_instructions is not None:
                instr = _instruction_for_target(outcomes, target)
                if instr:
                    pending_instructions[target] = instr
            i = group_index[node_group[target]]  # rewind to the target's group
            continue
        i += 1
    return results


def _instruction_for_target(outcomes: dict[str, NodeOutcome], target: str) -> str:
    """The one-time instruction from the outcome whose GoTo chose `target` (or "")."""
    for oc in outcomes.values():
        if oc.goto == target and oc.instruction:
            return oc.instruction
    return ""


def _pick_jump_back(outcomes, node_group, group_index, current_i, jumps, by_name, logger) -> str | None:
    """From a group's outcomes, pick a valid bounded backward GoTo target (or None).

    Only backward jumps (to an earlier group) are honored, bounded per target by
    the target node's max_cycles. Forward/unknown/exhausted targets are ignored
    with a log so a gate mistake fails visibly rather than looping.
    """
    for oc in outcomes.values():
        target = oc.goto
        if target is None:
            continue
        if target not in by_name:
            logger.warning(f"GoTo target {target!r} is not a known node — ignoring")
            continue
        target_i = group_index[node_group[target]]
        if target_i >= current_i:
            logger.warning(f"GoTo {target!r} is not a backward jump — ignoring")
            continue
        if jumps.get(target, 0) >= by_name[target].max_cycles:
            logger.warning(f"GoTo {target!r} exhausted (max_cycles) — proceeding")
            continue
        logger.info(f"jump-back to {target!r} (re-running from its group)")
        return target
    return None
