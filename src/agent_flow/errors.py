"""The agent-failure taxonomy — what the engine is allowed to RETRY.

A node's `run` fails in two materially different ways, and the engine must treat
them differently:

  - TRANSIENT — the agent hung (liveness timeout) or its process crashed (OOM, a
    provider 5xx, a rate limit). Nothing about the request was wrong; running it
    again may well succeed. The engine RETRIES these, bounded by the node's
    `max_retries`.
  - PERMANENT — the agent ran, diagnosed a problem itself, and reported it (a
    missing input, an unparseable source, a bad config). The same prompt will
    fail the same way, so a retry only burns tokens. The engine NEVER retries
    these.

Anything else a consumer's `run` raises is treated as permanent: the engine
cannot know whether it is safe to repeat, and silently repeating unknown
side-effects is worse than failing once.

This taxonomy is a LEAF (it imports nothing from agent_flow) precisely so both
sides can depend on it: `runners` raises these (Tier 1) and `engine` classifies
them (Tier 3), while the layering contract still forbids engine -> runners. The
classification therefore travels on the exception TYPE rather than a name match
or a magic attribute.

A consumer writing a custom `run` callable opts into the same policy by raising
`TransientAgentError` (retryable) or `PermanentAgentError` (not).

When retries are exhausted the failure is handed to the node's `criticality`,
which makes the final call: `degrade` records the node as degraded and the flow
continues; `blocking` halts the run.
"""

from __future__ import annotations


class AgentError(RuntimeError):
    """Base for a failure of the agent behind a node.

    Subclass `TransientAgentError` or `PermanentAgentError` rather than this —
    the engine's retry policy keys on that distinction, and a bare `AgentError`
    is treated as permanent (the safe default).
    """


class TransientAgentError(AgentError):
    """A failure that MAY succeed if simply retried.

    The request was fine; the execution was not — the agent went silent and was
    killed, or its process died. The engine re-runs the node, bounded by
    `max_retries`, before falling through to `criticality`.
    """


class PermanentAgentError(AgentError):
    """A failure that will repeat identically, so the engine must NOT retry.

    The agent completed and reported a problem it diagnosed itself. Retrying the
    same prompt cannot change the outcome; the failure goes straight to the
    node's `criticality`.
    """
