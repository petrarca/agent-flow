"""opencode runner — named agents via --agent, NDJSON event stream via --format json.

The ONE place opencode's CLI surface and wire format are known: build_command
(argv) and parse_event (stdout line -> neutral Event). The `_opencode_*` helpers
lift opencode's tool-event shape onto the neutral display fields; info() and
preflight_checks() introspect the opencode binary/config relative to agent_dir.

stderr capture: build_command sets capture_stderr=True and adds --print-logs
--log-level ERROR so opencode emits only ERROR-level diagnostic lines on stderr
(zero lines on a successful run). parse_stderr_line extracts the actionable
error= and ref= fields from those lines so SubprocessExecutor can enrich its
no-sidecar diagnostics with the real cause instead of the vague "Unexpected
server error" opencode emits on stdout.
"""

from __future__ import annotations

import json
import re
import shlex
import shutil
from pathlib import Path

from agent_flow.runners.base import MODE_PROCESS, TRANSPORT_SUBPROCESS, AgentInvocation, AgentRunnerInfo, Event, LaunchSpec, RunnerSpec

# Regex to extract key=value pairs for `error` and `ref` from an opencode
# stderr ERROR line.  opencode's logfmt formatter (logging.ts) quotes values
# with JSON.stringify when they contain spaces/=/"/\, bare otherwise.  We only
# need these two fields — no full logfmt parser required.
_STDERR_FIELD = re.compile(r'\b(error|ref)=("(?:[^"\\]|\\.)*"|[^\s]+)')


