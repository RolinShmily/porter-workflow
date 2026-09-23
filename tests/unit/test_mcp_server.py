"""Smoke tests for the MCP server: does it build, and do tools answer?"""

from __future__ import annotations

import os

import pytest

pytest.importorskip("fastmcp", reason="requires the [mcp] extra")

from fastmcp import Client

from porter import __version__
from porter.jobs import process_marker
from porter_mcp.server import create_server


class TestServerConstruction:
    def test_create_server_returns_a_server(self) -> None:
        assert create_server() is not None

    def test_server_import_is_cheap_without_mcp_extra(self) -> None:
        """``import porter_mcp`` must not require fastmcp.

        The package ``__init__`` stays dependency-light so that tooling and
        tests can import it without the optional extra installed.
        """
        import porter_mcp

        assert porter_mcp.__doc__


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
        from porter.doctor import GUIDES

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
    from porter.jobs import JobRegistry
    from porter_mcp.tools import jobs as jobs_module

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
        from porter.events import JobState
        from porter.jobs import JobRecord

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
        from porter.events import JobState
        from porter.jobs import JobRecord

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
        from porter.events import JobState
        from porter.jobs import JobRecord

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
        from porter.events import JobState
        from porter.jobs import JobRecord

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
        from porter.events import JobState
        from porter.jobs import JobRecord

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
        from porter.jobs import JobRecord

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

    async def test_a_valid_source_returns_a_job_id_immediately(
        self, _isolated_registry, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The whole point of the job API: the call returns before the work does.

        ``_run_job`` is replaced rather than allowed to run. Starting a real job
        here would launch a pipeline in a background thread, log its failure into
        the test output, and make the assertion depend on how far that thread got
        before the test ended.
        """
        from porter_mcp.tools import jobs as jobs_module

        launched: list[str] = []
        monkeypatch.setattr(
            jobs_module,
            "_run_job",
            lambda job, ctx, request: launched.append(job.job_id),
        )

        async with Client(create_server()) as client:
            result = await client.call_tool(
                "porter_job_start", {"source": "/videos/a.mp4"}
            )

        assert result.data["ok"] is True
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
        from porter_mcp.tools import jobs as jobs_module

        observed: dict[str, object] = {}

        def capture(job, ctx, request) -> None:
            observed["cancel_is_wired"] = job.cancel is ctx.cancel
            observed["running"] = job.state.value

        monkeypatch.setattr(jobs_module, "_run_job", capture)

        async with Client(create_server()) as client:
            await client.call_tool("porter_job_start", {"source": "/videos/a.mp4"})

        assert observed["cancel_is_wired"] is True
        assert observed["running"] == "running"


class TestRunJobThreadBody:
    """``_run_job`` must never let an exception escape.

    A background thread that dies silently leaves the job at ``running`` forever.
    The registry's staleness detection would eventually paper over it, but only
    while the process is alive, and it would report "killed or crashed" for what
    was an ordinary error. Recording the outcome is strictly better.
    """

    def _job_and_ctx(self, tmp_path):
        from porter.config import PorterConfig
        from porter.context import RunContext
        from porter.events import null_sink
        from porter.jobs import JobStore
        from porter.models.request import JobOptions, JobRequest

        store = JobStore()
        request = JobRequest.from_source("/videos/a.mp4", JobOptions())
        job = store.create(request)
        ctx = RunContext(
            job_id=job.job_id,
            options=JobOptions(output_dir=tmp_path / "out"),
            config=PorterConfig(),
            events=null_sink,
        )
        store.attach(job, ctx)
        return job, ctx, request

    def test_an_expected_error_becomes_a_failed_result(
        self, tmp_path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from porter.errors import ExtractionError
        from porter.models.request import JobResult
        from porter.pipeline import Pipeline
        from porter_mcp.tools import jobs as jobs_module

        job, ctx, request = self._job_and_ctx(tmp_path)

        def explode(self, req, context) -> JobResult:
            raise ExtractionError("local video not found: /videos/a.mp4")

        monkeypatch.setattr(Pipeline, "run", explode)

        jobs_module._run_job(job, ctx, request)

        assert job.state.value == "failed"
        assert "not found" in (job.result.error.message or "")

    def test_an_unexpected_error_is_still_recorded(
        self, tmp_path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from porter.models.request import JobResult
        from porter.pipeline import Pipeline
        from porter_mcp.tools import jobs as jobs_module

        job, ctx, request = self._job_and_ctx(tmp_path)

        def explode(self, req, context) -> JobResult:
            raise ZeroDivisionError("bug in a backend")

        monkeypatch.setattr(Pipeline, "run", explode)

        jobs_module._run_job(job, ctx, request)

        assert job.state.value == "failed"
        assert job.result.error.code == "internal_error"
        assert "ZeroDivisionError" in (job.result.error.message or "")

    def test_cancellation_becomes_a_cancelled_result(
        self, tmp_path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from porter.errors import JobCancelled
        from porter.models.request import JobResult
        from porter.pipeline import Pipeline
        from porter_mcp.tools import jobs as jobs_module

        job, ctx, request = self._job_and_ctx(tmp_path)

        def explode(self, req, context) -> JobResult:
            raise JobCancelled("cancelled by the client")

        monkeypatch.setattr(Pipeline, "run", explode)

        jobs_module._run_job(job, ctx, request)

        assert job.state.value == "cancelled"

    def test_a_cancel_while_queued_prevents_the_work(
        self, tmp_path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A job cancelled while waiting its turn must not start when it gets one."""
        from porter.events import JobState
        from porter.models.request import JobResult
        from porter.pipeline import Pipeline
        from porter_mcp.tools import jobs as jobs_module

        job, ctx, request = self._job_and_ctx(tmp_path)
        ran: list[str] = []

        def record(self, req, context) -> JobResult:
            ran.append("ran")
            return JobResult(job_id=job.job_id, state=JobState.DONE)

        monkeypatch.setattr(Pipeline, "run", record)
        # Cancelled before the semaphore is even acquired.
        job.cancel.set()

        jobs_module._run_job(job, ctx, request)

        assert ran == []
        assert job.state.value == "cancelled"


class TestInspectToolContract:
    """``porter_inspect``'s schema is part of the safety contract, not just a shape."""

    async def test_porter_inspect_is_exposed(self) -> None:
        async with Client(create_server()) as client:
            names = {tool.name for tool in await client.list_tools()}
        assert "porter_inspect" in names

    async def test_it_accepts_a_source_and_nothing_else(self) -> None:
        """No cookie parameters, deliberately.

        The CLI has ``--cookies`` / ``--cookies-from-browser``. Exposing them over
        MCP would write credentials into the conversation transcript and into any
        telemetry with it (§8.5). Cookies still work here -- they are read from the
        resolved config, which only the CLI can write.

        Asserted on the schema rather than trusted, because the tempting change is
        to "just add one parameter" for a link that needs authentication.
        """
        async with Client(create_server()) as client:
            tool = next(t for t in await client.list_tools() if t.name == "porter_inspect")

        assert set(tool.input_schema["properties"]) == {"source"}


class TestInspectTool:
    """Result shaping: what an agent can act on, and what it must not retry."""

    @pytest.fixture
    def fake_inspect(self, monkeypatch):
        """Replace the engine call so shaping can be tested on its own."""
        from porter.platforms import inspector as inspector_module

        def install(result_or_exc):
            def fake(url, ctx=None, **kwargs):
                if isinstance(result_or_exc, Exception):
                    raise result_or_exc
                return result_or_exc

            monkeypatch.setattr(inspector_module, "inspect_url", fake)

        return install

    @staticmethod
    def _valid() -> object:
        from porter.models.inspection import InspectionResult

        return InspectionResult(
            input_url="https://x.com/a/status/1",
            canonical_url="https://x.com/a/status/1",
            platform="x",
            is_valid=True,
            has_video=True,
            video_id="1",
            title="A Vertical Clip",
            duration_seconds=42.0,
            width=1080,
            height=1920,
            is_vertical=True,
            has_subtitles=False,
        )

    async def test_a_usable_link_returns_the_structured_result(self, fake_inspect) -> None:
        fake_inspect(self._valid())

        async with Client(create_server()) as client:
            result = await client.call_tool("porter_inspect", {"source": "https://x.com/a/status/1"})

        data = result.data
        assert data["ok"] is True
        assert data["is_valid"] is True
        assert data["platform"] == "x"
        assert data["duration_seconds"] == 42.0
        assert data["is_vertical"] is True

    async def test_the_rendered_summary_comes_back_too(self, fake_inspect) -> None:
        """An agent needs something to say to its user without a second call."""
        fake_inspect(self._valid())

        async with Client(create_server()) as client:
            result = await client.call_tool("porter_inspect", {"source": "https://x.com/a/status/1"})

        assert "A Vertical Clip" in result.data["summary"]

    async def test_an_unusable_link_is_a_successful_call(self, fake_inspect) -> None:
        """The contract that stops an agent burning its retry budget.

        ``ok`` describes the call; ``is_valid`` describes the link. A 404 reported
        as ``ok: false`` reads as "the tool broke", and the natural response --
        retry -- is exactly wrong.
        """
        from porter.models.inspection import InspectionResult

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
                "porter_inspect", {"source": "https://www.youtube.com/watch?v=dead"}
            )

        assert result.data["ok"] is True, "a bad link is a result, not a tool failure"
        assert result.data["is_valid"] is False
        assert "404" in result.data["error_message"]

    async def test_a_local_path_explains_itself(self) -> None:
        """Not "unsupported platform", which would be misleading.

        A local file is a legitimate input to ``porter_job_start``, so an agent
        that calls inspect on one deserves to be told that inspection is about
        links -- and that the file needs no pre-flight check.
        """
        async with Client(create_server()) as client:
            result = await client.call_tool("porter_inspect", {"source": "/videos/a.mp4"})

        assert result.data["ok"] is True
        assert result.data["is_valid"] is False
        assert result.data["platform"] == "local"
        assert "probes links, not local files" in result.data["error_message"]
        assert "porter_job_start" in result.data["error_message"]

    async def test_a_file_url_is_also_recognised_as_local(self) -> None:
        async with Client(create_server()) as client:
            result = await client.call_tool(
                "porter_inspect", {"source": "file:///videos/a.mp4"}
            )

        assert result.data["platform"] == "local"

    async def test_a_genuine_fault_is_a_tool_error(self, fake_inspect) -> None:
        """A missing dependency is not a bad link, and must not look like one."""
        from porter.errors import PorterError

        fake_inspect(PorterError("ffmpeg is not installed"))

        async with Client(create_server()) as client:
            result = await client.call_tool("porter_inspect", {"source": "https://x.com/a/status/1"})

        assert result.data["ok"] is False
        assert "ffmpeg" in result.data["error"]

    async def test_an_unresolvable_config_is_a_tool_error(self, monkeypatch) -> None:
        from porter.config import PorterConfig  # noqa: F401  (imported for clarity)
        from porter.errors import PorterError

        def boom(explicit=None):
            raise PorterError("config file is not valid JSON")

        monkeypatch.setattr("porter.config.resolve", boom)

        async with Client(create_server()) as client:
            result = await client.call_tool("porter_inspect", {"source": "https://x.com/a/status/1"})

        assert result.data["ok"] is False
        assert "could not be resolved" in result.data["error"]


