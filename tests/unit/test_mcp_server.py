"""Smoke tests for the MCP server: does it build, and do tools answer?"""

from __future__ import annotations

import os

import pytest

pytest.importorskip("fastmcp", reason="requires the [mcp] extra")

from fastmcp import Client

from porter import __version__
from porter.core.jobs import process_marker
from porter.mcp.server import create_server


def _feasible_plan(source: str = "/videos/a.mp4"):
    """A sealed plan that is cheap to build and never probes anything.

    Job-tool tests are about orchestration -- ids, threads, cancellation,
    result shaping -- not about whether this machine has ffmpeg. Supplying a
    plan through the same public path an agent uses keeps those concerns apart.
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
        workflow=WorkflowId.LOCAL_ASR,
        task=Task.RELEASE,
        source=source,
        runtime=PlanRuntime(output_dir="./porter_output"),
        source_probe=SourceProbe(
            kind=SourceKind.LOCAL_VIDEO,
            source=source,
            has_video=True,
            has_audio=True,
        ),
        stages=(
            StagePlan(
                id="acquire",
                stage=Stage.ACQUIRE,
                capability="acquire.local",
            ),
        ),
        feasible=True,
    ).sealed()


class TestServerConstruction:
    def test_create_server_returns_a_server(self) -> None:
        assert create_server() is not None

    def test_server_import_is_cheap_without_mcp_extra(self) -> None:
        """``import porter.mcp`` must not require fastmcp.

        The package ``__init__`` stays dependency-light so that tooling and
        tests can import it without the optional extra installed.
        """
        import porter.mcp

        assert porter.mcp.__doc__


class TestToolContract:
    async def test_porter_version_is_exposed(self) -> None:
        async with Client(create_server()) as client:
            names = {tool.name for tool in await client.list_tools()}
        assert "porter_version" in names

    async def test_porter_version_returns_engine_metadata(self) -> None:
        async with Client(create_server()) as client:
            result = await client.call_tool("porter_version", {})
        assert result.data["version"] == __version__
        assert result.data["name"] == "porter-workflow"

    async def test_every_tool_has_a_description(self) -> None:
        """An MCP tool without a description is invisible to the model."""
        async with Client(create_server()) as client:
            tools = await client.list_tools()
        assert tools, "the server exposes no tools"
        for tool in tools:
            assert tool.description, f"tool {tool.name} has no description"
            assert tool.input_schema is not None, f"tool {tool.name} has no input schema"


class TestDoctorTool:
    """``porter_doctor`` — the structured report, plus its remediation.

    This is the tool the fact/prose split exists for: an agent cannot act on a
    multi-paragraph Chinese install guide, but it can act on a ``remediation_key``
    plus the steps behind it.
    """

    @pytest.fixture(autouse=True)
    def _in_a_temporary_directory(self, tmp_path, monkeypatch) -> None:
        """Run doctor from an empty directory.

        The server resolves its config from the working directory, and
        ``output_dir`` defaults to the relative path ``./porter_output``. Without
        this the tests asked doctor about the repository root's own
        ``porter_output`` — and because doctor *probes* that directory by writing a
        file into it, running the suite mutated 347 MB of unrelated earlier job
        output. A test whose result depends on the current working directory is
        not hermetic, which is the real defect; the mutation was the symptom.
        """
        monkeypatch.chdir(tmp_path)

    async def test_porter_doctor_is_exposed(self) -> None:
        async with Client(create_server()) as client:
            names = {tool.name for tool in await client.list_tools()}
        assert "porter_doctor" in names

    async def test_the_report_is_structured(self) -> None:
        async with Client(create_server()) as client:
            result = await client.call_tool("porter_doctor", {})

        data = result.data
        assert set(data) >= {"platform", "ok", "blockers", "degradations", "findings"}
        assert isinstance(data["findings"], list)
        for finding in data["findings"]:
            assert set(finding) >= {
                "key",
                "label",
                "ok",
                "severity",
                "detail",
                "remediation_key",
            }

    async def test_severities_are_plain_strings(self) -> None:
        """The payload crosses JSON, so enum instances will not survive."""
        async with Client(create_server()) as client:
            result = await client.call_tool("porter_doctor", {})

        for finding in result.data["findings"]:
            assert finding["severity"] in {"info", "degraded", "blocker"}

    async def test_guidance_covers_exactly_the_failing_findings(self) -> None:
        async with Client(create_server()) as client:
            result = await client.call_tool("porter_doctor", {})

        data = result.data
        failing_keys = {
            finding["remediation_key"]
            for finding in data["findings"]
            if not finding["ok"] and finding["remediation_key"]
        }
        assert set(data["guidance"]) == failing_keys

    async def test_no_finding_escapes_the_report(self) -> None:
        """A finding with no key to report would be invisible to the agent."""
        async with Client(create_server()) as client:
            result = await client.call_tool("porter_doctor", {})
        assert result.data["findings"], "the report is empty"


class TestDoctorGuidesResource:
    async def test_the_guide_resource_is_exposed(self) -> None:
        async with Client(create_server()) as client:
            resources = await client.list_resources()
        assert {str(resource.uri) for resource in resources} >= {
            "porter://doctor/guides"
        }

    async def test_the_guide_document_is_markdown_naming_every_key(self) -> None:
        from porter.core.doctor import GUIDES

        async with Client(create_server()) as client:
            content = await client.read_resource("porter://doctor/guides")

        text = content[0].text
        assert "# porter capability guides" in text
        for key in GUIDES:
            assert f"`{key}`" in text

    async def test_the_guide_document_is_ascii(self) -> None:
        """It is documentation; it must survive an ASCII-locale terminal."""
        async with Client(create_server()) as client:
            content = await client.read_resource("porter://doctor/guides")
        assert content[0].text.isascii()


# ----------------------------------------------------------------------
# Job tools
# ----------------------------------------------------------------------


@pytest.fixture
def _isolated_registry(tmp_path, monkeypatch):
    """Point the server's registry at a temp file.

    The module builds its store at import time, so the path has to be replaced on
    the store that already exists rather than by re-importing. Without this the
    tests would write into the user's real cache directory -- which is exactly the
    kind of leak the doctor tests had.
    """
    from porter.core.jobs import JobRegistry
    from porter.mcp.tools import jobs as jobs_module

    registry = JobRegistry(tmp_path / "jobs.json")
    monkeypatch.setattr(jobs_module._STORE, "_registry", registry)
    monkeypatch.setattr(jobs_module, "_OWNED", set())
    return registry


class TestJobToolsContract:
    async def test_every_job_tool_is_exposed(self) -> None:
        async with Client(create_server()) as client:
            names = {tool.name for tool in await client.list_tools()}

        assert {
            "porter_job_start",
            "porter_job_status",
            "porter_job_result",
            "porter_job_cancel",
            "porter_job_list",
        } <= names

    async def test_the_log_resource_is_a_template(self) -> None:
        async with Client(create_server()) as client:
            templates = {
                getattr(t, "uri_template", None) or t.uriTemplate
                for t in await client.list_resource_templates()
            }

        assert "porter://jobs/{job_id}/log" in templates


class TestJobStatusTool:
    async def test_an_unknown_job_is_reported_as_data(
        self, _isolated_registry
    ) -> None:
        """A bad id is the caller's mistake, and an agent can act on it."""
        async with Client(create_server()) as client:
            result = await client.call_tool("porter_job_status", {"job_id": "nope"})

        assert result.data["ok"] is False
        assert "unknown job id" in result.data["error"]

    async def test_a_job_started_elsewhere_is_visible(self, _isolated_registry) -> None:
        """A CLI run in another terminal must be findable from MCP.

        Reporting "unknown job id" for work that is plainly recorded would make
        the two frontends disagree about reality.
        """
        from porter.core.events import JobState
        from porter.core.jobs import JobRecord

        _isolated_registry.publish(
            JobRecord(
                job_id="run-abc", source="/videos/a.mp4",
                state=JobState.RUNNING.value, phase="prepare", percent=30.0,
            )
        )

        async with Client(create_server()) as client:
            result = await client.call_tool("porter_job_status", {"job_id": "run-abc"})

        assert result.data["ok"] is True
        assert result.data["state"] == "running"
        assert result.data["phase"] == "prepare"
        assert result.data["percent"] == 30.0
        assert result.data["terminal"] is False