class OpenCodeRunner:
    """opencode: named agents via --agent; NDJSON event stream via --format json."""

    name = "opencode"

    def spec(self) -> RunnerSpec:
        """Static identity: the opencode runtime, process mode, subprocess transport."""
        return RunnerSpec(
            runtime="opencode",
            mode=MODE_PROCESS,
            transport=TRANSPORT_SUBPROCESS,
            name=self.name,
            needs_endpoint=False,
        )

    def build_verdict_preamble(self, agent: str, control_file: str, result_schema: dict | None = None) -> str:
        """The completion-protocol instruction block: write the control sidecar.

        opencode agents report their verdict by writing a JSON control file with
        the Write tool. This delegates to the shared `build_control_preamble`
        helper — the sidecar instruction is runtime-agnostic (any runtime with a
        Write tool + filesystem), so opencode simply reuses it. The subprocess
        AND remote opencode runners share this preamble; they differ only in how
        the executor reads the file back (off disk vs over the file API).
        """
        from agent_flow.core.control_protocol import build_control_preamble

        return build_control_preamble(agent, control_file, result_schema)

    def build_command(self, inv: AgentInvocation) -> LaunchSpec:
        # opencode identity lives in the agent .md; we pass --agent + work order.
        # --auto auto-approves permissions not explicitly denied: a headless,
        # orchestrated runner is unattended, so writes into a fresh workspace
        # (which opencode otherwise flags as an "external directory" and
        # auto-REJECTS) must be allowed. The agent .md still explicitly denies
        # bash/webfetch, so --auto only greenlights the tools the agent permits.
        # Only force a model when one was explicitly configured (param/env/CLI/
        # programmatic). With no model, OMIT --model so opencode resolves it from
        # its own config/router — the library never hardcodes a model.
        # --print-logs --log-level ERROR: capture stderr separately (see
        # capture_stderr=True below). At ERROR level opencode emits zero lines on
        # a successful run; on failure it emits the real cause (e.g.
        # ProviderModelNotFoundError) that its stdout JSON deliberately obscures
        # behind "Unexpected server error". parse_stderr_line extracts those.
        flags = ["opencode", "run", "--agent", inv.agent, "--format", "json", "--auto", "--print-logs", "--log-level", "ERROR"]
        if inv.model:
            flags += ["--model", inv.model]
        # --dir points opencode at the project where .opencode/agent lives, so
        # agents resolve regardless of the process cwd (opencode chdir's into it).
        if inv.agent_dir:
            flags += ["--dir", inv.agent_dir]
        # The prompt is the trailing positional. `display` shows everything BUT
        # the prompt (which is the whole composed prompt + control preamble —
        # hundreds of lines); we know it is the last element because we just put
        # it there, so no guessing.
        argv = [*flags, inv.prompt]
        display = shlex.join(flags) + f" <prompt: {len(inv.prompt)} chars>"
        # capture_stderr=True: SubprocessExecutor opens a separate stderr pipe
        # and routes each line through parse_stderr_line (below).
        return LaunchSpec(argv=argv, display=display, capture_stderr=True)

    def parse_event(self, line: str) -> Event:
        """Parse one opencode NDJSON line into supervision fields + the NEUTRAL view.

        This is the ONE place opencode's wire shape is interpreted. Besides the
        telemetry the engine needs (tokens/cost/is_terminal), we normalize the
        event into the runner-agnostic display fields (kind/title/detail/status)
        so the CLI can render it WITHOUT ever re-parsing opencode JSON. `raw` is
        still carried verbatim for --show-events / diagnostics.
        """
        stripped = line.strip()
        if not stripped.startswith("{"):
            return Event.none()
        try:
            ev = json.loads(stripped)
        except json.JSONDecodeError:
            return Event.none()
        part = ev.get("part") if isinstance(ev.get("part"), dict) else {}
        ptype = part.get("type") or ev.get("type") or "event"

        # A top-level runtime error (e.g. model unresolved, provider failure).
        # opencode emits it on stdout as {"type":"error","error":{name,data:{message,ref}}}
        # and exits non-zero without producing a control sidecar. Surface it so
        # the supervisor can report WHY the run has no sidecar.
        if ptype == "error":
            err = ev.get("error") if isinstance(ev.get("error"), dict) else {}
            data = err.get("data") if isinstance(err.get("data"), dict) else {}
            name = err.get("name") or "error"
            msg = data.get("message") or ""
            ref = data.get("ref")
            summary = f"{name}: {msg}".strip().rstrip(":").strip()
            if ref:
                summary = f"{summary} (ref {ref})"
            return Event(raw=stripped, kind="error", title=summary, error=summary or name)

        if ptype in ("step-start", "step_start"):
            return Event(raw=stripped, kind="step_start")
        if ptype in ("step-finish", "step_finish"):
            tokens = int((part.get("tokens") or {}).get("total") or 0)
            cost = float(part.get("cost") or 0.0)
            return Event(tokens=tokens, cost=cost, is_terminal=part.get("reason") == "stop", raw=stripped, kind="step_end")
        if ptype == "tool":
            title, detail, status, diff, added, removed = _opencode_tool_view(part)
            return Event(raw=stripped, kind="tool", title=title, detail=detail, status=status, diff=diff, added=added, removed=removed)
        if ptype == "text":
            text = " ".join((part.get("text") or "").split())
            return Event(raw=stripped, kind="text", title=text)
        # A real event we don't specially render: keep it displayable via `title`.
        return Event(raw=stripped, kind="other", title=str(ptype))

    def parse_stderr_line(self, line: str) -> str | None:
        """Extract an actionable error string from one opencode stderr line, or None.

        Called by SubprocessExecutor for each stderr line when capture_stderr is
        True.  Only ERROR-level lines are interesting (--log-level ERROR means
        nothing else arrives); within those, only lines that carry an `error=`
        field contain the real cause.  A `ref=` field, when present, is appended
        so the message can be correlated with the opaque ref in the stdout JSON
        error event.

        Returns None for lines that carry no actionable information (e.g. the
        secondary "share subscriber failed" ERROR line that has no error= field).
        """
        if "level=ERROR" not in line:
            return None
        fields: dict[str, str] = {}
        for m in _STDERR_FIELD.finditer(line):
            key, val = m.group(1), m.group(2)
            if val.startswith('"'):
                try:
                    val = json.loads(val)
                except json.JSONDecodeError:
                    val = val.strip('"')
            fields[key] = val
        error = fields.get("error")
        if not error:
            return None
        ref = fields.get("ref")
        return f"{error} (ref {ref})" if ref else error

    def preflight_checks(self, agent_dir: str | Path | None) -> list:
        """opencode-specific pre-conditions: binary on PATH, not nested, agent layout."""
        from agent_flow.preflight import Check

        checks: list[Check] = [_opencode_installed_check(), _not_nested_check()]
        # opencode resolves agents from `<agent_dir>/.opencode/agent*`.
        if agent_dir:
            oc = Path(agent_dir) / ".opencode"
            if not (oc.is_dir() and any(oc.glob("agent*"))):
                checks.append(Check("opencode-agent-layout", False, True, f"no .opencode/agent* under {agent_dir} (opencode --dir target)."))
            else:
                checks.append(Check("opencode-agent-layout", True, True, f"agent definitions at {oc}"))
        return checks

    def info(self, agent_dir: str | Path | None = None) -> AgentRunnerInfo:
        """Best-effort diagnostics: opencode version + its config AS RESOLVED IN agent_dir.

        opencode resolves its config (model, MCP tools, providers) relative to the
        directory it runs in (it walks up from --dir to the git root, merging
        opencode.json + global). So the resolved model and tools are a PER-DIR
        (hence per-node) property: the same runner in a different agent_dir can see
        a different model and a different tool set. We therefore introspect with
        cwd=agent_dir. With no agent_dir, we report only version/availability.
        """
        path = shutil.which("opencode")
        if not path:
            return AgentRunnerInfo(name=self.name, available=False, detail="opencode not found on PATH")
        version = _run_text(["opencode", "--version"])
        if not agent_dir:
            return AgentRunnerInfo(
                name=self.name, available=True, version=version, detail=f"found at {path}; pass agent_dir for resolved model/tools"
            )
        cfg = _opencode_debug_config(cwd=str(agent_dir))
        model = cfg.get("model")
        tools = sorted(k for k, v in (cfg.get("mcp") or {}).items() if isinstance(v, dict) and v.get("enabled"))
        return AgentRunnerInfo(
            name=self.name,
            available=True,
            version=version,
            resolved_model=model,
            tools=tools,
            detail=f"found at {path}; config resolved in {agent_dir}",
        )


