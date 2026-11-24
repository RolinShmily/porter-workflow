"""CLI wiring: argument parsing, the bare-URL shorthand, and exit codes."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from porter import __version__
from porter.cli import render
from porter.cli.app import _inject_run, main
from porter.core.events import JobState
from porter.core.jobs import process_marker
from porter.core.models.request import BurnMode

URL = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"


def _canned_plan(tmp_path: Path):
    """A valid plan for tests that need ``run`` to reach its executor seam.

    No ``config_fingerprint``: the fake executor never preflights, and leaving it
    empty keeps the fixture independent of the machine's configuration.
    """
    from porter.core.workflow.model import (
        PlanRuntime,
        SourceKind,
        SourceProbe,
        Stage,
        StagePlan,
        Task,
        WorkflowId,
        WorkflowPlan,
    )

    return WorkflowPlan(
        workflow=WorkflowId.URL_ASR,
        task=Task.RELEASE,
        source=URL,
        runtime=PlanRuntime(output_dir=str(tmp_path / "out")),
        source_probe=SourceProbe(
            kind=SourceKind.URL,
            source=URL,
            platform="youtube",
            has_video=True,
        ),
        stages=(
            StagePlan(id="probe", stage=Stage.PROBE, capability="probe.ytdlp"),
        ),
    ).sealed()


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

    def test_jobs_is_no_longer_a_stub(self, capsys: pytest.CaptureFixture[str]) -> None:
        """Regression guard: `porter jobs` must read the registry, not a stub."""
        assert main(["jobs"]) == render.EXIT_OK
        err = capsys.readouterr().err
        assert "not implemented" not in err
        assert "no running jobs" in err

    def test_run_is_no_longer_a_stub(
        self, monkeypatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Regression guard: `porter run` must attempt a real execution plan.

        Resolution is the first real step and it is the seam the command owns.
        A bad source that fails there is enough -- the point is that the command
        reaches it and reports a real failure instead of returning EXIT_MISUSE.
        """
        from porter.core.errors import ExtractionError

        def fail(source, overrides, ctx):
            raise ExtractionError("not a video")

        monkeypatch.setattr("porter.core.workflow.resolver.resolve_plan", fail)

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

    def test_set_refuses_a_toml_target(self, project_dir, tmp_path, capsys) -> None:
        target = tmp_path / "porter.toml"
        original = "[llm]\nmodel = 'keep-me'\n"
        target.write_text(original, encoding="utf-8")

        code = main(["config", "set", "llm.model=new", "--file", str(target)])

        assert code == render.EXIT_ERROR
        assert "TOML" in capsys.readouterr().err
        assert target.read_text(encoding="utf-8") == original


class TestInspectCommand:
    """``porter inspect`` — the command that had no implementation until P2.5.

    ``inspect_url`` is patched at its definition site; the command imports it
    inside ``run()``, so the patch takes effect at call time. No network.
    """

    @pytest.fixture
    def result(self):
        from porter.core.models.inspection import InspectionResult

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
            "porter.core.platforms.inspector.inspect_url", lambda *a, **k: result
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
        from porter.core.errors import ExtractionError

        def boom(*args, **kwargs):
            raise ExtractionError("network is down")

        monkeypatch.setattr("porter.core.platforms.inspector.inspect_url", boom)

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
        from porter.core.models.inspection import InspectionResult

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

        monkeypatch.setattr("porter.core.platforms.inspector.inspect_url", capture)

        main(["inspect", f"{URL}&t=15s&utm_source=share"])

        out = capsys.readouterr().out
        assert "t=15s" in out
        assert "utm_source" not in out


