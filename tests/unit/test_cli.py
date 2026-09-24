"""CLI wiring: argument parsing, the bare-URL shorthand, and exit codes."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from porter import __version__
from porter.events import JobState
from porter.jobs import process_marker
from porter.models.request import BurnMode
from porter_cli import render
from porter_cli.app import _inject_run, main

URL = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"


class TestBareUrlShorthand:
    """``porter <URL>`` must keep working — it is documented and in use."""

    def test_bare_url_becomes_run(self) -> None:
        assert _inject_run([URL, "-o", "out"]) == ["run", URL, "-o", "out"]

    def test_global_flag_before_url(self) -> None:
        # "run" is inserted after the global flag, so argparse still sees the
        # global option before the subcommand.
        assert _inject_run(["--config", "c.json", URL]) == [
            "--config",
            "c.json",
            "run",
            URL,
        ]

    def test_existing_subcommand_is_untouched(self) -> None:
        assert _inject_run(["inspect", URL]) == ["inspect", URL]

    def test_global_flag_alone_is_untouched(self) -> None:
        assert _inject_run(["--version"]) == ["--version"]

    def test_empty_argv_is_untouched(self) -> None:
        assert _inject_run([]) == []


class TestVersion:
    def test_version_flag_exits_zero(self, capsys: pytest.CaptureFixture[str]) -> None:
        with pytest.raises(SystemExit) as excinfo:
            main(["--version"])
        assert excinfo.value.code == 0
        assert __version__ in capsys.readouterr().out


class TestDispatch:
    def test_no_command_prints_help_and_reports_misuse(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert main([]) == render.EXIT_MISUSE
        assert "usage:" in capsys.readouterr().out

    def test_unimplemented_command_reports_misuse(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Skeleton commands fail loudly rather than silently doing nothing.

        ``config import-videocaptioner`` is the last stub. ``run`` and ``jobs``
        were the examples here while they were stubs, which is why this test names
        a stub rather than asserting that some stub exists.
        """
        assert main(["config", "import-videocaptioner"]) == render.EXIT_MISUSE
        err = capsys.readouterr().err
        assert "not implemented" in err
        # The message must be actionable on its own: it used to point the user at
        # an internal refactor section number, which is meaningless to them.
        assert "github.com/RolinShmily/porter-workflow/issues" in err

    def test_jobs_is_no_longer_a_stub(self, capsys: pytest.CaptureFixture[str]) -> None:
        """Regression guard: `porter jobs` must read the registry, not a stub."""
        assert main(["jobs"]) == render.EXIT_OK
        err = capsys.readouterr().err
        assert "not implemented" not in err
        assert "no running jobs" in err

    def test_run_is_no_longer_a_stub(self, capsys: pytest.CaptureFixture[str]) -> None:
        """Regression guard: `porter run` must reach the pipeline, not a stub.

        A bad URL that fails fast is enough -- the point is that the command
        attempts real work and reports a real failure instead of returning
        EXIT_MISUSE.
        """
        code = main(["run", "https://example.com/not-a-video", "--burn", "skip"])

        assert code == render.EXIT_ERROR
        assert "not implemented" not in capsys.readouterr().err


class TestConfigCommand:
    def test_list_masks_secrets(
        self, project_dir, capsys: pytest.CaptureFixture[str], monkeypatch
    ) -> None:
        monkeypatch.setenv("OPENAI_API_KEY", "sk-abcdefghijklmnop")
        assert main(["config", "list"]) == render.EXIT_OK

        out = capsys.readouterr().out
        assert "sk-abcdefghijklmnop" not in out, "the raw API key leaked to stdout"
        assert "sk-...mnop" in out

        json_start = out.index("{")
        payload = json.loads(out[json_start:])
        assert payload["llm"]["api_key"] == "sk-...mnop"

    def test_get_unknown_key_fails(self, project_dir, capsys) -> None:
        assert main(["config", "get", "llm.nope"]) == render.EXIT_ERROR
        assert "unknown configuration key" in capsys.readouterr().err

    def test_set_then_get_roundtrip(self, project_dir, tmp_path, capsys) -> None:
        target = tmp_path / "written.json"
        code = main(
            ["config", "set", "llm.model=some-model", "--file", str(target)]
        )
        assert code == render.EXIT_OK
        assert json.loads(target.read_text(encoding="utf-8"))["llm"]["model"] == "some-model"

    def test_set_requires_key_value_form(self, project_dir, capsys) -> None:
        assert main(["config", "set", "llm.model"]) == render.EXIT_MISUSE
        assert "KEY=VALUE" in capsys.readouterr().err