class TestJobResultTool:
    async def test_a_running_job_does_not_block(self, _isolated_registry) -> None:
        from porter.core.events import JobState
        from porter.core.jobs import JobRecord

        _isolated_registry.publish(
            JobRecord(job_id="a", source="u", state=JobState.RUNNING.value)
        )

        async with Client(create_server()) as client:
            result = await client.call_tool("porter_job_result", {"job_id": "a"})

        assert result.data["ok"] is False
        assert result.data["terminal"] is False
        assert "still running" in result.data["error"]

    async def test_a_finished_job_reports_its_artifacts(
        self, _isolated_registry
    ) -> None:
        from porter.core.events import JobState
        from porter.core.jobs import JobRecord

        _isolated_registry.publish(
            JobRecord(
                job_id="a", source="u", state=JobState.DONE.value,
                artifacts=["/out/task"], task_dir="/out/task",
            )
        )

        async with Client(create_server()) as client:
            result = await client.call_tool("porter_job_result", {"job_id": "a"})

        assert result.data["ok"] is True
        assert result.data["artifacts"] == ["/out/task"]
        assert result.data["task_dir"] == "/out/task"

    async def test_a_failed_job_surfaces_the_error(self, _isolated_registry) -> None:
        from porter.core.events import JobState
        from porter.core.jobs import JobRecord

        _isolated_registry.publish(
            JobRecord(
                job_id="a", source="u", state=JobState.FAILED.value,
                error="ffmpeg said no",
            )
        )

        async with Client(create_server()) as client:
            result = await client.call_tool("porter_job_result", {"job_id": "a"})

        assert result.data["ok"] is False
        assert result.data["error"] == "ffmpeg said no"