# Per-tool input field carrying the human "target" (the file, command, pattern,
# url, …). opencode also sets `state.title` which we prefer; this is the fallback
# for tools/versions that don't. Ordered by likelihood so the first present wins.
_OPENCODE_TOOL_TARGET_KEYS = ("filePath", "command", "pattern", "url", "path", "query", "name", "description")

# Compact per-tool metadata hint (opencode's state.metadata), rendered as a
# secondary "detail". Only a few scalar keys are worth one line.
_OPENCODE_TOOL_META_KEYS = (("matches", "matches"), ("count", "matches"), ("exit", "exit"))


def _opencode_tool_view(part: dict) -> tuple[str, str, str, str, int, int]:
    """Map an opencode `tool` part onto the neutral tool fields.

    Returns (title, detail, status, diff, added, removed). This is pure field
    EXTRACTION — it lifts opencode's shape onto the neutral one and never renders
    or computes (no `+A/-D` formatting, no diff math; the CLI does that).

    title  = "<tool> <target>" — prefer opencode's own state.title; else the
             tool name plus the first present target input field.
    detail = a compact non-diff hint ("12 matches", "exit 0") when available.
    status = "running" | "completed" | "error" | "" (from state.status/error).
    diff/added/removed = the file change (from metadata.diff + filediff.*).
    """
    tool = part.get("tool", "tool")
    state = part.get("state") if isinstance(part.get("state"), dict) else {}
    inp = state.get("input") if isinstance(state.get("input"), dict) else {}
    meta = state.get("metadata") if isinstance(state.get("metadata"), dict) else {}

    # Build the "target" part: opencode's own state.title if present (grep->the
    # pattern, edit->"Edit <file>", …), else the first present input field.
    target = (state.get("title") or "").strip()
    if not target:
        target = next((str(inp[k]) for k in _OPENCODE_TOOL_TARGET_KEYS if inp.get(k)), "")

    # Always LEAD with the tool name so every tool line reads uniformly
    # ("read <path>", "grep <pattern>", "edit <file>"). opencode's state.title is
    # inconsistent — for some tools it already embeds the verb (edit -> "Edit
    # <file>"), for others it is just the bare target (read -> "<path>"). Only
    # prefix the tool name when the target does not already start with it, so we
    # don't produce "edit Edit <file>".
    if target and target.lower().startswith(tool.lower()):
        title = target
    else:
        title = f"{tool} {target}".rstrip()

    detail = _opencode_tool_detail(meta)
    diff, added, removed = _opencode_tool_diff(meta)

    status = str(state.get("status") or "")
    if state.get("error"):
        status = "error"
    return title, detail, status, diff, added, removed


