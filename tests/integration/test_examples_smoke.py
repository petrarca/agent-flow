"""Smoke-run every shipped example in its token-free mode.

The examples ARE the consumer-facing contract: each demonstrates a tier
(imperative / declarative Tier 3, custom Tier 2, in-process) against the real
public API. Nothing else in the suite exercises them, and that gap let
`examples/custom_flow.py` ship broken for a whole release — `MockExecutor.run`
became a coroutine in the async migration and the example's mock branch never
awaited it, which only surfaced by running the example by hand.

Integration-marked because they spawn subprocesses (and the custom example spins
Prefect's temporary server). No tokens: every mode here is mocked.
"""

import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]

# (module, argv) — each runs with mocked agents, so no runtime and no tokens.
EXAMPLES = [
    pytest.param("examples.imperative", ["run", "-p", "product_key=smoke", "--mock-agents"], id="imperative-mock"),
    pytest.param("examples.declarative", ["run", "-p", "product_key=smoke", "--mock-agents"], id="declarative-mock"),
    pytest.param("examples.custom_flow", ["--topic", "Smoke", "--runtime", "mock"], id="custom-mock"),
    pytest.param("examples.inprocess", ["a ticket"], id="inprocess"),
    pytest.param("examples.inprocess", ["--async", "a ticket"], id="inprocess-async"),
]


@pytest.mark.parametrize("module,argv", EXAMPLES)
def test_example_runs(module, argv, tmp_path):
    cmd = [sys.executable, "-m", module, *argv]
    if module != "examples.inprocess":  # the in-process example takes no --run-dir
        cmd += ["--run-dir", str(tmp_path / "out")]
    proc = subprocess.run(cmd, cwd=REPO, capture_output=True, text=True, timeout=300)
    assert proc.returncode == 0, f"{module} failed (rc={proc.returncode})\nSTDOUT:\n{proc.stdout[-2000:]}\nSTDERR:\n{proc.stderr[-2000:]}"
    # An un-awaited coroutine is the failure mode that slipped through before: it
    # only warns, so assert on it explicitly rather than trusting the exit code.
    combined = proc.stdout + proc.stderr
    assert "was never awaited" not in combined, f"{module} left a coroutine un-awaited:\n{combined[-2000:]}"