class TestInspectCommand:
    """``porter inspect`` — the command that had no implementation until P2.5.

    ``inspect_url`` is patched at its definition site; the command imports it
    inside ``run()``, so the patch takes effect at call time. No network.
    """

    @pytest.fixture
    def result(self):
        from porter.models.inspection import InspectionResult

        return InspectionResult(
            input_url=URL,
            canonical_url=URL,
            platform="youtube",
            is_valid=True,
            has_video=True,
            video_id="dQw4w9WgXcQ",
            title="A Test Video",
            safe_title="A_Test_Video",
            uploader="Someone",
            duration_seconds=212.0,
            width=1920,
            height=1080,
            is_vertical=False,
            has_subtitles=True,
        )

    def _patch(self, monkeypatch, result):
        monkeypatch.setattr(
            "porter.platforms.inspector.inspect_url", lambda *a, **k: result
        )

    def test_report_goes_to_stdout(self, monkeypatch, result, capsys, project_dir) -> None:
        """The report is the command's result, so it must be pipeable."""
        self._patch(monkeypatch, result)

        assert main(["inspect", URL]) == render.EXIT_OK

        captured = capsys.readouterr()
        assert "dQw4w9WgXcQ" in captured.out
        assert "A Test Video" in captured.out
        assert "Horizontal 16:9" in captured.out

    def test_headings_go_to_stderr(self, monkeypatch, result, capsys, project_dir) -> None:
        self._patch(monkeypatch, result)
        main(["inspect", URL])

        captured = capsys.readouterr()
        assert "PRE-FLIGHT PROBE" not in captured.out
        assert "PRE-FLIGHT PROBE" in captured.err

    def test_json_mode_emits_only_json_on_stdout(
        self, monkeypatch, result, capsys, project_dir
    ) -> None:
        self._patch(monkeypatch, result)

        assert main(["inspect", URL, "--json"]) == render.EXIT_OK

        payload = json.loads(capsys.readouterr().out)
        assert payload["platform"] == "youtube"
        assert payload["video_id"] == "dQw4w9WgXcQ"

    def test_unusable_link_exits_non_zero(
        self, monkeypatch, result, capsys, project_dir
    ) -> None:
        result.is_valid = False
        result.has_video = False
        result.error_message = "The provided post/link does not contain any video streams."
        self._patch(monkeypatch, result)

        assert main(["inspect", URL]) == render.EXIT_ERROR
        assert "does not contain any video" in capsys.readouterr().err

    def test_engine_error_becomes_a_clean_exit_code(
        self, monkeypatch, capsys, project_dir
    ) -> None:
        from porter.errors import ExtractionError

        def boom(*args, **kwargs):
            raise ExtractionError("network is down")

        monkeypatch.setattr("porter.platforms.inspector.inspect_url", boom)

        assert main(["inspect", URL]) == render.EXIT_ERROR
        captured = capsys.readouterr()
        assert "network is down" in captured.err
        assert "Traceback" not in captured.err, "the CLI must not leak a stack trace"

    def test_report_shows_the_canonical_url_not_the_raw_input(
        self, monkeypatch, capsys, project_dir
    ) -> None:
        """Raw and canonical differ whenever tracking params are present.

        The stripping itself lives in ``canonicalize`` and is covered by
        ``tests/regression/test_url_cleaning_port.py``; what this pins is that
        the CLI reports the canonical form rather than echoing what was typed.
        """
        from porter.models.inspection import InspectionResult

        seen: list[str] = []

        def capture(url, *args, **kwargs):
            seen.append(url)
            return InspectionResult(
                input_url=url,
                canonical_url="https://www.youtube.com/watch?v=dQw4w9WgXcQ&t=15s",
                platform="youtube",
                is_valid=True,
                has_video=True,
                video_id="dQw4w9WgXcQ",
            )

        monkeypatch.setattr("porter.platforms.inspector.inspect_url", capture)

        main(["inspect", f"{URL}&t=15s&utm_source=share"])

        out = capsys.readouterr().out
        assert "t=15s" in out
        assert "utm_source" not in out


