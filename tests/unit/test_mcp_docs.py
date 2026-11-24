"""Tests for the documentation resources and the workflow prompt.

The important tests here are the *derivation* ones. Prose cannot be tested for
correctness, but a list of stages, workflows, capabilities or platforms can: the
whole reason ``docs.py`` reads them out of the engine is so that adding one
cannot leave the documentation wrong. Those tests assert the tables match the
engine exactly, which is the property that would otherwise rot silently.
"""

from __future__ import annotations

import json

import pytest

pytest.importorskip("fastmcp", reason="requires the [mcp] extra")

from fastmcp import Client

from porter.mcp.server import create_server
from porter.mcp.tools.docs import ARCHITECTURE_URI, CONFIG_URI, PROMPT_NAME


async def _read(client: Client, uri: str) -> str:
    return (await client.read_resource(uri))[0].text


async def _prompt_text(client: Client, arguments: dict[str, str] | None = None) -> str:
    result = await client.get_prompt(PROMPT_NAME, arguments or {})
    return result.messages[0].content.text


# ---------------------------------------------------------------------------
# Contract
# ---------------------------------------------------------------------------


class TestSurfaceContract:
    async def test_the_documented_resources_are_exposed(self) -> None:
        async with Client(create_server()) as client:
            uris = {str(r.uri) for r in await client.list_resources()}

        assert {ARCHITECTURE_URI, CONFIG_URI, "porter://doctor/guides"} <= uris

    async def test_the_job_log_template_is_still_exposed(self) -> None:
        async with Client(create_server()) as client:
            templates = {t.uri_template for t in await client.list_resource_templates()}
        assert "porter://jobs/{job_id}/log" in templates

    async def test_the_workflow_prompt_is_exposed(self) -> None:
        async with Client(create_server()) as client:
            prompts = {p.name for p in await client.list_prompts()}
        assert PROMPT_NAME in prompts


# ---------------------------------------------------------------------------
# porter://docs/architecture
# ---------------------------------------------------------------------------


