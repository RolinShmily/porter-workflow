"""Tests for the documentation resources and the workflow prompt.

The important tests here are the *derivation* ones. Prose cannot be tested for
correctness, but a table of platforms or backends can: the whole reason
``docs.py`` reads them out of the registry and the assembled pipeline is so that
adding a backend cannot leave the documentation wrong. Those tests assert the
table matches the engine exactly, which is the property that would otherwise rot
silently.
"""

from __future__ import annotations

import json

import pytest

pytest.importorskip("fastmcp", reason="requires the [mcp] extra")

from fastmcp import Client

from porter_mcp.server import create_server
from porter_mcp.tools.docs import ARCHITECTURE_URI, CONFIG_URI, PROMPT_NAME


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

        # §8.2's three resources. The job log is a template, not a static URI,
        # and is asserted below.
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
    async def test_it_names_every_phase_from_the_enum(self) -> None:
        from porter.events import Phase

        async with Client(create_server()) as client:
            doc = await _read(client, ARCHITECTURE_URI)

        for phase in Phase:
            assert f"`{phase.value}`" in doc

    async def test_the_platform_table_tracks_the_registry(self) -> None:
        """The derivation guarantee: a new platform appears without an edit here."""
        from porter.platforms import registry

        async with Client(create_server()) as client:
            doc = await _read(client, ARCHITECTURE_URI)

        for spec in (h.spec for h in registry().handlers()):
            assert f"| `{spec.name}` | {spec.display_name} |" in doc

    async def test_the_backend_table_tracks_the_assembled_chains(self) -> None:
        """Same guarantee for backends, from the pipeline the engine builds."""
        from porter.context import RunContext
        from porter.models.request import JobOptions
        from porter.pipeline import Pipeline

        ctx = RunContext(job_id="t", options=JobOptions())
        pipeline = Pipeline.default(ctx)

        async with Client(create_server()) as client:
            doc = await _read(client, ARCHITECTURE_URI)

        for chain_name, chain in (
            ("asr", pipeline.transcriber),
            ("translate", pipeline.translator),
        ):
            for backend in chain.backends:
                verified = "yes" if backend.endpoint_verified else "no"
                assert f"| `{backend.name}` | {chain_name} | {verified} |" in doc

    async def test_it_says_the_verified_column_is_not_live_availability(self) -> None:
        """Otherwise an agent reads "verified: no" as "broken" and gives up."""
        async with Client(create_server()) as client:
            doc = await _read(client, ARCHITECTURE_URI)

        assert "not a live availability check" in doc
        assert "porter_doctor" in doc

    async def test_it_records_which_platforms_reuse_a_chinese_track(self) -> None:
        async with Client(create_server()) as client:
            doc = await _read(client, ARCHITECTURE_URI)

        # Derived from the spec flags, so this reflects the real policy.
        assert "Chinese reused when present" in doc

    async def test_it_explains_that_only_phase_stops_rather_than_isolates(self) -> None:
        """The single most misread option in the whole tool surface."""
        async with Client(create_server()) as client:
            doc = await _read(client, ARCHITECTURE_URI)

        assert "stop after this phase" in doc
        assert "not *run only this phase*" in doc

    async def test_it_does_not_probe_anything(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A resource read must not open sockets.

        Availability is ``porter_doctor``'s and ``porter_plan``'s job. If this
        document probed, fetching it to render documentation would have side
        effects -- and would be slow.
        """
        from porter.asr import chain as asr_chain
        from porter.translate import chain as translate_chain

        def _boom(*_a: object, **_k: object) -> bool:
            raise AssertionError("the architecture document probed a backend")

        # ``_probe`` is a module-level function in each chain module, not a method.
        monkeypatch.setattr(asr_chain, "_probe", _boom)
        monkeypatch.setattr(translate_chain, "_probe", _boom)

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
        from porter.errors import PorterError

        def _boom(_explicit: object) -> object:
            raise PorterError("config file is not valid JSON")

        monkeypatch.setattr("porter_mcp.tools.docs.resolve", _boom)

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
            "porter_inspect",
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
        """The lesson from the 429 that cost a real job this session."""
        async with Client(create_server()) as client:
            text = await _prompt_text(client)
        assert "requested, not guaranteed" in text
        assert "429" in text

    async def test_it_points_at_blocking_issues(self) -> None:
        async with Client(create_server()) as client:
            text = await _prompt_text(client)
        assert "blocking_issues" in text
        assert "stop" in text

    async def test_it_explains_why_the_job_api_must_be_polled(self) -> None:
        async with Client(create_server()) as client:
            text = await _prompt_text(client)
        assert "timeout" in text
        assert "polled" in text

    async def test_it_says_recognition_needs_a_key(self) -> None:
        async with Client(create_server()) as client:
            text = await _prompt_text(client)
        assert "Whisper API key" in text

    async def test_a_source_argument_is_prepended(self) -> None:
        async with Client(create_server()) as client:
            text = await _prompt_text(client, {"source": "https://youtu.be/abc"})

        assert text.startswith("Localise this video: https://youtu.be/abc")
        assert "porter_inspect" in text

    async def test_it_names_the_diagnostic_tools(self) -> None:
        async with Client(create_server()) as client:
            text = await _prompt_text(client)
        assert "porter_doctor" in text
        assert "porter_config" in text
