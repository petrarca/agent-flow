---
type: Research
title: In-memory filesystem for the mock path — POC findings (fsspec / UPath)
description: Empirical findings from a /tmp POC probing how to swap the mock agent's real-disk file I/O for an in-memory filesystem, so mock flow tests can move from integration back to unit. Covers the three disk touchers, raw fsspec MemoryFileSystem vs universal-pathlib UPath, the class-level-store isolation trap, and the near-drop-in path-like seam. Reference evidence for the FS-seam design.
tags: [agent-flow, research, filesystem, fsspec, upath, mock-agent, testing]
status: findings
---

# In-memory filesystem for the mock path — POC findings

Goal probed: make a `mock_agents=True` run write/read through an **in-memory
filesystem** instead of real disk, so the mock-based flow tests (today
integration tests, because they touch `tmp_path`) can move back to **unit** tests
— fast, hermetic, no disk.

The POC lives under `/tmp/fs-poc` (throwaway `uv run` scripts). Findings below.

## The three disk touchers in the mock path (and only these)

Traced against the current source. In a `mock_agents=True` run exactly three
components hit the filesystem, and they meet through one value — **`run_dir`**:

1. **`MockAgentContext.write_file` / `read_file`** (`runners/mock_exec.py`) —
   `p.parent.mkdir(parents=True)`, `p.write_text`, `p.read_text`. The simulated
   artifacts (the files a real agent would write).
2. **`MockExecutor.run` sidecar write** (`runners/mock_exec.py`) —
   `run_dir.mkdir(parents=True)` + `control_file.write_text(json.dumps(control))`.
   The `<node>.control.json` a real out-of-process runner would write.
3. **`require_file` → `signals.produced(path)`** (`gates/`) — `path.exists()` and
   `path.stat().st_size > 0`. The gate reading back what the mock wrote.

**NOT touchers in the mock path** (important — bounds the seam):

- `assemble_result` (`runners/executor.py`) takes the `control` **dict** directly;
  the MockExecutor does NOT re-read the sidecar it wrote. So the sidecar write is
  write-only (on-disk traceability), and the harvest is in-memory already.
- `_read_sidecar` / `_sidecar_probe` / supervision (`runners/supervision.py`) are
  the **subprocess** path only. A subprocess (`opencode`) writes to real disk; you
  cannot fake the FS out from under an external process. The seam must therefore be
  bounded to the **mock / in-process + gate** path, never the subprocess path.

## Approach A — raw fsspec `MemoryFileSystem`: the isolation trap

`MemoryFileSystem.store` is a **class attribute** — a single process-global store.
`fsspec.filesystem("memory", skip_instance_cache=True)` returns a *new instance*
but the **same store**, so two "independent" runs bleed into each other:

```
R4 isolation (B must NOT see A's file): False       # FAILED
R4b store shared across instances?:     True         # fsA.store is fsB.store
```

This is the make-or-break gotcha. The store is global; a fresh instance is not a
fresh filesystem. Three ways to get real isolation on top of it:

1. **Unique root subtree per run** on the shared store (`/{uuid}/…`), teardown by
   `fs.rm(root, recursive=True)`. Simple; accepts the shared store.