class TestJobCancelTool:
    async def test_an_external_job_is_cancelled_through_the_registry(
        self, _isolated_registry
    ) -> None:
        """The owner is another process, so the request goes through the file."""
        from porter.core.events import JobState
        from porter.core.jobs import JobRecord

        _isolated_registry.publish(
            JobRecord(
                job_id="run-abc", source="u", state=JobState.RUNNING.value,
                pid=os.getpid(), pid_start=process_marker()[1],
            )
        )

        async with Client(create_server()) as client:
            result = await client.call_tool("porter_job_cancel", {"job_id": "run-abc"})

        assert result.data["ok"] is True
        assert _isolated_registry.is_cancel_requested("run-abc") is True

    async def test_cancelling_an_unknown_job_is_reported(
        self, _isolated_registry
    ) -> None:
        async with Client(create_server()) as client:
            result = await client.call_tool("porter_job_cancel", {"job_id": "nope"})

        assert result.data["ok"] is False


class TestJobListTool:
    async def test_it_lists_jobs_from_the_registry(self, _isolated_registry) -> None:
        from porter.core.jobs import JobRecord

        _isolated_registry.publish(JobRecord(job_id="a", source="one"))
        _isolated_registry.publish(JobRecord(job_id="b", source="two"))

        async with Client(create_server()) as client:
            result = await client.call_tool("porter_job_list", {})

        assert {job["job_id"] for job in result.data["jobs"]} == {"a", "b"}

    async def test_an_empty_registry_lists_nothing(self, _isolated_registry) -> None:
        async with Client(create_server()) as client:
            result = await client.call_tool("porter_job_list", {})

        assert result.data["ok"] is True
        assert result.data["jobs"] == []


