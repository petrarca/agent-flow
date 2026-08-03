"""Walking the planned groups — entry-point resolution and backward jump-back.

Where `interpret` owns one node's own re-run budget, this module owns movement
BETWEEN groups: where a run starts (`--start-from` / `--only`), and how far back
a cross-node `GoTo` may send the walk, bounded per target.
"""

from __future__ import annotations

from agent_flow.flow_types import NodeOutcome


def _resolve_entry(start_from: str, only: str, stop_after: str, by_name, group_index, node_group, logger) -> tuple[int, int, bool]:
    """Resolve the walk's forward range from the entry/exit knobs.

    Returns (start_index, stop_index, single_group), where the walk runs groups
    [start_index, stop_index):
      - `only`       -> that one group (single_group=True); ignores stop_after.
      - `start_from` -> from that group forward.
      - `stop_after` -> UP TO AND INCLUDING that group (so the named node is the
        last executed). Combine with `start_from` to run an arbitrary A..B segment.
      - none         -> the whole flow.

    `only` conflicts with both `start_from` and `stop_after` (it is already exactly
    one group). A `stop_after` group BEFORE the `start_from` group is an error.
    """
    n_groups = len(group_index)
    if only and (start_from or stop_after):
        raise ValueError("only is exclusive with start_from/stop_after (only runs a single group; the others bound a range)")
    if only:
        return _resolve_only_index(only, by_name, group_index, node_group, logger), n_groups, True
    start_index = _resolve_start_index(start_from, by_name, group_index, node_group, logger)
    stop_index = n_groups
    if stop_after:
        stop_i = _name_to_group_index(stop_after, by_name, group_index, node_group, "stop_after")
        if stop_i < start_index:
            raise ValueError(f"stop_after={stop_after!r} (group {stop_i}) is before start_from (group {start_index}) — the range is empty")
        stop_index = stop_i + 1  # inclusive: the named group is the last one run
        members = sorted(n for n in by_name if group_index[node_group[n]] == stop_i)
        logger.info(f"stop_after={stop_after}: last group is {members} (skipping everything after it)")
    return start_index, stop_index, False


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
    stop_index: int | None = None,
    single_group: bool = False,
    pending_instructions: dict[str, str] | None = None,
) -> dict[str, NodeOutcome]:
    """Walk the planned groups, honoring bounded cross-node jump-backs (GoTo).

    Returns per-node NodeOutcome (status + duration_s), so callers can render
    both. On a re-run (jump-back), a node's later outcome replaces the earlier.

    The walk runs groups in the half-open range [start_index, stop_index).

    `start_index` is the FORWARD entry point (default 0 = the first group): the
    walk begins at that group, skipping earlier ones. `stop_index` (default: all
    groups) is the exclusive upper bound — the `stop_after` knob sets it so the
    named group is the LAST one run. Both are orthogonal to jump-back: they bound
    where the walk STARTS and ENDS; jump-back mutates position DURING the run
    (and stays within the range — a jump target is behind the current position, so
    always < stop_index).

    `single_group` (the `only` mode) runs EXACTLY the group at start_index and
    stops: no forward advance to later groups, and gate GoTo jump-backs are
    ignored (there is nothing downstream to resume into). It is the surgical
    complement to start_from's "from here to the end".
    """
    results: dict[str, NodeOutcome] = {}
    jumps: dict[str, int] = {}
    # One-shot per-group RESTRICTION: {group_index: {node names to run}}. Set by a
    # node-level jump-back so the re-entered group re-runs ONLY the flagged
    # node(s), not their parallel siblings (the gate is the authority on what
    # re-runs). Consumed (popped) as the forward walk reaches each group, so a
    # later unrelated pass runs the group in full again.
    restrict: dict[int, set[str]] = {}
    stop = len(planned) if stop_index is None else stop_index
    i = start_index
    while i < stop:
        _key, group = planned[i]
        logger.debug(f"walk: group[{i}] {_key!r} -> nodes {[getattr(n, 'name', n) for n in group]}")
        outcomes = await run_group(group, restrict.pop(i, None))
        for n_name, oc in outcomes.items():
            results[n_name] = oc
        if single_group:
            break  # `only` mode: run one group, ignore jump-backs and forward advance
        jump = _pick_jump_back(outcomes, node_group, group_index, i, jumps, by_name, logger, planned)
        if jump is not None:
            target_i, targets, origin = jump
            for t in targets:
                jumps[t] = jumps.get(t, 0) + 1
                # Deliver each jumping node's one-time instruction to its target's re-run.
                if pending_instructions is not None:
                    instr = _instruction_for_target(outcomes, t, origin)
                    if instr:
                        pending_instructions[t] = instr
            # Restrict the target group to EXACTLY the flagged node(s) on re-entry
            # — a node-level jump-back (re-run only what the gate named, not their
            # siblings; the gate is the authority). The forward re-flow past it
            # runs later groups in full (they carry no restriction).
            restrict[target_i] = set(targets)
            i = target_i  # rewind to the target group
            continue
        i += 1
    return results