class TestDoctorCommand:
    """``porter doctor`` — facts on stderr, ``--json`` alone on stdout."""

    @pytest.fixture
    def healthy(self, monkeypatch):
        from porter.doctor import CapabilityReport
        from porter.doctor.probes import Finding

        report = CapabilityReport(
            findings=[
                Finding.passed("ffmpeg", "ffmpeg & ffprobe", "found"),
                Finding.passed("libass", "libass (hardsub)", "ass filter present"),
            ]
        )
        monkeypatch.setattr("porter.doctor.probe_all", lambda *a, **k: report)
        return report

    def test_a_healthy_machine_exits_zero(self, healthy, project_dir) -> None:
        assert main(["doctor"]) == render.EXIT_OK

    def test_the_human_report_writes_nothing_to_stdout(self, healthy, capsys, project_dir) -> None:
        """--json must never be corruptible by a stray status line."""
        main(["doctor"])
        captured = capsys.readouterr()

        assert captured.out == ""
        assert "[OK  ]" in captured.err

    def test_json_mode_puts_only_json_on_stdout(self, healthy, capsys, project_dir) -> None:
        assert main(["doctor", "--json"]) == render.EXIT_OK

        payload = json.loads(capsys.readouterr().out)
        assert payload["ok"] is True
        assert payload["findings"][0]["key"] == "ffmpeg"

    def test_a_blocker_exits_non_zero(self, monkeypatch, capsys, project_dir) -> None:
        from porter.doctor import CapabilityReport
        from porter.doctor.probes import Finding

        report = CapabilityReport(
            findings=[
                Finding.blocked("ffmpeg", "ffmpeg & ffprobe", "not found", "ffmpeg"),
            ]
        )
        monkeypatch.setattr("porter.doctor.probe_all", lambda *a, **k: report)

        assert main(["doctor"]) == render.EXIT_ERROR
        assert "1 blocking issue" in capsys.readouterr().err

    def test_a_blocker_shows_its_remediation(self, monkeypatch, capsys, project_dir) -> None:
        """The whole point of the key/text split: the fix must reach the operator."""
        from porter.doctor import CapabilityReport
        from porter.doctor.probes import Finding

        report = CapabilityReport(
            findings=[
                Finding.blocked("ffmpeg", "ffmpeg & ffprobe", "not found", "ffmpeg"),
            ]
        )
        monkeypatch.setattr("porter.doctor.probe_all", lambda *a, **k: report)
        main(["doctor"])

        err = capsys.readouterr().err
        assert "apt install ffmpeg" in err
        assert "[FAIL]" in err

    def test_a_degradation_warns_but_still_exits_zero(
        self, monkeypatch, capsys, project_dir
    ) -> None:
        """No GPU must not fail a gate the way no ffmpeg does."""
        from porter.doctor import CapabilityReport
        from porter.doctor.probes import Finding

        report = CapabilityReport(
            findings=[
                Finding.passed("ffmpeg", "ffmpeg & ffprobe", "found"),
                Finding.degraded("js_runtime", "JavaScript runtime", "none found", "js_runtime"),
            ]
        )
        monkeypatch.setattr("porter.doctor.probe_all", lambda *a, **k: report)

        assert main(["doctor"]) == render.EXIT_OK
        err = capsys.readouterr().err
        assert "[WARN]" in err
        assert "deno.land" in err, "the remediation should be shown"
        assert "1 degraded" in err

    def test_status_markers_are_ascii(self, monkeypatch, capsys, project_dir) -> None:
        """Under LANG=C a non-ASCII glyph raises UnicodeEncodeError."""
        from porter.doctor import CapabilityReport
        from porter.doctor.probes import Finding

        report = CapabilityReport(
            findings=[
                Finding.passed("a", "A"),
                Finding.degraded("b", "B", "d", "js_runtime"),
                Finding.blocked("c", "C", "d", "ffmpeg"),
            ]
        )
        monkeypatch.setattr("porter.doctor.probe_all", lambda *a, **k: report)
        main(["doctor"])

        assert capsys.readouterr().err.isascii()

    def test_verbose_shows_details_for_passing_checks(self, healthy, capsys, project_dir) -> None:
        main(["doctor"])
        assert "ass filter present" not in capsys.readouterr().err

        main(["doctor", "--verbose"])
        assert "ass filter present" in capsys.readouterr().err