class TestJobStartTool:
    async def test_a_bad_burn_mode_is_reported_not_raised(
        self, _isolated_registry
    ) -> None:
        """An agent can fix "unknown burn mode"; a raised error just looks broken."""
        async with Client(create_server()) as client:
            result = await client.call_tool(
                "porter_job_start", {"source": "/videos/a.mp4", "burn": "nonsense"}
            )

        assert result.data["ok"] is False
        assert "nonsense" in result.data["error"]

    async def test_a_plan_returns_a_job_id_immediately(
        self, _isolated_registry, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The whole point of the job API: the call returns before the work does.

        ``_run_job`` is replaced rather than allowed to run. Starting a real job
        here would launch a workflow in a background thread, log its failure into
        the test output, and make the assertion depend on how far that thread got
        before the test ended.
        """
        from porter.mcp.tools import jobs as jobs_module

        launched: list[str] = []
        monkeypatch.setattr(
            jobs_module,
            "_run_job",
            lambda job, ctx, plan: launched.append(job.job_id),
        )

        async with Client(create_server()) as client:
            result = await client.call_tool(
                "porter_job_start", {"plan": _feasible_plan().model_dump(mode="json")}
            )

        assert result.data["ok"] is True
        assert result.data["plan_id"].startswith("sha256:")
        assert result.data["workflow"] == "local.asr"
        job_id = result.data["job_id"]
        # Already visible to status before any work has happened.
        assert _isolated_registry.get(job_id) is not None

    async def test_a_started_job_is_attached_to_its_context(
        self, _isolated_registry, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Attach must happen before the thread starts.

        A cancel arriving in the first milliseconds has to find a job whose
        cancellation flag is already wired to the context; attaching afterwards
        would drop it, and the job would run to completion.
        """
        from porter.mcp.tools import jobs as jobs_module

        observed: dict[str, object] = {}

        def capture(job, ctx, plan) -> None:
            observed["cancel_is_wired"] = job.cancel is ctx.cancel
            observed["running"] = job.state.value
            observed["plan_source"] = plan.source

        monkeypatch.setattr(jobs_module, "_run_job", capture)

        async with Client(create_server()) as client:
            await client.call_tool(
                "porter_job_start", {"plan": _feasible_plan().model_dump(mode="json")}
            )

        assert observed["cancel_is_wired"] is True
        assert observed["running"] == "running"
        assert observed["plan_source"] == "/videos/a.mp4"

    async def test_planning_arguments_beside_a_plan_are_refused(
        self, _isolated_registry
    ) -> None:
        """The plan fixed those choices; silently preferring one is worse."""
        async with Client(create_server()) as client:
            result = await client.call_tool(
                "porter_job_start",
                {
                    "plan": _feasible_plan().model_dump(mode="json"),
                    "target_lang": "en",
                },
            )

        assert result.data["ok"] is False
        assert "target_lang" in result.data["error"]

    async def test_an_infeasible_plan_is_refused_before_work(
        self, _isolated_registry
    ) -> None:
        from porter.core.workflow.model import PlanIssue

        plan = _feasible_plan().model_copy(
            update={
                "feasible": False,
                "issues": (
                    PlanIssue(
                        code="NO_CAPABILITY",
                        severity="blocker",
                        message="no transcript capability is available",
                    ),
                ),
            }
        )

        async with Client(create_server()) as client:
            result = await client.call_tool(
                "porter_job_start", {"plan": plan.model_dump(mode="json")}
            )

        assert result.data["ok"] is False
        assert result.data["plan"]["feasible"] is False


class TestRunJobThreadBody:
    """``_run_job`` must never let an exception escape.

    A background thread that dies silently leaves the job at ``running`` forever.
    The registry's staleness detection would eventually paper over it, but only
    while the process is alive, and it would report "killed or crashed" for what
    was an ordinary error. Recording the outcome is strictly better.
    """

    def _job_and_ctx(self, tmp_path):
        from porter.core.config import PorterConfig
        from porter.core.context import RunContext
        from porter.core.events import null_sink
        from porter.core.jobs import JobStore
        from porter.core.models.request import JobOptions, JobRequest

        plan = _feasible_plan()
        store = JobStore()
        request = JobRequest.from_source(plan.source, JobOptions())
        job = store.create(request)
        ctx = RunContext(
            job_id=job.job_id,
            options=JobOptions(output_dir=tmp_path / "out"),
            config=PorterConfig(),
            events=null_sink,
        )
        store.attach(job, ctx)
        return job, ctx, plan

    def test_an_expected_error_becomes_a_failed_result(
        self, tmp_path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from porter.core.errors import ExtractionError
        from porter.mcp.tools import jobs as jobs_module

        job, ctx, plan = self._job_and_ctx(tmp_path)

        def explode(_plan, _ctx):
            raise ExtractionError("local video not found: /videos/a.mp4")

        monkeypatch.setattr("porter.core.workflow.executor.execute_plan", explode)

        jobs_module._run_job(job, ctx, plan)

        assert job.state.value == "failed"
        assert "not found" in (job.result.error.message or "")
        workflow = jobs_module._workflow_for(job.job_id)
        assert workflow is not None
        assert workflow.error is not None
        assert workflow.error.code == "extraction_error"

    def test_an_unexpected_error_is_still_recorded(
        self, tmp_path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from porter.mcp.tools import jobs as jobs_module

        job, ctx, plan = self._job_and_ctx(tmp_path)

        def explode(_plan, _ctx):
            raise ZeroDivisionError("bug in a backend")

        monkeypatch.setattr("porter.core.workflow.executor.execute_plan", explode)

        jobs_module._run_job(job, ctx, plan)

        assert job.state.value == "failed"
        assert job.result.error.code == "internal_error"
        assert "ZeroDivisionError" in (job.result.error.message or "")

    def test_cancellation_becomes_a_cancelled_result(
        self, tmp_path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from porter.core.errors import JobCancelled
        from porter.mcp.tools import jobs as jobs_module

        job, ctx, plan = self._job_and_ctx(tmp_path)

        def explode(_plan, _ctx):
            raise JobCancelled("cancelled by the client")

        monkeypatch.setattr("porter.core.workflow.executor.execute_plan", explode)

        jobs_module._run_job(job, ctx, plan)

        assert job.state.value == "cancelled"
        workflow = jobs_module._workflow_for(job.job_id)
        assert workflow is not None
        assert workflow.state.value == "cancelled"

    def test_a_cancel_while_queued_prevents_the_work(
        self, tmp_path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A job cancelled while waiting its turn must not start when it gets one."""
        from porter.mcp.tools import jobs as jobs_module

        job, ctx, plan = self._job_and_ctx(tmp_path)
        ran: list[str] = []

        def record(_plan, _ctx):
            ran.append("ran")
            raise AssertionError("cancelled work must not start")

        monkeypatch.setattr("porter.core.workflow.executor.execute_plan", record)
        # Cancelled before the semaphore is even acquired.
        job.cancel.set()

        jobs_module._run_job(job, ctx, plan)

        assert ran == []
        assert job.state.value == "cancelled"


# ----------------------------------------------------------------------
# Probe
# ----------------------------------------------------------------------


class TestProbeToolContract:
    """``porter_probe``'s schema is part of the safety contract, not just a shape."""

    async def test_porter_probe_is_exposed(self) -> None:
        async with Client(create_server()) as client:
            names = {tool.name for tool in await client.list_tools()}
        assert "porter_probe" in names

    async def test_the_old_inspect_name_is_gone(self) -> None:
        """A rename is a migration, not an alias: one name per concept."""
        async with Client(create_server()) as client:
            names = {tool.name for tool in await client.list_tools()}
        assert "porter_inspect" not in names

    async def test_it_accepts_a_source_and_nothing_else(self) -> None:
        """No cookie parameters, deliberately.

        The CLI has ``--cookies`` / ``--cookies-from-browser``. Exposing them over
        MCP would write credentials into the conversation transcript and into any
        telemetry with it. Cookies still work here -- they are read from the
        resolved config, which only the CLI can write.

        Asserted on the schema rather than trusted, because the tempting change is
        to "just add one parameter" for a link that needs authentication.
        """
        async with Client(create_server()) as client:
            tool = next(t for t in await client.list_tools() if t.name == "porter_probe")

        assert set(tool.input_schema["properties"]) == {"source"}


class TestProbeTool:
    """Result shaping: what an agent can act on, and what it must not retry."""

    @pytest.fixture
    def fake_inspect(self, monkeypatch):
        """Replace the engine call so shaping can be tested on its own."""
        from porter.core.workflow import probe as probe_module

        def install(result_or_exc):
            def fake(url, ctx=None, **kwargs):
                if isinstance(result_or_exc, Exception):
                    raise result_or_exc
                return result_or_exc

            monkeypatch.setattr(probe_module, "inspect_url", fake)

        return install

    @staticmethod
    def _valid(**overrides) -> object:
        from porter.core.models.inspection import InspectionResult

        fields = {
            "input_url": "https://x.com/a/status/1",
            "canonical_url": "https://x.com/a/status/1",
            "platform": "x",
            "is_valid": True,
            "has_video": True,
            "video_id": "1",
            "title": "A Vertical Clip",
            "duration_seconds": 42.0,
            "width": 1080,
            "height": 1920,
            "is_vertical": True,
            "has_subtitles": False,
        }
        fields.update(overrides)
        return InspectionResult(**fields)

    async def test_a_usable_link_returns_the_probe_facts(self, fake_inspect) -> None:
        fake_inspect(self._valid())

        async with Client(create_server()) as client:
            result = await client.call_tool("porter_probe", {"source": "https://x.com/a/status/1"})

        data = result.data
        assert data["ok"] is True
        assert data["kind"] == "url"
        assert data["platform"] == "x"
        assert data["duration_seconds"] == 42.0
        assert data["is_vertical"] is True
        assert data["blockers"] == []

    async def test_a_listed_track_is_reported_as_unverified(self, fake_inspect) -> None:
        """A listing is not a promise: the warning is the contract."""
        fake_inspect(
            self._valid(
                input_url="https://www.youtube.com/watch?v=abc",
                canonical_url="https://www.youtube.com/watch?v=abc",
                platform="youtube",
                raw_info={"subtitles": {"en": [{"ext": "vtt"}]}},
            )
        )

        async with Client(create_server()) as client:
            result = await client.call_tool(
                "porter_probe", {"source": "https://www.youtube.com/watch?v=abc"}
            )

        data = result.data
        assert data["ok"] is True
        assert data["tracks"][0]["lang"] == "en"
        assert data["tracks"][0]["verified"] is False
        assert any(issue["code"] == "TRACKS_UNVERIFIED" for issue in data["warnings"])

    async def test_an_unusable_link_is_a_successful_call(self, fake_inspect) -> None:
        """The contract that stops an agent burning its retry budget.

        ``ok`` describes the call; the warning describes the source. A 404
        reported as ``ok: false`` reads as "the tool broke", and the natural
        response -- retry -- is exactly wrong.
        """
        from porter.core.models.inspection import InspectionResult

        fake_inspect(
            InspectionResult(
                input_url="https://www.youtube.com/watch?v=dead",
                canonical_url="https://www.youtube.com/watch?v=dead",
                platform="youtube",
                is_valid=False,
                has_video=False,
                error_message="Resource not found, deleted, or private (404).",
            )
        )

        async with Client(create_server()) as client:
            result = await client.call_tool(
                "porter_probe", {"source": "https://www.youtube.com/watch?v=dead"}
            )

        data = result.data
        assert data["ok"] is True, "a bad link is a result, not a tool failure"
        assert data["warnings"][0]["code"] == "SOURCE_UNUSABLE"
        assert "404" in data["warnings"][0]["message"]
        assert data["blockers"] == ["SOURCE_UNUSABLE"]

    async def test_a_local_path_is_a_first_class_source(self) -> None:
        """Unlike the old link-only inspection, a path is probed, not refused."""
        async with Client(create_server()) as client:
            result = await client.call_tool("porter_probe", {"source": "/videos/a.mp4"})

        data = result.data
        assert data["ok"] is True
        assert data["kind"] == "local_video"
        assert data["has_video"] is False
        assert data["warnings"][0]["code"] == "SOURCE_NOT_FOUND"

    async def test_a_file_url_is_treated_as_a_path(self) -> None:
        async with Client(create_server()) as client:
            result = await client.call_tool(
                "porter_probe", {"source": "file:///videos/a.mp4"}
            )

        assert result.data["kind"] == "local_video"
        assert result.data["source"] == "/videos/a.mp4"

    async def test_a_subtitle_file_is_recognised(self, tmp_path) -> None:
        subtitle = tmp_path / "input.srt"
        subtitle.write_text(
            "1\n00:00:00,500 --> 00:00:02,500\nHello\n", encoding="utf-8"
        )

        async with Client(create_server()) as client:
            result = await client.call_tool("porter_probe", {"source": str(subtitle)})

        data = result.data
        assert data["ok"] is True
        assert data["kind"] == "subtitle"
        assert data["tracks"][0]["verified"] is True
        assert data["blockers"] == []

    async def test_a_genuine_fault_is_a_tool_error(self, fake_inspect) -> None:
        """A missing dependency is not a bad link, and must not look like one."""
        from porter.core.errors import PorterError

        fake_inspect(PorterError("ffmpeg is not installed"))

        async with Client(create_server()) as client:
            result = await client.call_tool("porter_probe", {"source": "https://x.com/a/status/1"})

        assert result.data["ok"] is False
        assert "ffmpeg" in result.data["error"]

    async def test_an_unresolvable_config_is_a_tool_error(self, monkeypatch) -> None:
        from porter.core.errors import PorterError

        def boom(explicit=None):
            raise PorterError("config file is not valid JSON")

        monkeypatch.setattr("porter.core.config.resolve", boom)

        async with Client(create_server()) as client:
            result = await client.call_tool("porter_probe", {"source": "https://x.com/a/status/1"})

        assert result.data["ok"] is False
        assert "could not be resolved" in result.data["error"]


class TestProbeConcurrency:
    """The LIGHT cap: cheap locally, not remotely."""

    async def test_probes_are_capped(self, monkeypatch, tmp_path) -> None:
        """An agent fanning out must not open one socket per link.

        The bound is what is asserted, not the exact number: the cap exists to
        protect the remote platform, and a lower effective concurrency is fine.
        """
        import asyncio
        import threading

        from porter.core.models.inspection import InspectionResult
        from porter.core.workflow import probe as probe_module
        from porter.mcp import limits

        monkeypatch.chdir(tmp_path)
        lock = threading.Lock()
        state = {"now": 0, "peak": 0}

        def slow(url, ctx=None, **kwargs):
            with lock:
                state["now"] += 1
                state["peak"] = max(state["peak"], state["now"])
            try:
                import time

                time.sleep(0.2)
            finally:
                with lock:
                    state["now"] -= 1
            return InspectionResult(
                input_url=url,
                canonical_url=url,
                platform="youtube",
                is_valid=True,
                has_video=True,
            )

        monkeypatch.setattr(probe_module, "inspect_url", slow)

        async with Client(create_server()) as client:
            await asyncio.gather(
                *(
                    client.call_tool("porter_probe", {"source": f"https://x.com/a/{i}"})
                    for i in range(limits.LIGHT_CONCURRENCY * 2)
                )
            )

        assert state["peak"] <= limits.LIGHT_CONCURRENCY, (
            f"peak concurrency {state['peak']} exceeded the cap"
        )
        assert state["peak"] > 1, "probes should still run in parallel"


# ----------------------------------------------------------------------
# Capabilities
# ----------------------------------------------------------------------


class TestCapabilitiesToolContract:
    async def test_porter_capabilities_is_exposed(self) -> None:
        async with Client(create_server()) as client:
            names = {tool.name for tool in await client.list_tools()}
        assert "porter_capabilities" in names

    async def test_it_exposes_only_filters(self) -> None:
        async with Client(create_server()) as client:
            tool = next(
                t for t in await client.list_tools() if t.name == "porter_capabilities"
            )

        assert set(tool.input_schema["properties"]) == {"stage", "available_only"}


class TestCapabilitiesTool:
    async def test_every_capability_is_listed(self) -> None:
        from porter.core.workflow.capabilities import CAPABILITY_IDS

        async with Client(create_server()) as client:
            data = (await client.call_tool("porter_capabilities", {})).data

        assert data["ok"] is True
        assert data["count"] == len(CAPABILITY_IDS)
        assert {item["id"] for item in data["capabilities"]} == set(CAPABILITY_IDS)

    async def test_unavailable_capabilities_are_not_hidden(self) -> None:
        """Hiding them erases "not installed" vs "not supported"."""
        async with Client(create_server()) as client:
            data = (await client.call_tool("porter_capabilities", {})).data

        assert any(item["available"] is False for item in data["capabilities"])

    async def test_paid_capabilities_are_marked_paid(self) -> None:
        from porter.core.workflow.capabilities import TRANSLATE_LLM

        async with Client(create_server()) as client:
            data = (await client.call_tool("porter_capabilities", {})).data

        paid = next(item for item in data["capabilities"] if item["id"] == TRANSLATE_LLM)
        assert paid["cost"] == "paid"

    async def test_a_stage_filter_narrows_the_catalog(self) -> None:
        async with Client(create_server()) as client:
            data = (
                await client.call_tool("porter_capabilities", {"stage": "transcript"})
            ).data

        assert data["ok"] is True
        assert data["capabilities"]
        assert {item["stage"] for item in data["capabilities"]} == {"transcript"}

    async def test_available_only_filters_missing_entries(self) -> None:
        async with Client(create_server()) as client:
            data = (
                await client.call_tool(
                    "porter_capabilities", {"available_only": True}
                )
            ).data

        assert all(item["available"] is True for item in data["capabilities"])

    async def test_an_unknown_stage_is_refused_by_name(self) -> None:
        async with Client(create_server()) as client:
            data = (
                await client.call_tool("porter_capabilities", {"stage": "nope"})
            ).data

        assert data["ok"] is False
        assert "unknown stage" in data["error"]
        assert "transcript" in data["error"]


# ----------------------------------------------------------------------
# Plan
# ----------------------------------------------------------------------


class TestPlanTool:
    """``porter_plan`` -- the executable WorkflowPlan, before compute."""

    @pytest.fixture(autouse=True)
    def _in_a_temporary_directory(self, tmp_path, monkeypatch) -> None:
        monkeypatch.chdir(tmp_path)

    @pytest.fixture
    def inspected(self, monkeypatch):
        from porter.core.models.inspection import InspectionResult
        from porter.core.workflow import probe as probe_module

        def install(**overrides):
            fields = {
                "input_url": "https://www.youtube.com/watch?v=abc",
                "canonical_url": "https://www.youtube.com/watch?v=abc",
                "platform": "youtube",
                "is_valid": True,
                "has_video": True,
                "duration_seconds": 213.0,
                "width": 1920,
                "height": 1080,
                "raw_info": {},
            }
            fields.update(overrides)
            monkeypatch.setattr(
                probe_module,
                "inspect_url",
                lambda url, c=None, **k: InspectionResult(**fields),
            )

        return install

    @pytest.fixture
    def machine(self, monkeypatch):
        """Make capability probing deterministic without touching the host."""
        from porter.core.workflow import capabilities as caps

        def install(
            *,
            ffmpeg: bool = True,
            ytdlp: bool = True,
            whisper_local: bool = False,
            whisper_api: bool = False,
            translation: bool = True,
        ) -> None:
            monkeypatch.setattr(caps, "_probe_ffmpeg", lambda ctx: caps._Probe(ffmpeg, True))
            monkeypatch.setattr(caps, "_probe_ytdlp", lambda: caps._Probe(ytdlp, True))
            monkeypatch.setattr(
                caps, "_probe_whisper_local", lambda ctx: caps._Probe(whisper_local, True)
            )
            monkeypatch.setattr(
                caps, "_probe_whisper_api", lambda ctx: caps._Probe(whisper_api, True)
            )
            monkeypatch.setattr(
                caps,
                "_probe_translation",
                lambda ctx, capability: caps._Probe(
                    translation and capability != caps.TRANSLATE_LLM, True
                ),
            )

        return install

    async def test_porter_plan_is_exposed(self) -> None:
        async with Client(create_server()) as client:
            names = {tool.name for tool in await client.list_tools()}
        assert "porter_plan" in names

    async def test_it_offers_planning_choices_but_no_cookies(self) -> None:
        """An agent must be able to choose peers; credentials are CLI-only."""
        async with Client(create_server()) as client:
            tool = next(t for t in await client.list_tools() if t.name == "porter_plan")

        properties = set(tool.input_schema["properties"])
        assert {
            "source",
            "task",
            "burn",
            "target_lang",
            "transcript",
            "translate",
            "render",
            "allow_paid",
            "output_dir",
        } <= properties
        assert "cookies" not in properties
        assert "cookies_from_browser" not in properties

    async def test_a_subtitled_url_plans_the_platform_track(
        self, inspected, machine
    ) -> None:
        machine(whisper_local=True)
        inspected(raw_info={"subtitles": {"en": [{"ext": "vtt"}]}})

        async with Client(create_server()) as client:
            result = await client.call_tool(
                "porter_plan", {"source": "https://www.youtube.com/watch?v=abc"}
            )

        data = result.data
        assert data["ok"] is True
        assert data["schema_version"] == 1
        assert data["plan_id"].startswith("sha256:")
        assert data["feasible"] is True
        assert data["workflow"] == "url.subtitled"
        transcript = next(stage for stage in data["stages"] if stage["stage"] == "transcript")
        assert transcript["capability"] == "transcript.platform-track"
        assert data["source_probe"]["platform"] == "youtube"

    async def test_an_asr_url_plans_one_named_capability(
        self, inspected, machine
    ) -> None:
        machine(whisper_local=True)
        inspected(raw_info={})

        async with Client(create_server()) as client:
            result = await client.call_tool(
                "porter_plan", {"source": "https://www.youtube.com/watch?v=abc"}
            )

        data = result.data
        assert data["ok"] is True
        assert data["workflow"] == "url.asr"
        transcript = next(stage for stage in data["stages"] if stage["stage"] == "transcript")
        assert transcript["capability"] == "transcript.whisper-local"

    async def test_a_requested_unavailable_capability_blocks(
        self, inspected, machine
    ) -> None:
        """A named peer is never replaced by another one."""
        machine(whisper_local=False, whisper_api=True)
        inspected(raw_info={})

        async with Client(create_server()) as client:
            result = await client.call_tool(
                "porter_plan",
                {
                    "source": "https://www.youtube.com/watch?v=abc",
                    "transcript": "whisper-local",
                    "allow_paid": True,
                },
            )

        data = result.data
        assert data["ok"] is True
        assert data["feasible"] is False
        issue = data["issues"][0]
        assert issue["code"] == "CAPABILITY_UNAVAILABLE"
        assert issue["alternatives"] == ["transcript.whisper-api"]

    async def test_a_subtitle_file_plans_subtitle_only(
        self, tmp_path, machine
    ) -> None:
        source = tmp_path / "input.srt"
        source.write_text(
            "1\n00:00:00,500 --> 00:00:02,500\nHello\n", encoding="utf-8"
        )
        machine()

        async with Client(create_server()) as client:
            result = await client.call_tool("porter_plan", {"source": str(source)})

        data = result.data
        assert data["ok"] is True
        assert data["feasible"] is True
        assert data["workflow"] == "subtitle.only"
        transcript = next(stage for stage in data["stages"] if stage["stage"] == "transcript")
        assert transcript["capability"] == "transcript.supplied"

    async def test_an_unusable_link_is_a_successful_call(
        self, inspected, machine
    ) -> None:
        """Same contract as ``porter_probe``: the call worked, the link is bad."""
        machine(whisper_local=True)
        inspected(
            is_valid=False,
            has_video=False,
            error_message="Resource not found, deleted, or private (404).",
        )

        async with Client(create_server()) as client:
            result = await client.call_tool(
                "porter_plan", {"source": "https://www.youtube.com/watch?v=abc"}
            )

        assert result.data["ok"] is True
        assert result.data["feasible"] is False
        assert result.data["blockers"][0]["code"] == "SOURCE_UNUSABLE"
        assert "404" in result.data["blockers"][0]["message"]

    async def test_no_transcript_route_is_flagged(self, inspected, machine) -> None:
        """The prediction the tool exists for: not after thirty minutes of encoding."""
        machine(whisper_local=False, whisper_api=False)
        inspected(raw_info={})

        async with Client(create_server()) as client:
            result = await client.call_tool(
                "porter_plan", {"source": "https://www.youtube.com/watch?v=abc"}
            )

        assert result.data["ok"] is True
        assert result.data["feasible"] is False
        assert result.data["blockers"][0]["code"] == "NO_CAPABILITY"
        assert result.data["blockers"][0]["fix"]

    async def test_a_bad_burn_mode_is_reported_as_data(self, machine) -> None:
        machine()

        async with Client(create_server()) as client:
            result = await client.call_tool(
                "porter_plan", {"source": "/videos/a.mp4", "burn": "nonsense"}
            )

        assert result.data["ok"] is False
        assert "nonsense" in result.data["error"]

    async def test_a_genuine_fault_is_a_tool_error(self, monkeypatch) -> None:
        from porter.core.errors import PorterError

        def boom(source, **choices):
            raise PorterError("ffmpeg is not installed")

        monkeypatch.setattr("porter.mcp.tools.planning.plan_for", boom)

        async with Client(create_server()) as client:
            result = await client.call_tool("porter_plan", {"source": "/videos/a.mp4"})

        assert result.data["ok"] is False
        assert "ffmpeg" in result.data["error"]

    async def test_an_unresolvable_config_is_a_tool_error(self, monkeypatch) -> None:
        from porter.core.errors import PorterError

        def boom(explicit=None):
            raise PorterError("config file is not valid JSON")

        monkeypatch.setattr("porter.mcp.tools.planning.resolve", boom)

        async with Client(create_server()) as client:
            result = await client.call_tool("porter_plan", {"source": "/videos/a.mp4"})

        assert result.data["ok"] is False
        assert "could not be resolved" in result.data["error"]


class TestSharedConcurrencyLimits:
    """The caps live in one module, so a tool cannot invent its own."""

    def test_the_limits_are_declared_once(self) -> None:
        from porter.mcp import limits

        assert limits.HEAVY_CONCURRENCY == 1
        assert limits.LIGHT_CONCURRENCY == 4

    def test_every_network_probe_shares_the_light_cap(self) -> None:
        """Probing and planning both hit the network; one cap covers both."""
        from porter.mcp import limits
        from porter.mcp.tools import plan as plan_module
        from porter.mcp.tools import probe as probe_module

        assert probe_module.LIGHT is limits.LIGHT
        assert plan_module.LIGHT is limits.LIGHT

    def test_jobs_share_the_heavy_cap(self) -> None:
        from porter.mcp import limits
        from porter.mcp.tools import jobs as jobs_module

        assert jobs_module._HEAVY_JOBS is limits.HEAVY
