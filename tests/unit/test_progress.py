"""Unit tests for the line-based node progress printer and results table.

NodeProgressPrinter is pure rendering (no Prefect): it consumes on_node_event
calls and prints one line per transition. The build_flow -> on_node_event
emission itself is covered in integration (it needs the Prefect task).
"""

from agent_flow.cli import NodeProgressPrinter, print_results_table
from agent_flow.engine import NodeOutcome
from agent_flow.runners.executor import qualified_agent


class _CaptureConsole:
    """Minimal console stub capturing printed lines (avoids rich terminal deps)."""

    is_terminal = False

    def __init__(self):
        self.lines: list[str] = []

    def print(self, *args, **_kwargs):
        self.lines.append(" ".join(str(a) for a in args))


def test_progress_prints_running_then_finish():
    con = _CaptureConsole()
    p = NodeProgressPrinter(console=con)
    p.on_node_event("domain", "start", None, "domain-analyst")
    p.on_node_event("domain", "finish", "ok", "domain-analyst")
    assert len(con.lines) == 2
    assert "running" in con.lines[0] and "domain" in con.lines[0]
    assert "ok" in con.lines[1]
    # agent shown as informal context
    assert "domain-analyst" in con.lines[0]


def test_progress_finish_shows_duration():
    con = _CaptureConsole()
    p = NodeProgressPrinter(console=con)
    p.on_node_event("a", "start", None, "")
    p.on_node_event("a", "finish", "ok", "")
    # finish line carries an elapsed "…s" marker; start line does not
    assert "s" in con.lines[1]
    assert con.lines[0].endswith("running") or "running" in con.lines[0]


def test_progress_finish_without_start_is_safe():
    # A finish with no prior start (e.g. printer created mid-run) must not crash.
    con = _CaptureConsole()
    NodeProgressPrinter(console=con).on_node_event("x", "finish", "degraded", "")
    assert "degraded" in con.lines[0]


def test_results_table_accepts_node_outcomes():
    con = _CaptureConsole()
    results = {
        "a": NodeOutcome(status="ok", duration_s=1.2),
        "b": NodeOutcome(status="degraded", duration_s=3.4),
    }
    # Should not raise; renders a rich Table via the stub console.
    print_results_table(results, agents={"a": "analyst", "b": "verifier"}, console=con)
    assert con.lines  # something was printed


def test_results_table_accepts_bare_status_strings():
    # Back-compat: a plain {name: status} dict still renders.
    con = _CaptureConsole()
    print_results_table({"a": "ok"}, console=con)
    assert con.lines


def test_results_table_renders_runtime_qualified_agent():
    # The Agent column shows "<runtime>:<agent>", runtime taken from the outcome.
    # Use a real rich Console capturing to text so we assert on rendered cells.
    from rich.console import Console

    con = Console(record=True, width=120)
    results = {"a": NodeOutcome(status="ok", duration_s=1.0, runtime="opencode")}
    print_results_table(results, agents={"a": "analyst"}, console=con)
    assert "opencode:analyst" in con.export_text()


def test_qualified_agent_formats():
    # Canonical label format (single source of truth for the ':' separator).
    assert qualified_agent("opencode", "my-agent") == "opencode:my-agent"
    assert qualified_agent("inproc", "x") == "inproc:x"
    assert qualified_agent("mock", "y") == "mock:y"
    # Degenerate inputs fall back to the non-empty side (no dangling separator).
    assert qualified_agent("", "bare") == "bare"
    assert qualified_agent("mock", "") == "mock"