class TestStdoutEncodingSafety:
    """A video title is remote data and is routinely non-ASCII.

    ``sys.stdout`` defaults to ``errors="strict"`` while ``sys.stderr`` defaults to
    ``backslashreplace`` (PEP 528). So under ``PYTHONUTF8=0`` on a C locale,
    ``sys.stdout.encoding`` is ASCII and printing a Chinese title raised
    ``UnicodeEncodeError`` -- aborting the very command that had already done the
    work.
    """

    def test_make_stdout_safe_installs_a_non_strict_error_handler(self, monkeypatch) -> None:
        import io
        import sys

        stream = io.TextIOWrapper(io.BytesIO(), encoding="ascii")
        monkeypatch.setattr(sys, "stdout", stream)

        assert stream.errors == "strict", "precondition"
        render.make_stdout_safe()
        assert stream.errors == "backslashreplace"

    def test_a_cjk_title_no_longer_aborts_the_command(self, monkeypatch) -> None:
        import io
        import sys

        buffer = io.BytesIO()
        stream = io.TextIOWrapper(buffer, encoding="ascii")
        monkeypatch.setattr(sys, "stdout", stream)
        render.make_stdout_safe()

        render.value("中文标题 Big Buck Bunny")  # must not raise
        stream.flush()

        assert buffer.getvalue().decode("ascii")

    def test_it_tolerates_a_stream_that_cannot_be_reconfigured(self, monkeypatch) -> None:
        """A test harness may replace sys.stdout with a StringIO."""
        import io
        import sys

        monkeypatch.setattr(sys, "stdout", io.StringIO())
        render.make_stdout_safe()  # must not raise

    def test_main_hardens_stdout_before_dispatching(self, monkeypatch) -> None:
        """Wiring it into main() is the part that actually protects users."""
        import sys

        from porter_cli import app

        calls: list[str] = []
        monkeypatch.setattr(render, "make_stdout_safe", lambda: calls.append("called"))
        monkeypatch.setattr(sys, "argv", ["porter", "--version"])

        with pytest.raises(SystemExit):
            app.main(["--version"])

        assert calls == ["called"]


class TestJsonIsEncodingIndependent:
    def test_emit_json_is_pure_ascii(self, capsys) -> None:
        """Otherwise a non-UTF-8 stdout corrupts the document, not just the display."""
        render.emit_json({"title": "中文标题", "emoji": "🎬"})
        assert capsys.readouterr().out.isascii()

    def test_emit_json_round_trips_through_json_loads(self, capsys) -> None:
        """The escapes must be *valid* JSON, not merely ASCII.

        ``errors="backslashreplace"`` alone is not enough: outside the BMP Python
        emits an eight-character escape that is not valid JSON, whereas
        ``json.dumps`` always emits a surrogate pair.
        """
        payload = {"title": "中文标题", "emoji": "🎬", "path": Path("/var/porter/x")}
        render.emit_json(payload)

        decoded = json.loads(capsys.readouterr().out)
        assert decoded["title"] == "中文标题"
        assert decoded["emoji"] == "🎬"
        assert decoded["path"] == "/var/porter/x"

    def test_emit_json_serialises_paths_and_enums(self, capsys) -> None:
        from porter.events import Phase

        render.emit_json({"phase": Phase.PREPARE, "path": Path("/var/porter/a/b")})
        decoded = json.loads(capsys.readouterr().out)

        assert decoded["path"] == "/var/porter/a/b"
        assert decoded["phase"] == "prepare"


