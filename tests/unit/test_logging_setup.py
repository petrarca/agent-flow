"""Unit tests for setup_logging — loguru sink + stdlib->loguru interception.

setup_logging mutates PROCESS-GLOBAL logging state (loguru sinks + the stdlib
root handlers), so every test here snapshots and restores that state; otherwise
it would leak into the rest of the suite.
"""

import logging

import pytest
from loguru import logger

from agent_flow.logging_setup import InterceptHandler, setup_logging


@pytest.fixture
def restore_logging():
    """Snapshot + restore the global logging state setup_logging rewires."""
    root = logging.getLogger()
    saved_handlers, saved_level = list(root.handlers), root.level
    saved_levels = {name: logging.getLogger(name).level for name in list(logging.root.manager.loggerDict)}
    yield
    root.handlers = saved_handlers
    root.setLevel(saved_level)
    for name, level in saved_levels.items():
        logging.getLogger(name).setLevel(level)
    logger.remove()


def _sink_messages() -> tuple[list[str], int]:
    """Add a capturing loguru sink; return (messages, sink_id)."""
    msgs: list[str] = []
    sink_id = logger.add(lambda m: msgs.append(m.record["message"]), level="DEBUG")
    return msgs, sink_id


def test_stdlib_records_are_intercepted_into_loguru(restore_logging):
    # The whole point: a plain `logging` call must land in loguru's sink.
    setup_logging("DEBUG")
    msgs, sink_id = _sink_messages()
    try:
        logging.getLogger("agent_flow").info("hello from stdlib")
    finally:
        logger.remove(sink_id)
    assert "hello from stdlib" in msgs


def test_root_handler_is_the_intercept_handler(restore_logging):
    setup_logging("INFO")
    assert any(isinstance(h, InterceptHandler) for h in logging.getLogger().handlers)


def test_noisy_third_party_loggers_are_pinned_to_warning(restore_logging):
    # --log-level DEBUG must stay focused on agent-flow, not asyncio internals.
    setup_logging("DEBUG")
    for noisy in ("asyncio", "anyio", "httpx", "httpcore", "urllib3"):
        assert logging.getLogger(noisy).level == logging.WARNING, noisy


def test_level_is_case_insensitive_and_defaults(restore_logging):
    # Accepts lowercase and an empty value (-> INFO) without raising.
    setup_logging("debug")
    setup_logging("")
    setup_logging("WARNING")


def test_is_idempotent_no_duplicate_sinks(restore_logging):
    # Calling twice must not double-log (it removes existing sinks first).
    setup_logging("DEBUG")
    setup_logging("DEBUG")
    msgs, sink_id = _sink_messages()
    try:
        logging.getLogger("agent_flow").warning("once")
    finally:
        logger.remove(sink_id)
    assert msgs.count("once") == 1