class TestInspectToolIntegration:
    """The real ``inspect_url``, through the registry, with only yt-dlp faked.

    The shaping tests above prove the tool's own logic; this proves the tool is
    actually wired to the engine rather than to a convenient stand-in.
    """

    @pytest.fixture
    def ydl(self, monkeypatch):
        from types import SimpleNamespace

        from porter.platforms import inspector as inspector_module

        state = SimpleNamespace(info=None, raises=None)

        class FakeYDL:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def extract_info(self, url, download=False):
                if state.raises is not None:
                    raise state.raises
                return state.info

            def download(self, urls):  # pragma: no cover - inspection must not download
                raise AssertionError("inspection must not download")

        monkeypatch.setattr("porter.platforms.base.build_ydl", lambda *a, **k: FakeYDL())
        # Zero the retry backoff so a failure test does not sleep 4.5 seconds.
        monkeypatch.setattr(inspector_module, "BACKOFF_BASE_SECONDS", 0.0)
        return state

    @staticmethod
    def _info(**overrides):
        info = {
            "id": "abc123",
            "title": "A Real Probe",
            "uploader": "Someone",
            "duration": 125.0,
            "width": 1920,
            "height": 1080,
            "formats": [{"vcodec": "avc1", "width": 1920, "height": 1080}],
            "subtitles": {"en": [{"ext": "vtt"}]},
            "webpage_url": "https://www.youtube.com/watch?v=abc123",
        }
        info.update(overrides)
        return info

    async def test_a_real_probe_reports_measured_facts(self, ydl, tmp_path, monkeypatch) -> None:
        monkeypatch.chdir(tmp_path)
        ydl.info = self._info()

        async with Client(create_server()) as client:
            result = await client.call_tool(
                "porter_inspect", {"source": "https://www.youtube.com/watch?v=abc123"}
            )

        data = result.data
        assert data["ok"] is True
        assert data["platform"] == "youtube"
        assert data["title"] == "A Real Probe"
        assert (data["width"], data["height"]) == (1920, 1080)
        assert data["is_vertical"] is False
        assert data["has_subtitles"] is True
        assert data["video_id"] == "abc123"

    async def test_a_photo_carousel_is_reported_as_having_no_video(
        self, ydl, tmp_path, monkeypatch
    ) -> None:
        """The v0.1 bug this guards: formats present but no video codec.

        v0.1 computed ``has_video_stream``, used it for the rejection, then fell
        through and returned ``is_valid=True, has_video=True`` anyway. A test with
        ``formats: []`` could not see it.
        """
        monkeypatch.chdir(tmp_path)
        ydl.info = self._info(formats=[{"vcodec": "none", "width": 1080, "height": 1080}])

        async with Client(create_server()) as client:
            result = await client.call_tool(
                "porter_inspect", {"source": "https://www.youtube.com/watch?v=abc123"}
            )

        assert result.data["is_valid"] is False
        assert "does not contain any video" in result.data["error_message"]

    async def test_an_unsupported_host_names_the_supported_ones(self, ydl, tmp_path, monkeypatch) -> None:
        monkeypatch.chdir(tmp_path)

        async with Client(create_server()) as client:
            result = await client.call_tool(
                "porter_inspect", {"source": "https://example.com/v/1"}
            )

        assert result.data["is_valid"] is False
        assert "youtube" in result.data["error_message"]

    async def test_a_dead_link_is_not_retried_into_a_wall_of_text(
        self, ydl, tmp_path, monkeypatch
    ) -> None:
        """A 404 returns at once with advice, not the raw yt-dlp error."""
        monkeypatch.chdir(tmp_path)
        ydl.raises = RuntimeError("ERROR: Video unavailable")

        async with Client(create_server()) as client:
            result = await client.call_tool(
                "porter_inspect", {"source": "https://www.youtube.com/watch?v=abc123"}
            )

        assert result.data["is_valid"] is False
        assert result.data["error_message"] == "Resource not found, deleted, or private (404)."