class TestRunOptionsReachThePipeline:
    """Every flag must land on the object the pipeline actually reads.

    This is the guard for a real bug: ``JobOptions`` exists on both ``JobRequest``
    and ``RunContext``, and the pipeline read phase selection from one and the
    renderer's settings from the other. The CLI happened to pass the *same object*
    to both, which is exactly why the bug was invisible here and only a real
    end-to-end run found it. These tests pin that the flags reach the request,
    which is the object that now wins.
    """

    @pytest.fixture
    def captured(self, monkeypatch: pytest.MonkeyPatch) -> dict[str, object]:
        """Intercept the pipeline before it runs, capturing the request and context."""
        import porter_cli.commands.run as run_module

        seen: dict[str, object] = {}

        class FakePipeline:
            @staticmethod
            def default(ctx: object) -> FakePipeline:
                seen["ctx"] = ctx
                return FakePipeline()

            def run(self, request: object, ctx: object) -> object:
                seen["request"] = request
                from porter.models.request import JobResult

                return JobResult(job_id="x", state=JobState.DONE)

        monkeypatch.setattr(run_module, "Pipeline", FakePipeline, raising=False)
        import porter.pipeline as pipeline_module

        monkeypatch.setattr(pipeline_module, "Pipeline", FakePipeline)
        return seen

    def test_burn_lands_on_the_request(self, captured, tmp_path, capsys) -> None:

        main(["run", URL, "-o", str(tmp_path), "--burn", "zh_only"])

        request = captured["request"]
        assert request.options.burn is BurnMode.ZH_ONLY  # type: ignore[attr-defined]

    def test_target_lang_and_force_land_on_the_request(self, captured, tmp_path, capsys) -> None:
        main(["run", URL, "-o", str(tmp_path), "--target-lang", "zh-Hant", "--force"])

        options = captured["request"].options  # type: ignore[attr-defined]
        assert options.target_lang == "zh-Hant"
        assert options.force is True

    def test_only_phase_lands_on_the_request(self, captured, tmp_path, capsys) -> None:
        from porter.events import Phase

        main(["run", URL, "-o", str(tmp_path), "--only-phase", "translate"])

        assert captured["request"].options.only_phase is Phase.TRANSLATE  # type: ignore[attr-defined]

    def test_the_cli_does_not_depend_on_the_two_options_objects_matching(
        self, captured, tmp_path, capsys
    ) -> None:
        """The request is authoritative, so even a mismatched context is safe.

        Asserted by construction: the CLI passes one object to both, and the
        pipeline rebinds from the request. A frontend that passes different
        objects is now also correct.
        """

        main(["run", URL, "-o", str(tmp_path), "--burn", "bilingual_only"])

        ctx = captured["ctx"]
        request = captured["request"]
        assert request.options.burn is BurnMode.BILINGUAL_ONLY  # type: ignore[attr-defined]
        assert ctx.options is request.options or ctx.options.burn is BurnMode.BILINGUAL_ONLY  # type: ignore[attr-defined]


# ----------------------------------------------------------------------
# porter jobs
# ----------------------------------------------------------------------


