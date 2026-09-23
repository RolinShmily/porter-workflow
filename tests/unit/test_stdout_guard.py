"""The stdout guard is what keeps a leaky dependency from killing the MCP server."""

from __future__ import annotations

import pytest

from porter_mcp.stdout_guard import StdoutViolation, guard_stdout, protect, violations


class TestStrictMode:
    """Strict mode is used in tests, so the defect fails at its source line."""

    def test_print_raises(self) -> None:
        with pytest.raises(StdoutViolation) as excinfo, guard_stdout(strict=True):
            print("oops")
        assert "oops" in excinfo.value.payload

    def test_violation_message_names_the_protocol(self) -> None:
        with pytest.raises(StdoutViolation, match="JSON-RPC"), guard_stdout(strict=True):
            print("x")


class TestPermissiveMode:
    """Production mode degrades instead of dying: divert, record, keep serving."""

    def test_write_is_diverted_not_swallowed(self, capsys: pytest.CaptureFixture[str]) -> None:
        with guard_stdout() as captured:
            print("diagnostic")
        assert "diagnostic" in "".join(captured)
        # Diverted to stderr, so the protocol stream stays clean.
        assert "diagnostic" in capsys.readouterr().err

    def test_stdout_is_clean_after_the_guarded_region(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        with guard_stdout():
            print("should not appear on stdout")
        assert capsys.readouterr().out == ""

    def test_stdout_is_restored_on_exception(self, capsys: pytest.CaptureFixture[str]) -> None:
        with pytest.raises(RuntimeError), guard_stdout():
            raise RuntimeError("boom")
        print("after")
        assert "after" in capsys.readouterr().out


class TestViolationLog:
    """``violations()`` records raw ``write`` payloads.

    ``print()`` issues ``write("text")`` and ``write("\n")`` separately, so the
    recorded list holds writes rather than lines. Join it to reconstruct output.
    """

    def test_violations_returns_and_clears(self) -> None:
        with guard_stdout():
            print("one")
        assert "".join(violations()) == "one\n"
        assert violations() == [], "the log must be cleared once read"

    def test_multiple_writes_preserve_order(self) -> None:
        with guard_stdout():
            print("a")
            print("b")
        assert "".join(violations()) == "a\nb\n"

    def test_log_is_per_thread(self) -> None:
        import threading

        seen: list[str] = []

        def worker() -> None:
            with guard_stdout():
                print("from thread")
            seen.append("".join(violations()))

        thread = threading.Thread(target=worker)
        thread.start()
        thread.join()

        assert seen == ["from thread\n"]
        assert violations() == [], "the main thread's log must stay clean"


class TestProtectDecorator:
    def test_sync_function(self, capsys: pytest.CaptureFixture[str]) -> None:
        @protect
        def noisy() -> int:
            print("from inside")
            return 7

        assert noisy() == 7
        assert capsys.readouterr().out == ""

    def test_async_function(self, capsys: pytest.CaptureFixture[str]) -> None:
        @protect
        async def noisy() -> str:
            print("from inside")
            return "ok"

        import asyncio

        assert asyncio.run(noisy()) == "ok"
        assert capsys.readouterr().out == ""

    def test_metadata_is_preserved(self) -> None:
        @protect
        def documented() -> None:
            """Docstring survives wrapping."""

        assert documented.__name__ == "documented"
        assert documented.__doc__ == "Docstring survives wrapping."