class TestInspectConcurrency:
    """§8.4's LIGHT cap: cheap locally, not remotely."""

    async def test_inspections_are_capped(self, monkeypatch, tmp_path) -> None:
        """An agent fanning out must not open one socket per link.

        The bound is what is asserted, not the exact number: the cap exists to
        protect the remote platform, and a lower effective concurrency is fine.
        """
        import asyncio
        import threading

        from porter.models.inspection import InspectionResult
        from porter.platforms import inspector as inspector_module
        from porter_mcp import limits

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

        monkeypatch.setattr(inspector_module, "inspect_url", slow)

        async with Client(create_server()) as client:
            await asyncio.gather(
                *(
                    client.call_tool("porter_inspect", {"source": f"https://x.com/a/{i}"})
                    for i in range(limits.LIGHT_CONCURRENCY * 2)
                )
            )

        assert state["peak"] <= limits.LIGHT_CONCURRENCY, (
            f"peak concurrency {state['peak']} exceeded the cap"
        )
        assert state["peak"] > 1, "inspections should still run in parallel"


class TestPlanTool:
    """``porter_plan`` — the resolved execution plan, before committing compute."""

    @pytest.fixture(autouse=True)
    def _in_a_temporary_directory(self, tmp_path, monkeypatch) -> None:
        monkeypatch.chdir(tmp_path)

    @pytest.fixture
    def inspected(self, monkeypatch):
        from porter.models.inspection import InspectionResult
        from porter.platforms import inspector as inspector_module

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
                inspector_module,
                "inspect_url",
                lambda url, c=None, **k: InspectionResult(**fields),
            )

        return install

    @pytest.fixture
    def asr_available(self, monkeypatch):
        from porter.asr.chain import AsrChain

        def install(entries):
            monkeypatch.setattr(AsrChain, "availability", lambda self, ctx: list(entries))

        return install

    async def test_porter_plan_is_exposed(self) -> None:
        async with Client(create_server()) as client:
            names = {tool.name for tool in await client.list_tools()}
        assert "porter_plan" in names

    async def test_it_accepts_a_source_and_nothing_else(self) -> None:
        """Options are deliberately not accepted.

        The plan describes the default run. An agent that wants different flags
        passes them to ``porter_job_start`` and reads the plan's phase list to know
        what it is choosing between; accepting options here would mean two
        frontends deriving a job's shape from two different places.
        """
        async with Client(create_server()) as client:
            tool = next(t for t in await client.list_tools() if t.name == "porter_plan")

        assert set(tool.input_schema["properties"]) == {"source"}

    async def test_a_plan_reports_the_resolved_route(self, inspected, asr_available) -> None:
        asr_available([("whisper-api", True)])
        inspected(raw_info={"subtitles": {"en": [{}]}})

        async with Client(create_server()) as client:
            result = await client.call_tool(
                "porter_plan", {"source": "https://www.youtube.com/watch?v=abc"}
            )

        data = result.data
        assert data["ok"] is True
        assert data["feasible"] is True
        assert data["subtitles"]["route"] == "platform"
        assert data["subtitles"]["asr_runs"] is False
        assert data["platform"] == "youtube"

    async def test_it_reports_where_the_artifacts_will_land(self, inspected, asr_available) -> None:
        """So "where did it go?" needs no second call, and it is not a guess."""
        asr_available([("whisper-api", True)])
        inspected(raw_info={"subtitles": {"en": [{}]}})

        async with Client(create_server()) as client:
            result = await client.call_tool(
                "porter_plan", {"source": "https://www.youtube.com/watch?v=abc"}
            )

        assert result.data["output_dir"]

    async def test_a_local_file_plans_asr(self, asr_available) -> None:
        asr_available([("whisper-api", True)])

        async with Client(create_server()) as client:
            result = await client.call_tool("porter_plan", {"source": "/videos/a.mp4"})

        assert result.data["kind"] == "local"
        assert result.data["subtitles"]["route"] == "asr"

    async def test_an_unusable_link_is_a_successful_call(self, inspected, asr_available) -> None:
        """Same contract as ``porter_inspect``: the call worked, the link is bad.

        And unlike inspection, this one says what it means for the job -- which is
        the entire reason both tools exist.
        """
        asr_available([("whisper-api", True)])
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
        assert "404" in result.data["blocking_issues"][0]

    async def test_a_job_that_cannot_finish_is_flagged(self, inspected, asr_available) -> None:
        """The prediction the tool exists for: not after thirty minutes of encoding."""
        asr_available([("whisper-api", False), ("bcut", False)])
        inspected(raw_info={})

        async with Client(create_server()) as client:
            result = await client.call_tool(
                "porter_plan", {"source": "https://www.youtube.com/watch?v=abc"}
            )

        assert result.data["feasible"] is False
        assert "TRANSCRIBE will fail" in result.data["blocking_issues"][0]

    async def test_an_unverified_engine_is_flagged_as_a_note(
        self, inspected, asr_available
    ) -> None:
        asr_available([("whisper-api", False), ("bcut", True)])
        inspected(raw_info={})

        async with Client(create_server()) as client:
            result = await client.call_tool(
                "porter_plan", {"source": "https://www.youtube.com/watch?v=abc"}
            )

        assert any("unverified endpoints" in note for note in result.data["notes"])

    async def test_a_genuine_fault_is_a_tool_error(self, monkeypatch) -> None:
        from porter.errors import PorterError

        def boom(source, options=None, ctx=None):
            raise PorterError("ffmpeg is not installed")

        monkeypatch.setattr("porter.plan.plan_for", boom)

        async with Client(create_server()) as client:
            result = await client.call_tool("porter_plan", {"source": "/videos/a.mp4"})

        assert result.data["ok"] is False
        assert "ffmpeg" in result.data["error"]

    async def test_an_unresolvable_config_is_a_tool_error(self, monkeypatch) -> None:
        from porter.errors import PorterError

        def boom(explicit=None):
            raise PorterError("config file is not valid JSON")

        monkeypatch.setattr("porter.config.resolve", boom)

        async with Client(create_server()) as client:
            result = await client.call_tool("porter_plan", {"source": "/videos/a.mp4"})

        assert result.data["ok"] is False
        assert "could not be resolved" in result.data["error"]


class TestSharedConcurrencyLimits:
    """§8.4's caps live in one module, so a tool cannot invent its own."""

    def test_the_limits_are_declared_once(self) -> None:
        from porter_mcp import limits

        assert limits.HEAVY_CONCURRENCY == 1
        assert limits.LIGHT_CONCURRENCY == 4

    def test_every_network_probe_shares_the_light_cap(self) -> None:
        """Inspection and planning both hit the network; one cap covers both."""
        from porter_mcp import limits
        from porter_mcp.tools import inspect as inspect_module
        from porter_mcp.tools import plan as plan_module

        assert inspect_module.LIGHT is limits.LIGHT
        assert plan_module.LIGHT is limits.LIGHT

    def test_jobs_share_the_heavy_cap(self) -> None:
        from porter_mcp import limits
        from porter_mcp.tools import jobs as jobs_module

        assert jobs_module._HEAVY_JOBS is limits.HEAVY