@pytest.fixture
def registry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A job registry in a temp directory.

    Patches the *path seam* rather than the class, so the command still builds a
    real :class:`~porter.jobs.JobRegistry` and every read and write goes through
    the real file. The alternative -- faking the registry -- would test the
    command against an object that behaves nothing like the one it ships with.
    """
    from porter.jobs import JobRegistry
    from porter.jobs import records as records_module

    path = tmp_path / "jobs.json"
    monkeypatch.setattr(records_module, "registry_file", lambda: path)
    return JobRegistry(path)


class TestJobsCommand:
    def test_list_reports_no_jobs_on_an_empty_registry(
        self, registry, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert main(["jobs", "list"]) == render.EXIT_OK
        assert "no running jobs" in capsys.readouterr().err

    def test_list_hides_finished_jobs_without_all(
        self, registry, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """`porter jobs list` answers "what is running", so done jobs are noise."""
        from porter.events import JobState
        from porter.jobs import JobRecord

        registry.publish(JobRecord(job_id="a", source="u", state=JobState.DONE.value))

        assert main(["jobs", "list"]) == render.EXIT_OK
        assert "no running jobs" in capsys.readouterr().err

        assert main(["jobs", "list", "--all"]) == render.EXIT_OK
        assert "a" in capsys.readouterr().out

    def test_list_prints_a_running_job(
        self, registry, capsys: pytest.CaptureFixture[str]
    ) -> None:
        from porter.events import JobState
        from porter.jobs import JobRecord

        registry.publish(
            JobRecord(
                job_id="abc123", source="https://example.com/v",
                state=JobState.RUNNING.value, phase="prepare", percent=42.0,
            )
        )

        assert main(["jobs", "list"]) == render.EXIT_OK

        out = capsys.readouterr().out
        assert "abc123" in out
        assert "running" in out
        assert "prepare" in out
        assert "42%" in out

    def test_list_json_goes_to_stdout(
        self, registry, capsys: pytest.CaptureFixture[str]
    ) -> None:
        from porter.jobs import JobRecord

        registry.publish(JobRecord(job_id="a", source="u"))

        assert main(["jobs", "list", "--all", "--json"]) == render.EXIT_OK

        payload = json.loads(capsys.readouterr().out)
        assert payload[0]["job_id"] == "a"

    def test_status_shows_the_record_and_its_artifacts(
        self, registry, capsys: pytest.CaptureFixture[str]
    ) -> None:
        from porter.events import JobState
        from porter.jobs import JobRecord

        registry.publish(
            JobRecord(
                job_id="a", source="u", state=JobState.DONE.value,
                artifacts=["/out/task"], task_dir="/out/task",
            )
        )

        assert main(["jobs", "status", "a"]) == render.EXIT_OK

        captured = capsys.readouterr()
        assert "done" in captured.err
        # Artifacts are the result, so they go to stdout where they can be piped.
        assert "/out/task" in captured.out

    def test_status_of_an_unknown_job_is_an_error(
        self, registry, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert main(["jobs", "status", "nope"]) == render.EXIT_ERROR
        assert "unknown job id" in capsys.readouterr().err

    def test_status_reaps_a_dead_job(
        self, registry, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """The question this command exists for: did it die without saying so."""
        from porter.events import JobState
        from porter.jobs import JobRecord

        registry.publish(
            JobRecord(
                job_id="a", source="u", state=JobState.RUNNING.value,
                pid=_dead_pid(), pid_start=1.0,
            )
        )

        assert main(["jobs", "status", "a"]) == render.EXIT_OK

        assert "failed" in capsys.readouterr().err
        # Persisted, so a later reader does not have to re-derive it.
        assert registry.get("a").state == JobState.FAILED.value

    def test_cancel_records_a_request(
        self, registry, capsys: pytest.CaptureFixture[str]
    ) -> None:
        from porter.events import JobState
        from porter.jobs import JobRecord

        registry.publish(
            JobRecord(
                job_id="a", source="u", state=JobState.RUNNING.value,
                pid=os.getpid(), pid_start=process_marker()[1],
            )
        )

        assert main(["jobs", "cancel", "a"]) == render.EXIT_OK

        assert "cancellation requested" in capsys.readouterr().err
        assert registry.is_cancel_requested("a") is True

    def test_cancelling_a_finished_job_is_an_error(
        self, registry, capsys: pytest.CaptureFixture[str]
    ) -> None:
        from porter.events import JobState
        from porter.jobs import JobRecord

        registry.publish(JobRecord(job_id="a", source="u", state=JobState.DONE.value))

        assert main(["jobs", "cancel", "a"]) == render.EXIT_ERROR
        assert "already finished" in capsys.readouterr().err

    def test_cancelling_an_unknown_job_is_an_error(
        self, registry, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert main(["jobs", "cancel", "nope"]) == render.EXIT_ERROR
        assert "unknown job id" in capsys.readouterr().err

    def test_clear_drops_finished_jobs(
        self, registry, capsys: pytest.CaptureFixture[str]
    ) -> None:
        from porter.events import JobState
        from porter.jobs import JobRecord

        registry.publish(JobRecord(job_id="a", source="u", state=JobState.DONE.value))

        assert main(["jobs", "clear"]) == render.EXIT_OK

        assert "removed 1 finished job" in capsys.readouterr().err
        assert registry.read() == []


def _dead_pid() -> int:
    """A PID nothing is using, for staleness tests."""
    import subprocess

    proc = subprocess.Popen(["true"])
    proc.wait()
    return proc.pid