class TestArchitectureDocument:
    async def test_it_names_every_stage_from_the_enum(self) -> None:
        from porter.core.workflow.model import Stage

        async with Client(create_server()) as client:
            doc = await _read(client, ARCHITECTURE_URI)

        for stage in Stage:
            assert f"`{stage.value}`" in doc

    async def test_it_names_every_workflow_from_the_enum(self) -> None:
        from porter.core.workflow.model import WorkflowId

        async with Client(create_server()) as client:
            doc = await _read(client, ARCHITECTURE_URI)

        for workflow in WorkflowId:
            assert f"`{workflow.value}`" in doc

    async def test_the_capability_table_tracks_the_catalog(self) -> None:
        """A new capability appears without an edit here."""
        from porter.core.workflow.capabilities import CAPABILITY_IDS

        async with Client(create_server()) as client:
            doc = await _read(client, ARCHITECTURE_URI)

        for capability in CAPABILITY_IDS:
            assert f"`{capability}`" in doc

    async def test_the_platform_table_tracks_the_registry(self) -> None:
        from porter.core.platforms import registry

        async with Client(create_server()) as client:
            doc = await _read(client, ARCHITECTURE_URI)

        for spec in (h.spec for h in registry().handlers()):
            assert f"| `{spec.name}` | {spec.display_name} |" in doc

    async def test_it_states_the_one_capability_rule(self) -> None:
        async with Client(create_server()) as client:
            doc = await _read(client, ARCHITECTURE_URI)

        assert "exactly one capability per" in doc
        assert "not a fallback chain" in doc

    async def test_it_states_the_paid_policy(self) -> None:
        async with Client(create_server()) as client:
            doc = await _read(client, ARCHITECTURE_URI)

        assert "`cost=paid`" in doc
        assert "allow_paid" in doc

    async def test_it_records_which_platforms_reuse_a_chinese_track(self) -> None:
        async with Client(create_server()) as client:
            doc = await _read(client, ARCHITECTURE_URI)

        assert "Chinese reused when present" in doc

    async def test_it_does_not_probe_anything(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A resource read must not run ffmpeg or open sockets.

        Availability is ``porter_doctor``'s and ``porter_capabilities``' job. If
        this document probed, fetching it to render documentation would have
        side effects -- and would be slow.
        """
        from porter.core.workflow import capabilities as caps

        def _boom(*_a: object, **_k: object) -> bool:
            raise AssertionError("the architecture document probed a capability")

        for name in (
            "_probe_ffmpeg",
            "_probe_ytdlp",
            "_probe_whisper_local",
            "_probe_whisper_api",
            "_probe_translation",
        ):
            monkeypatch.setattr(caps, name, _boom)

        async with Client(create_server()) as client:
            doc = await _read(client, ARCHITECTURE_URI)

        assert "porter architecture" in doc


# ---------------------------------------------------------------------------
# porter://config
# ---------------------------------------------------------------------------


class TestConfigResource:
    async def test_it_returns_parseable_json(self) -> None:
        async with Client(create_server()) as client:
            text = await _read(client, CONFIG_URI)

        data = json.loads(text)
        assert set(data) >= {"llm", "asr", "ffmpeg", "style", "output_dir"}

    async def test_a_configured_key_is_masked(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:
        """The resource and the tool must not disagree about what is safe."""
        secret = "sk-" + "resource-do-not-leak-" + "987654"
        monkeypatch.setenv("OPENAI_API_KEY", secret)
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))

        async with Client(create_server()) as client:
            text = await _read(client, CONFIG_URI)

        data = json.loads(text)
        # Guard against a vacuous pass: if the key never arrived, this test would
        # hold even with masking removed.
        assert data["llm"]["api_key"], "the key never reached the config"
        assert secret not in text
        assert data["llm"]["api_key"] != secret

    async def test_it_agrees_with_the_config_tool(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:
        """Two surfaces, one masking routine -- asserted, not assumed."""
        monkeypatch.setenv("OPENAI_API_KEY", "sk-" + "shared-masking-" + "1234")
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))

        async with Client(create_server()) as client:
            text = await _read(client, CONFIG_URI)
            tool = (await client.call_tool("porter_config", {"action": "get"})).data

        assert json.loads(text)["llm"]["api_key"] == tool["config"]["llm"]["api_key"]

    async def test_a_broken_config_is_reported_as_json(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from porter.core.errors import PorterError

        def _boom(_explicit: object) -> object:
            raise PorterError("config file is not valid JSON")

        monkeypatch.setattr("porter.mcp.tools.docs.resolve", _boom)

        async with Client(create_server()) as client:
            text = await _read(client, CONFIG_URI)

        data = json.loads(text)
        assert data["ok"] is False
        assert "not valid JSON" in data["error"]


# ---------------------------------------------------------------------------
# localize-video prompt
# ---------------------------------------------------------------------------


class TestWorkflowPrompt:
    async def test_it_walks_the_whole_loop_in_order(self) -> None:
        async with Client(create_server()) as client:
            text = await _prompt_text(client)

        order = [
            "porter_doctor",
            "porter_capabilities",
            "porter_probe",
            "porter_plan",
            "porter_job_start",
            "porter_job_status",
            "porter_job_result",
        ]
        positions = [text.index(name) for name in order]
        assert positions == sorted(positions), "the steps are out of order"

    async def test_it_says_to_confirm_before_starting(self) -> None:
        async with Client(create_server()) as client:
            text = await _prompt_text(client)
        assert "Confirm with the user" in text

    async def test_it_warns_that_platform_tracks_are_not_guaranteed(self) -> None:
        """The lesson from the 429 that cost a real job."""
        async with Client(create_server()) as client:
            text = await _prompt_text(client)
        assert "requested, not guaranteed" in text
        assert "429" in text
        assert "TRACKS_UNVERIFIED" in text

    async def test_it_points_at_blockers_and_their_fixes(self) -> None:
        async with Client(create_server()) as client:
            text = await _prompt_text(client)
        assert "blocker" in text
        assert "fix" in text

    async def test_it_explains_why_the_job_api_must_be_polled(self) -> None:
        async with Client(create_server()) as client:
            text = await _prompt_text(client)
        assert "timeout" in text
        assert "polled" in text

    async def test_it_says_there_is_no_runtime_fallback(self) -> None:
        async with Client(create_server()) as client:
            text = await _prompt_text(client)
        assert "There is no runtime fallback" in text
        assert "replan" in text.lower() or "alternatives" in text

    async def test_it_explains_paid_consent(self) -> None:
        async with Client(create_server()) as client:
            text = await _prompt_text(client)
        assert "allow_paid" in text
        assert "not consent" in text

    async def test_a_source_argument_is_prepended(self) -> None:
        async with Client(create_server()) as client:
            text = await _prompt_text(client, {"source": "https://youtu.be/abc"})

        assert text.startswith("Localise this video: https://youtu.be/abc")
        assert "porter_probe" in text

    async def test_it_names_the_diagnostic_tools(self) -> None:
        async with Client(create_server()) as client:
            text = await _prompt_text(client)
        assert "porter_doctor" in text
        assert "porter_config" in text
