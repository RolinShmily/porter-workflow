"""Logging contract: stderr only, and records must actually arrive.

The propagation bug these tests guard against was invisible for a whole refactor
phase: ``configure()`` installed a handler, ``get_logger()`` returned a logger,
``--log-level`` parsed — and every engine record was silently discarded because
child loggers had ``propagate = False``. Python's ``lastResort`` handler then
dumped records to stderr unformatted, which *looked* like logging working.

So these tests assert on the configured handler's output, not on stderr at large.
"""

from __future__ import annotations

import io
import logging

import pytest

from porter.logging import (
    DEFAULT_FORMAT,
    LOG_LEVEL_ENV,
    ROOT_LOGGER_NAME,
    configure,
    get_logger,
    reset,
    resolve_level,
)


@pytest.fixture
def sink() -> io.StringIO:
    """A configured engine whose output lands in a buffer we can inspect."""
    buffer = io.StringIO()
    reset()
    configure(level="DEBUG", stream=buffer)
    return buffer


class TestPropagation:
    """The regression: child records must reach the engine root's handler."""

    def test_child_logger_records_reach_the_configured_handler(self, sink: io.StringIO) -> None:
        get_logger("platforms.ydl").error("this must be captured")
        assert "this must be captured" in sink.getvalue(), (
            "engine log records are being discarded — check logger.propagate"
        )

    def test_deeply_nested_logger_reaches_the_handler(self, sink: io.StringIO) -> None:
        get_logger("asr.bcut.internal").warning("nested")
        assert "nested" in sink.getvalue()

    def test_records_use_the_configured_format(self, sink: io.StringIO) -> None:
        get_logger("platforms.ydl").error("formatted")
        line = sink.getvalue().strip()
        assert "ERROR" in line
        assert f"{ROOT_LOGGER_NAME}.platforms.ydl" in line

    def test_engine_root_does_not_reach_the_global_root(self, sink: io.StringIO) -> None:
        """The engine root is the boundary; a host app's config must not see it."""
        assert logging.getLogger(ROOT_LOGGER_NAME).propagate is False

    def test_child_loggers_do_propagate(self) -> None:
        assert get_logger("subtitles.srt").propagate is True

    def test_get_logger_repairs_a_previously_broken_child(self) -> None:
        """A logger reused by name from an older build must be fixed up."""
        logging.getLogger(f"{ROOT_LOGGER_NAME}.legacy").propagate = False
        assert get_logger("legacy").propagate is True

    def test_a_null_handler_on_a_child_does_not_block_the_parent(self) -> None:
        """logging stops walking up once a handler is found, so children must stay bare."""
        child = get_logger("subtitles.ass")
        assert not any(isinstance(h, logging.NullHandler) for h in child.handlers)


class TestNaming:
    def test_bare_name_is_namespaced(self) -> None:
        assert get_logger("asr.bcut").name == f"{ROOT_LOGGER_NAME}.asr.bcut"

    def test_already_qualified_name_is_kept(self) -> None:
        assert get_logger(f"{ROOT_LOGGER_NAME}.x").name == f"{ROOT_LOGGER_NAME}.x"

    def test_none_returns_the_engine_root(self) -> None:
        assert get_logger().name == ROOT_LOGGER_NAME

    def test_root_name_returns_the_engine_root(self) -> None:
        assert get_logger(ROOT_LOGGER_NAME).name == ROOT_LOGGER_NAME

    def test_repeated_calls_return_the_same_logger(self) -> None:
        assert get_logger("a.b") is get_logger("a.b")


class TestConfigure:
    def test_defaults_to_stderr(self, capsys: pytest.CaptureFixture[str]) -> None:
        reset()
        configure(level="INFO")
        get_logger("x").warning("to stderr")

        captured = capsys.readouterr()
        assert captured.out == "", "engine logging reached stdout"
        assert "to stderr" in captured.err

    def test_is_idempotent(self, sink: io.StringIO) -> None:
        configure(level="INFO")
        configure(level="INFO")
        get_logger("x").error("once")
        assert sink.getvalue().count("once") == 1

    def test_force_replaces_the_handler(self, sink: io.StringIO) -> None:
        second = io.StringIO()
        configure(level="INFO", stream=second, force=True)
        get_logger("x").error("moved")
        assert second.getvalue().count("moved") == 1
        assert "moved" not in sink.getvalue()

    def test_level_is_applied(self, sink: io.StringIO) -> None:
        # force=True reinstalls with the *default* stream, so pass the sink
        # explicitly to keep the assertion on the buffer rather than stderr.
        configure(level="ERROR", stream=sink, force=True)
        logger = get_logger("x")
        logger.info("dropped")
        logger.error("kept")
        assert "dropped" not in sink.getvalue()
        assert "kept" in sink.getvalue()

    def test_unconfigured_engine_is_silent_not_noisy(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Without configure(), a record must vanish rather than hit lastResort."""
        reset()
        get_logger("x").error("should not appear")
        captured = capsys.readouterr()
        assert captured.err == ""
        assert captured.out == ""


class TestReset:
    def test_removes_handlers(self, sink: io.StringIO) -> None:
        reset()
        assert not logging.getLogger(ROOT_LOGGER_NAME).handlers

    def test_after_reset_records_are_discarded(self, sink: io.StringIO) -> None:
        reset()
        get_logger("x").error("after reset")
        assert sink.getvalue() == ""


class TestResolveLevel:
    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            ("debug", logging.DEBUG),
            ("INFO", logging.INFO),
            ("warning", logging.WARNING),
            ("error", logging.ERROR),
            (logging.CRITICAL, logging.CRITICAL),
        ],
    )
    def test_names_and_numbers(self, value, expected: int) -> None:
        assert resolve_level(value) == expected

    def test_none_defaults_to_info(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(LOG_LEVEL_ENV, raising=False)
        assert resolve_level(None) == logging.INFO

    def test_env_var_is_honoured(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(LOG_LEVEL_ENV, "debug")
        assert resolve_level(None) == logging.DEBUG

    def test_unknown_name_falls_back_to_info(self) -> None:
        assert resolve_level("nonsense") == logging.INFO


def test_default_format_is_stderr_friendly() -> None:
    """No colour codes: the output is a log file and an MCP client's stderr."""
    assert "\x1b[" not in DEFAULT_FORMAT