class TestDoctorCommand:
    """``porter doctor`` — facts on stderr, ``--json`` alone on stdout."""

    @pytest.fixture
    def healthy(self, monkeypatch):
        from porter.core.doctor import CapabilityReport
        from porter.core.doctor.probes import Finding

        report = CapabilityReport(
            findings=[
                Finding.passed("ffmpeg", "ffmpeg & ffprobe", "found"),
                Finding.passed("libass", "libass (hardsub)", "ass filter present"),
            ]
        )
        monkeypatch.setattr("porter.core.doctor.probe_all", lambda *a, **k: report)
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

    def test_json_mode_carries_remediation_for_a_blocker(
        self, monkeypatch, capsys, project_dir
    ) -> None:
        """An agent reading --json must get the fix in the same response."""
        from porter.core.doctor import CapabilityReport
        from porter.core.doctor.probes import Finding

        report = CapabilityReport(
            findings=[
                Finding.blocked("ffmpeg", "ffmpeg & ffprobe", "not found", "ffmpeg"),
            ]
        )
        monkeypatch.setattr("porter.core.doctor.probe_all", lambda *a, **k: report)

        assert main(["doctor", "--json"]) == render.EXIT_ERROR

        payload = json.loads(capsys.readouterr().out)
        guidance = payload["guidance"]["ffmpeg"]
        assert guidance["summary"]
        assert any("apt install ffmpeg" in step for step in guidance["steps"])

    def test_a_blocker_exits_non_zero(self, monkeypatch, capsys, project_dir) -> None:
        from porter.core.doctor import CapabilityReport
        from porter.core.doctor.probes import Finding

        report = CapabilityReport(
            findings=[
                Finding.blocked("ffmpeg", "ffmpeg & ffprobe", "not found", "ffmpeg"),
            ]
        )
        monkeypatch.setattr("porter.core.doctor.probe_all", lambda *a, **k: report)

        assert main(["doctor"]) == render.EXIT_ERROR
        assert "1 blocking issue" in capsys.readouterr().err

    def test_a_blocker_shows_its_remediation(self, monkeypatch, capsys, project_dir) -> None:
        """The whole point of the key/text split: the fix must reach the operator."""
        from porter.core.doctor import CapabilityReport
        from porter.core.doctor.probes import Finding

        report = CapabilityReport(
            findings=[
                Finding.blocked("ffmpeg", "ffmpeg & ffprobe", "not found", "ffmpeg"),
            ]
        )
        monkeypatch.setattr("porter.core.doctor.probe_all", lambda *a, **k: report)
        main(["doctor"])

        err = capsys.readouterr().err
        assert "apt install ffmpeg" in err
        assert "[FAIL]" in err

    def test_a_degradation_warns_but_still_exits_zero(
        self, monkeypatch, capsys, project_dir
    ) -> None:
        """No GPU must not fail a gate the way no ffmpeg does."""
        from porter.core.doctor import CapabilityReport
        from porter.core.doctor.probes import Finding

        report = CapabilityReport(
            findings=[
                Finding.passed("ffmpeg", "ffmpeg & ffprobe", "found"),
                Finding.degraded("js_runtime", "JavaScript runtime", "none found", "js_runtime"),
            ]
        )
        monkeypatch.setattr("porter.core.doctor.probe_all", lambda *a, **k: report)

        assert main(["doctor"]) == render.EXIT_OK
        err = capsys.readouterr().err
        assert "[WARN]" in err
        assert "deno.land" in err, "the remediation should be shown"
        assert "1 degraded" in err

    def test_status_markers_are_ascii(self, monkeypatch, capsys, project_dir) -> None:
        """Under LANG=C a non-ASCII glyph raises UnicodeEncodeError."""
        from porter.core.doctor import CapabilityReport
        from porter.core.doctor.probes import Finding

        report = CapabilityReport(
            findings=[
                Finding.passed("a", "A"),
                Finding.degraded("b", "B", "d", "js_runtime"),
                Finding.blocked("c", "C", "d", "ffmpeg"),
            ]
        )
        monkeypatch.setattr("porter.core.doctor.probe_all", lambda *a, **k: report)
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

        from porter.cli import app

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
        # ``str(Path(...))`` rather than the literal: on Windows the same path is
        # ``\var\porter\x``. The invariant is "a Path serialises as its own
        # string", not "with POSIX separators".
        assert decoded["path"] == str(Path("/var/porter/x"))

    def test_emit_json_serialises_paths_and_enums(self, capsys) -> None:
        from porter.core.events import Phase

        render.emit_json({"phase": Phase.PREPARE, "path": Path("/var/porter/a/b")})
        decoded = json.loads(capsys.readouterr().out)

        assert decoded["path"] == str(Path("/var/porter/a/b"))
        assert decoded["phase"] == "prepare"