def _instruction_for_target(outcomes: dict[str, NodeOutcome], target: str, chose: dict[str, str]) -> str:
    """The one-time instruction destined for `target` (or "").

    `chose` maps each resolved target node to the name the jumping outcome
    actually wrote — the same string for a plain node, but the GROUP name when
    the jump was expanded. Without it a group jump would deliver nothing: the
    outcome says `goto="analysis"` while the targets are its members.

    A group instruction reaches EVERY member that re-runs. That is the intended
    reading: choosing the group (over one of its members) is itself the claim
    that the reason applies to the whole wave.
    """
    wanted = chose.get(target, target)
    for oc in outcomes.values():
        if oc.goto == wanted and oc.instruction:
            return oc.instruction
    return ""


def _pick_jump_back(
    outcomes, node_group, group_index, current_i, jumps, by_name, logger, planned=None
) -> tuple[int, set[str], dict[str, str]] | None:
    """Pick the backward jump-back as (target_group_index, {node names}, origin).

    Only backward jumps (to an earlier group) are honored, bounded per target by
    the target node's max_cycles. Forward/unknown/exhausted targets are ignored
    with a log so a mistake fails visibly rather than looping.

    A target may name a parallel GROUP instead of a node — the way to say "re-run
    that whole wave", which a single GoTo could not otherwise express (it carries
    one name). A group EXPANDS to its members here, and each member is then bound
    by its OWN max_cycles: an exhausted member is skipped while its eligible
    siblings still re-run, exactly as a per-node target behaves. A group whose
    members are all exhausted yields no jump.

    When outcomes name valid backward targets in DIFFERENT groups (e.g. a
    consistency-check flags nodes across stages), the EARLIEST group wins — the
    flow rewinds furthest back and re-flows forward through the rest, matching
    "re-run the earliest affected stage, cascade forward". ALL valid targets that
    land in that earliest group are returned together, so several verifiers in one
    fan-out each re-run their own analyst (a subset of the group), not just one.

    `origin` maps each resolved node back to the name the outcome WROTE (itself,
    or the group it came from), so the instruction can be delivered to a group's
    members. Returns None when no valid backward target exists.
    """
    members = {key: [getattr(n, "name", str(n)) for n in group] for key, group in (planned or [])}
    by_group: dict[int, set[str]] = {}
    origin: dict[str, str] = {}
    for oc in outcomes.values():
        target = oc.goto
        if target is None:
            continue
        # A group name stands for its members; a node stands for itself.
        if target in members and target not in by_name:
            resolved, target_i = members[target], group_index[target]
        elif target in by_name:
            resolved, target_i = [target], group_index[node_group[target]]
        else:
            logger.warning(f"GoTo target {target!r} is not a known node or parallel group — ignoring")
            continue
        if target_i >= current_i:
            logger.warning(f"GoTo {target!r} is not a backward jump — ignoring")
            continue
        eligible = {n for n in resolved if jumps.get(n, 0) < by_name[n].max_cycles}
        if not eligible:
            logger.warning(f"GoTo {target!r} exhausted (max_cycles) — proceeding")
            continue
        if skipped := sorted(set(resolved) - eligible):
            logger.warning(f"GoTo {target!r}: {skipped} exhausted (max_cycles) — re-running the rest")
        by_group.setdefault(target_i, set()).update(eligible)
        for n in eligible:
            origin[n] = target
    if not by_group:
        return None
    earliest = min(by_group)  # earliest affected group; re-flow handles later ones
    targets = by_group[earliest]
    logger.info(f"jump-back to {sorted(targets)} (re-running only those node(s), then re-flowing forward)")
    return earliest, targets, origin
