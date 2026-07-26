#!/usr/bin/env python3
"""Domain-free subprocess stub — a dumb opencode-shaped process for tests ONLY.

This is NOT the mock agent system. Programmable, deterministic agent simulation
lives in `mock_agent` behaviours run in-process by `MockExecutor` (the
`--mock-agents` mode; see runners/mock_exec.py and docs design/mock-agent.md).

This stub exists solely to exercise `SubprocessExecutor`'s spawn / supervise-by-
liveness / kill-on-stale machinery, which by nature can only be tested with a
real child process. It holds NO agent names and NO domain logic — it does exactly
what it is told on the command line:

  --emit '<json>'   write the given JSON object to CONTROL_FILE as the sidecar,
                    then exit 0. (If the JSON status is not ok/verified the
                    caller's SubprocessExecutor treats it as a content failure.)
  --sleep           sleep effectively forever, writing no sidecar — so the
                    supervisor must kill it on the idle deadline (the stale path).
  (neither)         write a minimal {"status": "ok"} sidecar and exit.

The sidecar path is read from the `CONTROL_FILE: <path>` line in --prompt
(the control preamble SubprocessExecutor prepends), matching how a real agent
learns where to write.
"""

from __future__ import annotations

import argparse
import json
import re
import time
from pathlib import Path


def _control_file(prompt: str) -> Path | None:
    m = re.search(r"^CONTROL_FILE:\s*(.+)$", prompt, re.MULTILINE)
    return Path(m.group(1).strip()) if m else None


def _write_sidecar(prompt: str, payload: dict) -> None:
    path = _control_file(prompt)
    if path is None:
        return  # no control file -> write nothing (SubprocessExecutor -> error)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--agent", required=True)  # accepted, ignored (no domain routing)
    ap.add_argument("--prompt", required=True)
    ap.add_argument("--model", required=False, default="")
    ap.add_argument("--emit", required=False, default="")  # JSON envelope to write
    ap.add_argument("--sleep", action="store_true")  # hang forever (test the kill path)
    ap.add_argument("--print", required=False, default="")  # print a raw stdout line (no sidecar)
    ap.add_argument("--exit-code", type=int, default=0)  # exit with this code
    args = ap.parse_args()

    if args.sleep:
        time.sleep(100000)  # never returns; the supervisor kills us on idle
        return

    # A raw stdout line with NO sidecar (tests the no-verdict diagnostic + exit
    # code routing). Used to simulate a runtime that fails to start.
    if args.print:
        print(args.print, flush=True)
        raise SystemExit(args.exit_code)

    if args.exit_code:
        raise SystemExit(args.exit_code)

    payload = json.loads(args.emit) if args.emit else {"status": "ok", "agent": args.agent}
    _write_sidecar(args.prompt, payload)


if __name__ == "__main__":
    main()