class TestRunOptionsReachThePlan:
    """Every flag must land on the object the resolver actually reads.

    The old version of these tests pinned that flags reached ``JobOptions`` on the
    request, because the pipeline read phase selection from one object and render
    settings from another. The refactor removes that class of bug: the CLI now
    builds ``PlanOverrides``, and the executor rebuilds ``JobOptions`` *from the
    plan*. These tests pin the first half of that chain.
    """

    @pytest.fixture
    def captured(self, monkeypatch: pytest.MonkeyPatch) -> dict[str, object]:
        """Intercept resolution before it probes or executes anything."""
        from porter.core.errors import ExtractionError

        seen: dict[str, object] = {}

        def fake_resolve(source, overrides, ctx):
            seen["source"] = source
            seen["overrides"] = overrides
            seen["ctx"] = ctx
            raise ExtractionError("stop here")

        monkeypatch.setattr("porter.core.workflow.resolver.resolve_plan", fake_resolve)
        return seen

    def test_burn_lands_on_the_overrides(self, captured, tmp_path, capsys) -> None:
        main(["run", URL, "-o", str(tmp_path), "--burn", "zh_only"])

        assert captured["overrides"].burn is BurnMode.ZH_ONLY  # type: ignore[attr-defined]

    def test_a_hyphenated_burn_mode_is_accepted(self, captured, tmp_path, capsys) -> None:
        """The old parser offered ``zh-only`` but the enum value is ``zh_only``.

        Passing the advertised form raised ``ValueError`` inside the command, so
        this pins both spellings.
        """
        main(["run", URL, "-o", str(tmp_path), "--burn", "zh-only"])

        assert captured["overrides"].burn is BurnMode.ZH_ONLY  # type: ignore[attr-defined]

    def test_denoise_falls_back_to_the_config_key(self, captured, tmp_path, capsys) -> None:
        cfg = tmp_path / "porter.json"
        cfg.write_text('{"asr": {"audio_denoise": false}}', encoding="utf-8")

        main(["--config", str(cfg), "run", URL, "-o", str(tmp_path)])

        assert captured["overrides"].audio_denoise is False  # type: ignore[attr-defined]

    def test_no_denoise_flag_wins_over_the_config(self, captured, tmp_path, capsys) -> None:
        cfg = tmp_path / "porter.json"
        cfg.write_text('{"asr": {"audio_denoise": true}}', encoding="utf-8")

        main(["--config", str(cfg), "run", URL, "-o", str(tmp_path), "--no-denoise"])

        assert captured["overrides"].audio_denoise is False  # type: ignore[attr-defined]

    def test_target_lang_and_force_land_on_the_overrides(
        self, captured, tmp_path, capsys
    ) -> None:
        main(["run", URL, "-o", str(tmp_path), "--target-lang", "zh-Hant", "--force"])

        overrides = captured["overrides"]
        assert overrides.target_lang == "zh-Hant"  # type: ignore[attr-defined]
        assert overrides.force is True  # type: ignore[attr-defined]

    def test_a_capability_request_lands_on_the_overrides(
        self, captured, tmp_path, capsys
    ) -> None:
        main(
            [
                "run",
                URL,
                "-o",
                str(tmp_path),
                "--transcript",
                "whisper-api",
                "--translate",
                "llm",
                "--allow-paid",
            ]
        )

        overrides = captured["overrides"]
        assert overrides.transcript == "whisper-api"  # type: ignore[attr-defined]
        assert overrides.translate == "llm"  # type: ignore[attr-defined]
        assert overrides.allow_paid is True  # type: ignore[attr-defined]

    def test_only_phase_is_mapped_onto_a_task(self, captured, tmp_path, capsys) -> None:
        from porter.core.workflow.model import Task

        main(["run", URL, "-o", str(tmp_path), "--only-phase", "translate"])

        assert captured["overrides"].task is Task.SUBTITLE  # type: ignore[attr-defined]


# ----------------------------------------------------------------------
# porter jobs
# ----------------------------------------------------------------------


