"""The re-run REQUEST — the one flow-control lever an agent may pull.

An agent's only way to influence the flow is to ask that an EARLIER step run
again. It does that with the control file's top-level `rerun_required` field.
This module owns that field end to end: what a node GRANTS (`RerunSpec`), what
an agent may WRITE (the accepted JSON shapes), and how it is PARSED
(`parse_rerun`).

Where it sits in the control file. The sidecar has three zones with three
distinct readers, and the re-run field is its own:

    status / agent / reason   the ENGINE reads (the success verdict)
    rerun_required            FLOW CONTROL — read by the engine only because
                              the node explicitly declared `rerun_targets`
    result                    the APPLICATION reads (domain data); the engine
                              never looks inside

Granting. A node grants the lever by declaring `rerun_targets` — the node/group
names it may ask to re-run. No declaration means no grant: the preamble says
nothing about re-running and the field is ignored. So the ~40 agents in a
pipeline that never re-run anything carry none of this in their prompt.

The accepted shapes (all mean "re-run one target"):

    "rerun_required": true                       # sole target implied
    "rerun_required": "domain"                   # named target
    "rerun_required": {"target": "domain",
                       "instruction": "recompute the coupling figure"}

`target` is OPTIONAL when the node granted exactly one — there is no choice to
make, so demanding the agent echo a name it cannot get wrong is ceremony that
only adds a failure mode. It is REQUIRED when several were granted; `parse_rerun`
fills in the sole target for the first case and refuses an unresolvable request
in the second.

Granting is an ALLOWLIST, and parsing does NOT enforce it — the engine does, at
the point it acts on the request (`engine.interpreter._with_rerun_request`), so
there is exactly one place that decides what an agent was permitted to ask for.
A target outside the grant is refused with a log line, never silently honored:
an LLM naming a plausible-but-ungranted node must not be able to steer the flow.

Deliberately SINGULAR. One request names one target, because the machinery is
singular all the way down: a jump-back resolves to one `GoTo`, which carries one
node. A list would promise something the engine cannot honor. "Re-run several
nodes" is expressed by naming their parallel GROUP, which expands to its members.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class RerunTarget:
    """One granted target: a node, or a parallel GROUP and what it stands for.

    name     the node or parallel-group name the agent writes.
    members  for a GROUP, the node names it expands to (so the preamble can say
             what the name covers — a bare "analysis" is opaque to an agent
             choosing between targets). Empty for a plain node, which stands for
             itself.
    """

    name: str
    members: tuple[str, ...] = ()


@dataclass(frozen=True)
class RerunSpec:
    """What a node GRANTS: the targets its agent may ask to re-run.

    Built from the node's `rerun_targets` declaration and carried to the agent
    (via the invocation) so the preamble can name the legal set. Empty targets is
    not a valid spec — a node with no declaration gets no spec at all (None).

    targets  the granted targets, in declaration order. A group target carries its
             members for display; the walker expands it when the jump happens.
    """

    targets: tuple[RerunTarget, ...]

    @property
    def names(self) -> tuple[str, ...]:
        """The granted names — what an agent may legally write as `target`."""
        return tuple(t.name for t in self.targets)

    @property
    def sole_target(self) -> str | None:
        """The single granted target's name, or None when several were granted.

        This is what makes `target` optional in the request: with one grant there
        is nothing to choose, so an agent may simply answer `true`.
        """
        return self.targets[0].name if len(self.targets) == 1 else None

    @classmethod
    def of(cls, *names: str) -> RerunSpec:
        """Build a spec of plain NODE targets — the convenience for tests/callers
        that have no group membership to express."""
        return cls(targets=tuple(RerunTarget(name=n) for n in names))


@dataclass(frozen=True)
class RerunRequest:
    """A parsed re-run request: WHICH target, and WHAT to tell it.

    target       the node/group to re-run. Always populated after parsing — when
                 the agent omitted it, `parse_rerun` filled in the spec's sole
                 target (the only thing it could have meant).
    instruction  optional one-time guidance delivered to the target's next run,
                 verbatim, as the last block of its prompt. Empty when the agent
                 gave none.
    """

    target: str
    instruction: str = ""


def parse_rerun(control: dict | None, spec: RerunSpec | None) -> RerunRequest | None:
    """Parse `rerun_required` from a control envelope into a RerunRequest.

    Returns None when no re-run was asked for — the field is absent, false, empty,
    or the node was never granted the lever (`spec` is None). Returning None is
    the overwhelmingly common case: most agents never ask.

    Accepts the three documented shapes (`true` / a name / an object). A `true`
    or a target-less object resolves to the spec's `sole_target`; when the node
    granted SEVERAL targets there is nothing to resolve to, so the request is
    incomplete and None is returned — the agent had a choice and did not make it.

    This does NOT authorize the target. Whether the named target is on the
    granted list, is backward, and still has re-run budget is decided at the
    jump, by the engine — one checkpoint, not one per caller.
    """
    if spec is None or not isinstance(control, dict):
        return None
    raw = control.get("rerun_required")
    if raw is None or raw is False:
        return None
    if raw is True:
        return RerunRequest(target=spec.sole_target) if spec.sole_target else None
    if isinstance(raw, str):
        name = raw.strip()
        return RerunRequest(target=name) if name else None
    if isinstance(raw, dict):
        target = str(raw.get("target") or "").strip() or spec.sole_target
        if not target:
            return None
        instruction = raw.get("instruction")
        return RerunRequest(target=target, instruction=instruction.strip() if isinstance(instruction, str) else "")
    return None