def _opencode_tool_diff(meta: dict) -> tuple[str, int, int]:
    """Lift an opencode edit/write file change onto neutral (diff, added, removed).

    Pure extraction: the patch text from metadata.diff, the counts from
    metadata.filediff.{additions,deletions}. No parsing/computing.
    """
    diff = meta.get("diff") if isinstance(meta.get("diff"), str) else ""
    filediff = meta.get("filediff") if isinstance(meta.get("filediff"), dict) else {}
    if not diff and isinstance(filediff.get("patch"), str):
        diff = filediff["patch"]
    added = filediff.get("additions")
    removed = filediff.get("deletions")
    return diff, int(added) if isinstance(added, (int, float)) else 0, int(removed) if isinstance(removed, (int, float)) else 0


def _opencode_tool_detail(meta: dict) -> str:
    """A compact NON-DIFF hint from an opencode tool's state.metadata, or "".

    grep/glob match counts and bash exit codes. The diff stat is handled
    separately (as structured added/removed) — this stays presentation-free.
    """
    # grep/glob match counts, bash exit codes. (Diff stats are structured
    # added/removed fields, formatted by the CLI — not here.)
    for src_key, label in _OPENCODE_TOOL_META_KEYS:
        val = meta.get(src_key)
        if isinstance(val, (int, float)):
            return f"{int(val)} {label}" if label == "matches" else f"{label} {int(val)}"
    return ""


def _run_text(cmd: list[str], timeout: float = 10.0) -> str | None:
    """Run a command and return its stripped stdout first line, or None on any error."""
    import subprocess

    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)
    except OSError, subprocess.SubprocessError:
        return None
    line = (out.stdout or "").strip().splitlines()
    return line[0].strip() if line else None


def _opencode_debug_config(cwd: str | None = None) -> dict:
    """`opencode debug config` (run in `cwd`) parsed to a dict; {} on any failure.

    Run with cwd=agent_dir: opencode resolves config relative to where it runs
    (there is no --dir flag on `debug config`), so the cwd determines the model
    and enabled MCP tools reported.
    """
    import subprocess

    try:
        out = subprocess.run(["opencode", "debug", "config"], capture_output=True, text=True, timeout=15.0, check=False, cwd=cwd)
        return json.loads(out.stdout)
    except OSError, subprocess.SubprocessError, json.JSONDecodeError:
        return {}


def _opencode_installed_check():
    from agent_flow.preflight import Check

    path = shutil.which("opencode")
    if path:
        return Check("opencode-installed", True, True, f"found at {path}")
    return Check("opencode-installed", False, True, "opencode not found on PATH. Install it and ensure `opencode` is executable.")


def _not_nested_check():
    import os

    from agent_flow.preflight import Check

    if os.environ.get("OPENCODE") == "1":
        return Check(
            "not-nested-session", False, True, "Running inside an opencode session (OPENCODE=1). Start from a normal shell, outside opencode."
        )
    return Check("not-nested-session", True, True, "not inside an opencode session")