@pytest.fixture
def registry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A job registry in a temp directory.

    Patches the *path seam* rather than the class, so the command still builds a
    real :class:`~porter.core.jobs.JobRegistry` and every read and write goes through
    the real file. The alternative -- faking the registry -- would test the
    command against an object that behaves nothing like the one it ships with.
    """
    from porter.core.jobs import JobRegistry
    from porter.core.jobs import records as records_module

    path = tmp_path / "jobs.json"
    monkeypatch.setattr(records_module, "registry_file", lambda: path)
    return JobRegistry(path)


class TestRunFromAPlanFile:
    """``run --plan`` executes the approved document, not a re-resolution.

    The plan is the agent-facing contract: it was computed once, shown to the
    user, and is now handed back. Only execution settings (output directory,
    ``--force``) may change; a capability choice may not, because doing so would
    run a pipeline nobody approved.
    """

    @pytest.fixture
    def saved(self, tmp_path: Path) -> Path:
        path = tmp_path / "plan.json"
        path.write_text(_canned_plan(tmp_path).model_dump_json(), encoding="utf-8")
        return path

    @pytest.fixture
    def executed(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> dict[str, object]:
        from porter.core.events import JobState
        from porter.core.models.request import JobResult
        from porter.core.workflow.executor import ExecutionOutcome
        from porter.core.workflow.model import WorkflowResult

        seen: dict[str, object] = {}

        def fake_execute(plan, ctx):
            seen["plan"] = plan
            result = WorkflowResult(
                job_id=ctx.job_id,
                state=JobState.DONE,
                plan_id=plan.plan_id,
                workflow=plan.workflow,
                task=plan.task,
                task_dir=str(tmp_path / "task"),
                artifacts={"video": str(tmp_path / "task" / "video.mp4")},
            )
            return ExecutionOutcome(
                workflow=result,
                job=JobResult(job_id=ctx.job_id, state=JobState.DONE),
            )

        monkeypatch.setattr("porter.core.workflow.executor.execute_plan", fake_execute)
        return seen

    def test_the_saved_plan_is_executed_unchanged(
        self, saved, executed, capsys
    ) -> None:
        assert main(["run", "--plan", str(saved)]) == render.EXIT_OK

        plan = executed["plan"]
        assert plan.plan_id == _canned_plan(  # type: ignore[attr-defined]
            Path(saved).parent
        ).plan_id
        assert "video.mp4" in capsys.readouterr().out

    def test_output_and_force_are_runtime_overrides(
        self, saved, executed, tmp_path
    ) -> None:
        assert (
            main(
                [
                    "run",
                    "--plan",
                    str(saved),
                    "-o",
                    str(tmp_path / "elsewhere"),
                    "--force",
                ]
            )
            == render.EXIT_OK
        )

        plan = executed["plan"]
        assert plan.runtime.output_dir == str(tmp_path / "elsewhere")  # type: ignore[attr-defined]
        assert plan.runtime.force is True  # type: ignore[attr-defined]
        # The reseal makes the changed execution settings part of the identity.
        assert plan.plan_id != _canned_plan(tmp_path).plan_id  # type: ignore[attr-defined]

    def test_capability_flags_are_reported_as_ignored(
        self, saved, executed, capsys
    ) -> None:
        assert main(["run", "--plan", str(saved), "--burn", "skip"]) == render.EXIT_OK

        assert "ignored with --plan" in capsys.readouterr().err

    def test_a_malformed_plan_file_is_an_error(self, tmp_path, capsys) -> None:
        path = tmp_path / "plan.json"
        path.write_text("{not json", encoding="utf-8")

        assert main(["run", "--plan", str(path)]) == render.EXIT_ERROR
        assert "invalid plan file" in capsys.readouterr().err

    def test_neither_source_nor_plan_is_misuse(self, capsys) -> None:
        assert main(["run"]) == render.EXIT_MISUSE
        assert "needs a SOURCE" in capsys.readouterr().err


class TestCapabilitiesCommand:
    @pytest.fixture
    def catalog(self, monkeypatch: pytest.MonkeyPatch):
        from porter.core.workflow.model import CapabilityDescriptor, Stage

        descriptors = (
            CapabilityDescriptor(
                id="transcript.whisper-local",
                stage=Stage.TRANSCRIPT,
                available=True,
                cost="free",
            ),
            CapabilityDescriptor(
                id="transcript.whisper-api",
                stage=Stage.TRANSCRIPT,
                available=False,
                cost="paid",
            ),
        )
        monkeypatch.setattr(
            "porter.core.workflow.capabilities.capability_catalog",
            lambda ctx: descriptors,
        )
        return descriptors

    def test_json_lists_every_capability_including_missing_ones(
        self, catalog, capsys
    ) -> None:
        assert main(["capabilities", "--json"]) == render.EXIT_OK

        payload = json.loads(capsys.readouterr().out)
        assert [item["id"] for item in payload] == [
            "transcript.whisper-local",
            "transcript.whisper-api",
        ]
        assert payload[1]["available"] is False

    def test_available_filters_the_catalog(self, catalog, capsys) -> None:
        assert main(["capabilities", "--available", "--json"]) == render.EXIT_OK

        payload = json.loads(capsys.readouterr().out)
        assert [item["id"] for item in payload] == ["transcript.whisper-local"]

    def test_stage_filters_the_catalog(self, catalog, capsys) -> None:
        assert main(["capabilities", "--stage", "translate", "--json"]) == render.EXIT_OK

        assert json.loads(capsys.readouterr().out) == []


class TestProbeCommand:
    def test_a_subtitle_file_is_reported_without_touching_the_network(
        self, tmp_path, capsys
    ) -> None:
        source = tmp_path / "captions.srt"
        source.write_text(
            "1\n00:00:00,000 --> 00:00:01,000\nHello\n", encoding="utf-8"
        )

        assert main(["probe", str(source), "--json"]) == render.EXIT_OK

        payload = json.loads(capsys.readouterr().out)
        assert payload["kind"] == "subtitle"
        assert payload["tracks"][0]["verified"] is True

    def test_an_unusable_source_exits_non_zero(self, tmp_path, capsys) -> None:
        assert main(["probe", str(tmp_path / "gone.mp3"), "--json"]) == render.EXIT_ERROR

        payload = json.loads(capsys.readouterr().out)
        assert payload["warnings"][0]["code"] == "SOURCE_NOT_FOUND"


class TestAnInterruptIsReportedNotRaised:
    """Ctrl+C must produce a sentence, not a traceback ending in socket internals.

    Measured before this existed: ``KeyboardInterrupt`` unwound out of the
    pipeline and the user was shown the frame stack of whichever library call was
    running -- ssl, socket, ffmpeg's pipe -- and the process exited with the
    Windows control-exit code rather than 130.
    """

    @pytest.fixture
    def interrupted(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """A resolved plan whose executor raises ``KeyboardInterrupt``.

        The interrupt is injected at the executor seam rather than inside the
        pipeline: ``run`` now owns the job record and the handler under test, and
        resolving a real plan would probe the network.
        """

        def interrupted_execute(plan: object, ctx: object) -> object:
            raise KeyboardInterrupt

        monkeypatch.setattr(
            "porter.core.workflow.resolver.resolve_plan",
            lambda source, overrides, ctx: _canned_plan(tmp_path),
        )
        monkeypatch.setattr(
            "porter.core.workflow.executor.execute_plan", interrupted_execute
        )

    def test_an_interrupt_in_a_run_exits_cancelled(
        self, interrupted, tmp_path, capsys
    ) -> None:
        assert main(["run", URL, "-o", str(tmp_path)]) == render.EXIT_CANCELLED
        assert "interrupted" in capsys.readouterr().err

    def test_the_job_records_its_own_obituary(
        self, interrupted, tmp_path, capsys
    ) -> None:
        """The process is alive and knows what happened, so it should say so.

        Left to the reaper, the record sits at ``running`` until another porter
        process notices the PID is gone -- inferring an interruption from a dead
        process when this one could simply have written it down.
        """
        from porter.core.jobs import JobRegistry

        main(["run", URL, "-o", str(tmp_path)])

        assert [r.state for r in JobRegistry().read(reap=False)] == [
            JobState.CANCELLED.value
        ]

    def test_an_interrupt_outside_a_job_is_reported(self, monkeypatch, capsys) -> None:
        import porter.cli.app as app_module

        def interrupt(args: object) -> int:
            raise KeyboardInterrupt

        monkeypatch.setattr(app_module, "dispatch", interrupt)

        assert main(["doctor"]) == render.EXIT_CANCELLED
        assert "interrupted" in capsys.readouterr().err


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
        from porter.core.events import JobState
        from porter.core.jobs import JobRecord

        registry.publish(JobRecord(job_id="a", source="u", state=JobState.DONE.value))

        assert main(["jobs", "list"]) == render.EXIT_OK
        assert "no running jobs" in capsys.readouterr().err

        assert main(["jobs", "list", "--all"]) == render.EXIT_OK
        assert "a" in capsys.readouterr().out

    def test_list_prints_a_running_job(
        self, registry, capsys: pytest.CaptureFixture[str]
    ) -> None:
        from porter.core.events import JobState
        from porter.core.jobs import JobRecord

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
        from porter.core.jobs import JobRecord

        registry.publish(JobRecord(job_id="a", source="u"))

        assert main(["jobs", "list", "--all", "--json"]) == render.EXIT_OK

        payload = json.loads(capsys.readouterr().out)
        assert payload[0]["job_id"] == "a"

    def test_status_shows_the_record_and_its_artifacts(
        self, registry, capsys: pytest.CaptureFixture[str]
    ) -> None:
        from porter.core.events import JobState
        from porter.core.jobs import JobRecord

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
        from porter.core.events import JobState
        from porter.core.jobs import JobRecord

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
        from porter.core.events import JobState
        from porter.core.jobs import JobRecord

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
        from porter.core.events import JobState
        from porter.core.jobs import JobRecord

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
        from porter.core.events import JobState
        from porter.core.jobs import JobRecord

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


class TestPlanAcceptsCookies:
    """``plan`` inspects the source, so it needs the credentials ``run`` needs.

    It used to lack the flags while its own blocker text told the user to pass
    them -- advice the command could not follow. ``inspect`` always had them.
    """

    def test_the_cookie_flags_parse(self) -> None:
        from porter.cli.app import build_parser

        args = build_parser().parse_args(
            ["plan", URL, "--cookies-from-browser", "firefox", "--cookies", "c.txt"]
        )

        assert args.cookies_from_browser == "firefox"
        assert args.cookies == "c.txt"

    def test_the_flags_reach_the_resolver(self, monkeypatch, tmp_path) -> None:
        from porter.cli.app import build_parser
        from porter.cli.commands import plan as plan_cmd
        from porter.core.errors import ExtractionError

        captured: dict[str, object] = {}

        def fake_resolve(source, overrides, ctx):
            captured["ctx"] = ctx
            captured["overrides"] = overrides
            # A ``PorterError`` is caught by the command and returned as a status,
            # which ends the test before it needs a fully-formed plan.
            raise ExtractionError("stop here")

        monkeypatch.setattr("porter.core.workflow.resolver.resolve_plan", fake_resolve)

        cfg = tmp_path / "porter.json"
        cfg.write_text('{"asr": {"engine": "whisper-api"}}', encoding="utf-8")
        args = build_parser().parse_args(
            [
                "--config",
                str(cfg),
                "plan",
                URL,
                "--cookies-from-browser",
                "firefox",
                "--cookies",
                "c.txt",
            ]
        )

        assert plan_cmd.run(args) == render.EXIT_ERROR

        overrides = captured["overrides"]
        assert overrides.cookies_browser == "firefox"  # type: ignore[attr-defined]
        assert overrides.cookies_file == Path("c.txt")  # type: ignore[attr-defined]
        ctx = captured["ctx"]
        assert ctx is not None, "the CLI must pass a context, not just the options"
        assert ctx.options.cookies_browser == "firefox"
        # ``cookies_file`` is a ``Path`` on the model, not a ``str``.
        assert ctx.options.cookies_file == Path("c.txt")
        # And the resolved config must reach the resolver. Passing only ``options``
        # made it re-resolve the config itself, which silently dropped ``--config``
        # -- so the plan described a different run from the one the same command
        # line would perform.
        assert ctx.config.asr.engine == "whisper-api"


class TestSymbolDegradesOnANonUtf8Console:
    """A GBK or cp1252 console cannot encode ``✓``/``✗``.

    ``make_stdout_safe`` sets ``errors="backslashreplace"``, so this cannot
    crash the command -- but the user would read a literal ``\\u2713``, which is
    worse than a plain mark, since legibility is the mark's only job.
    """

    @pytest.fixture
    def gbk_console(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(render, "_stream_encoding", lambda stream: "gbk")

    def test_a_glyph_the_console_cannot_encode_falls_back(self, gbk_console: None) -> None:
        assert render.symbol("✓", "ok") == "ok"
        assert render.symbol("✗", "x") == "x"
        assert render.symbol("⊘", "-") == "-"

    def test_a_glyph_gbk_can_encode_is_kept(self, gbk_console: None) -> None:
        # GBK covers these, so there is no reason to degrade them.
        assert render.symbol("…", ".") == "…"
        assert render.symbol("→", ">") == "→"

    def test_a_utf8_console_keeps_the_glyph(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(render, "_stream_encoding", lambda stream: "utf-8")

        assert render.symbol("✓", "ok") == "✓"

    def test_job_state_uses_the_fallback(self, gbk_console: None) -> None:
        # Single ASCII glyphs, so the ``porter jobs`` column keeps its width.
        assert render.render_job_state(JobState.DONE) == "+"
        assert render.render_job_state(JobState.FAILED) == "x"
        assert render.render_job_state(JobState.RUNNING) == ">"