2. **Throwaway subclass** with its own `store = {}` per run — true store isolation.
3. `MemoryFileSystem()` direct instances still share the store (Option 3 confirmed
   this: a second `MemoryFileSystem()` sees the first's files).

All three work; option 1 is the least magic.

The operational surface we need maps cleanly onto fsspec: `open(p,"w"/"r")` /
`pipe_file` / `cat_file` (write/read), `exists`, `info(p)["size"]` (for the
non-empty check). Empty-file and missing-file both correctly yield
`produced == False`.

## Approach B — `universal-pathlib` (UPath): a near-drop-in

The in-house precedent is **sonnet-storage**, which uses **UPath**
(`universal-pathlib`) over fsspec, not raw `AbstractFileSystem`. UPath returns a
`pathlib.Path`-compatible object that dispatches to an fsspec backend by protocol
(`file://`, `memory://`, a custom `db://`).

The decisive POC: the three touchers' **exact code runs unchanged** whether
`run_dir` is a `PosixPath` or a `UPath("memory://…")`:

```
run_dir / rel                       # join            — works on both
p.parent.mkdir(parents=True, ...)   # auto-parents    — works on both
p.write_text(...) / p.read_text()   # write/read      — works on both
path.exists() and path.stat().st_size > 0             — works on both
```

So the seam is simply **"`run_dir` is a `Path`-like object"** — not a new FS API
threaded through every call site. Local runs get a `PosixUPath` (a real
`PosixPath`-like that writes to actual disk — confirmed `Path(str(p)).exists()`);
mock/unit runs get a `MemoryPath`.

Isolation: give each run a **unique netloc** (`memory://run-<uuid>/…`); distinct
netlocs are distinct subtrees on the shared memory store. Per-run teardown removes
the run's netloc subtree; a full-suite teardown leaves **no leftovers**
(`mem.find("/")` == `[]`). End-to-end two-"test" isolation + teardown verified.

### The one code change UPath forces

`MockAgentContext._resolve` does `Path(resolve_template(path, tmpl, strict=True))`
— hardcoding `PosixPath`. That is the single spot that breaks for `memory://`. It
must become **`UPath(resolved)`** (protocol-aware), which serves BOTH local and
memory from one code path:

- `"{PRODUCT_REPOS_ROOT}/{PRODUCT_KEY}/…/x.json"` where the anchor is a
  `memory://…` URL → resolves to a `MemoryPath` and lands in-memory.
- `"{run_dir}/.rerun_once_marker"` where `run_dir` is a memory URL → same.
- The same templates with local (`/tmp/…`) anchors → a `PosixUPath` writing real
  disk. Confirmed.

For the mock unit test, the run's anchors (`run_dir`, `PRODUCT_REPOS_ROOT`,
`capibara_home`) are memory URLs rooted at the same per-run netloc, so the
absolute paths the mocks build and the `require_file` gate checks resolve onto the
same in-memory subtree.

## Recommendation (design input, not yet built)

- **Seam = "`run_dir` is `Path`-like" via UPath**, defaulting to a local
  `UPath("file://…")` / `Path`, opt-in `UPath("memory://…")` for mock unit tests.
  Minimal blast radius: the touchers barely change; only the `Path(...)` in
  `_resolve` becomes `UPath(...)`.
- **Core, not an extra.** Mock agents are a core capability; the FS that makes them
  fast is core too. `universal-pathlib` is a small pure-python dep over fsspec
  (already an indirect dep). Alternatively a hand-written 2-adapter seam avoids the
  dep entirely — but UPath's *pathlib compatibility* is exactly what keeps the
  change tiny, which the hand-written route would forfeit (it reintroduces an FS
  API at every call site). The dep buys the drop-in.
- **Isolation contract for tests**: one unique `memory://run-<uuid>/…` per run;
  teardown removes that netloc subtree. The store is process-global, so the netloc
  IS the isolation boundary — document it so a suite never shares a netloc.
- **Bound the reach**: mock/in-process writes + sidecar + gate reads. Explicitly
  NOT the subprocess/supervision path (can't fake an external process's disk).

### pyfakefs (rejected for this)

`pyfakefs` patches stdlib `open`/`pathlib` under the test — zero library change —
but it is a test-time monkeypatch, not a runtime capability a consumer can select,
and the stated goal ("swap, optionally, with an in-memory FS") is a runtime seam.
UPath gives the runtime seam with nearly the same small footprint.

## Outcome (implemented)

The recommendation shipped: `universal-pathlib` is a core dep; `resolve_run_dir`
returns a `UPath` for `memory://` (and defaults a no-run_dir mock run to a unique
`memory://run-<id>/` root); `MockAgentContext._resolve` builds a `UPath`; and
`require_file` joins `run_dir` only for bare-relative paths (an absolute or
`memory://` path is used verbatim, since a plain `run_dir / "memory://…"` would
concatenate into nonsense). The seam is "`run_dir` is a `Path`-like object," so
the mock ctx, executor sidecar, and gates are otherwise unchanged. See the
[mock-agent concept](../design/mock-agent.md#in-memory-filesystem-the-default-for-a-mock-run)
and the [testing guide](../usage/testing.md#in-memory-runs-integration-test-to-unit-test).
