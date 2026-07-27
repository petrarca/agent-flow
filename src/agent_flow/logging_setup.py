"""Logging setup — loguru as the sink, stdlib logging intercepted into it.

agent-flow follows the house standard (loguru), but its own engine/backends log
through the stdlib `logging` module under the ``agent_flow`` logger — and the
opt-in Prefect backend logs through Prefect's stdlib-based `get_run_logger()`.
Rather than rewrite every call site, `setup_logging` installs an
`InterceptHandler` that forwards ALL stdlib `logging` records into loguru, so a
single `--log-level` (or `AGENT_FLOW_LOG_LEVEL`) controls everything and the
records land in loguru's formatted, colorized stderr sink.

This is intentionally opt-in: importing agent-flow configures NO handlers (a
library must not hijack the root logger). The CLI calls `setup_logging(level)`
once at startup; a programmatic consumer that already runs loguru does nothing,
or calls `setup_logging(...)` itself. loguru writes to **stderr**, leaving the
CLI's rich display (stdout) untouched.
"""

from __future__ import annotations

import logging
import sys

from loguru import logger

# The library's own stdlib logger name (engine, node_builder, backends all use it).
LIBRARY_LOGGER = "agent_flow"


class InterceptHandler(logging.Handler):
    """Forward stdlib `logging` records to loguru (the house pattern).

    Maps the stdlib level name to loguru's, and walks back the call stack so the
    loguru record points at the ORIGINAL caller, not this handler.
    """

    def emit(self, record: logging.LogRecord) -> None:
        # Map to a loguru level by name; fall back to the numeric level.
        try:
            level: str | int = logger.level(record.levelname).name
        except ValueError:
            level = record.levelno

        # Find the frame where the log call originated (skip stdlib logging frames).
        frame, depth = logging.currentframe(), 2
        while frame and frame.f_code.co_filename == logging.__file__:
            frame = frame.f_back
            depth += 1

        logger.opt(depth=depth, exception=record.exc_info).log(level, record.getMessage())


def setup_logging(log_level: str = "INFO") -> None:
    """Configure loguru's stderr sink and redirect stdlib logging into it.

    Idempotent: safe to call more than once (it removes loguru's existing sinks
    first). `log_level` is case-insensitive (DEBUG/INFO/WARNING/ERROR/CRITICAL).
    """
    log_level = (log_level or "INFO").upper()

    logger.remove()  # drop loguru's default handler (and any prior setup_logging sink)
    logger.add(sys.stderr, level=log_level, colorize=True)

    # Redirect ALL stdlib logging into loguru: install the intercept on the root
    # (force=True clears any pre-existing handlers) and clear per-logger handlers
    # so nothing double-logs or bypasses the sink.
    logging.basicConfig(handlers=[InterceptHandler()], level=0, force=True)
    for name in list(logging.root.manager.loggerDict):
        lg = logging.getLogger(name)
        lg.handlers = []
        lg.propagate = True  # let records bubble to the root's InterceptHandler

    # The library's own logger emits at INFO/DEBUG for node lifecycle + supervision;
    # let it flow at the requested level (loguru's sink does the final filtering).
    logging.getLogger(LIBRARY_LOGGER).setLevel(logging.DEBUG if log_level in ("DEBUG", "TRACE") else logging.INFO)
