"""Tests for the Agent Skill assets and the ``porter plan`` CLI command.

The skill is the repository's actual product for agents, and until now nothing
verified it. Two classes of failure are worth catching mechanically:

* **Spec violations** -- frontmatter limits, a second ``SKILL.md`` breaking the
  one-line install, a reference to a file that does not exist. These are cheap to
  check and expensive to discover after publishing.
* **Regressions in the honesty of the text** -- the plan is explicit that v0.1's
  "pure-Python, zero-key" claim must not be copied forward, because it is false
  and an agent decides whether to invoke the skill from the description alone. A
  prose claim cannot be tested for truth, but it can be tested for *presence*,
  which is what stops it from being silently dropped.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

SKILL_ROOT = Path(__file__).resolve().parents[2] / "skills" / "porter-skill"
REPO_ROOT = Path(__file__).resolve().parents[2]


def _frontmatter() -> tuple[dict[str, object], str]:
    """Return the parsed frontmatter and the body of ``SKILL.md``."""
    yaml = pytest.importorskip("yaml", reason="requires PyYAML")
    text = (SKILL_ROOT / "SKILL.md").read_text(encoding="utf-8")
    match = re.match(r"^---\n(.*?)\n---\n(.*)$", text, re.S)
    assert match, "SKILL.md does not start with a YAML frontmatter block"
    return yaml.safe_load(match.group(1)), match.group(2)


# ---------------------------------------------------------------------------
# Layout
# ---------------------------------------------------------------------------


class TestSkillLayout:
    def test_there_is_exactly_one_skill_md_in_the_repository(self) -> None:
        """A second one turns the bare install command into a multi-select.

        ``npx skills add <repo>`` auto-discovers the skill only when there is
        exactly one ``SKILL.md``; two of them make it prompt, which breaks the
        one-line install that the README documents.
        """
        # ``.pi`` and ``.agents`` hold this machine's *installed* copies of skills,
        # which are not part of the repository. Counting them would make the test
        # fail on any developer machine that has the skill installed -- i.e. always.
        skip = {".venv", ".git", ".pi", ".agents", "node_modules", "__pycache__"}
        found = [p for p in REPO_ROOT.rglob("SKILL.md") if not skip & set(p.parts)]
        assert len(found) == 1, f"expected one SKILL.md, found {found}"

    def test_the_skill_directory_name_matches_the_frontmatter_name(self) -> None:
        """The spec requires it, and changing it breaks existing installs."""
        front, _ = _frontmatter()
        assert front["name"] == SKILL_ROOT.name

    def test_the_expected_assets_exist(self) -> None:
        for rel in (
            "SKILL.md",
            "README.md",
            "scripts/porter.py",
            "scripts/bootstrap.py",
            "references/ARCHITECTURE.md",
            "references/CONFIG.md",
            "references/MCP.md",
            "references/HOSTS.md",
            "references/PLAN.md",
            "references/SETUP.md",
            "references/WEBUI.md",
            "assets/config.example.json",
        ):
            assert (SKILL_ROOT / rel).is_file(), f"missing {rel}"

    def test_the_scripts_are_python_only(self) -> None:
        """One implementation, not a Bash/PowerShell pair to keep in sync.

        The state-directory rules and ``uvx`` invocation are identical on every
        host, so a second shell implementation is duplicate logic with a second
        set of bugs. Python is already a prerequisite of the skill.
        """
        scripts = sorted(p.name for p in (SKILL_ROOT / "scripts").iterdir() if p.is_file())
        assert scripts == ["bootstrap.py", "porter.py"], scripts

    def test_the_scripts_parse_as_python(self) -> None:
        for name in ("porter.py", "bootstrap.py"):
            source = (SKILL_ROOT / "scripts" / name).read_text(encoding="utf-8")
            compile(source, name, "exec")

    def test_the_docs_do_not_advertise_removed_shell_wrappers(self) -> None:
        stale = ("porter.sh", "porter.ps1", "inspect.sh", "bootstrap.sh", "probe.sh")
        for rel in (
            "SKILL.md",
            "README.md",
            "references/SETUP.md",
            "references/HOSTS.md",
            "references/PLAN.md",
            "references/MCP.md",
            "references/WEBUI.md",
            "references/ARCHITECTURE.md",
        ):
            text = (SKILL_ROOT / rel).read_text(encoding="utf-8")
            for name in stale:
                assert name not in text, f"{rel} still advertises {name}"


# ---------------------------------------------------------------------------
# Frontmatter
# ---------------------------------------------------------------------------


class TestFrontmatter:
    def test_name_respects_the_length_and_charset_rules(self) -> None:
        front, _ = _frontmatter()
        name = str(front["name"])
        assert len(name) <= 64
        assert re.fullmatch(r"[a-z0-9]+(-[a-z0-9]+)*", name), name

    def test_description_states_both_what_and_when(self) -> None:
        """The description is all an agent sees when deciding to load the skill."""
        front, _ = _frontmatter()
        description = str(front["description"])
        assert len(description) <= 1024
        assert "Use whenever" in description, "the description does not say when to use it"
        assert "subtitle" in description.lower()

    def test_description_carries_the_transcription_prerequisite(self) -> None:
        """An agent that only reads the description must learn ASR needs a key.

        Otherwise it invokes the skill on a keyless host, the job dies at the
        transcribe phase, and the failure looks like a porter bug.
        """
        front, _ = _frontmatter()
        description = str(front["description"])
        assert "Whisper API key" in description or "WHISPER_API_KEY" in description

    def test_compatibility_is_within_its_limit(self) -> None:
        front, _ = _frontmatter()
        assert len(str(front["compatibility"])) <= 500

    def test_the_body_stays_within_the_recommended_length(self) -> None:
        _, body = _frontmatter()
        assert len(body.splitlines()) < 500, "the SKILL.md body is meant to stay under 500 lines"

    def test_metadata_version_tracks_package_version(self) -> None:
        """The skill's metadata.version must stay aligned with porter.__version__."""
        import porter

        front, _ = _frontmatter()
        metadata = front.get("metadata", {})
        assert isinstance(metadata, dict)
        skill_ver = str(metadata.get("version", ""))
        assert skill_ver.startswith(porter.__version__.rsplit(".", 1)[0])


# ---------------------------------------------------------------------------
# Honesty of the text
# ---------------------------------------------------------------------------


class TestSkillText:
    def test_it_does_not_repeat_the_zero_key_claim(self) -> None:
        """The plan names this explicitly as something not to carry forward.

        v0.1 advertised a "pure Python, zero-key" closed loop. The only key-free
        ASR is local Whisper behind an explicit extra, and the skill must say so.
        """
        text = (SKILL_ROOT / "SKILL.md").read_text(encoding="utf-8")
        assert "纯 Python 零 Key 闭环" not in text
        assert "asr-local" in text
        assert "免 Key" in text

    def test_the_environment_comes_before_the_workflow(self) -> None:
        """The SOP's first job is to establish the complete runtime environment.

        This is the ordering the skill exists for: an agent must not plan a job
        on a machine whose capabilities it has not checked. `doctor`,
        `capabilities` and `probe` all have to appear before the workflow model.
        """
        text = (SKILL_ROOT / "SKILL.md").read_text(encoding="utf-8")

        assert "完整运行环境" in text
        assert "guidance" in text, "doctor's JSON remediation is not mentioned"

        workflow_heading = text.index("## 1. 工作流模型")
        for command in ("doctor", "capabilities", "probe"):
            assert text.index(command) < workflow_heading, (
                f"{command} is introduced after the workflow model"
            )

    def test_it_states_the_transcript_capabilities(self) -> None:
        text = (SKILL_ROOT / "SKILL.md").read_text(encoding="utf-8")
        assert "OPENAI_API_KEY" in text or "WHISPER_API_KEY" in text
        assert "asr-local" in text
        assert "whisper-api" in text
        assert "NO_CAPABILITY" in text, "the no-backend failure must be named"
        assert "platform-track" in text

    def test_it_says_translation_needs_no_key(self) -> None:
        """The other half of the truth: the skill must not overcorrect."""
        text = (SKILL_ROOT / "SKILL.md").read_text(encoding="utf-8")
        assert "免 Key" in text
        assert "Bing" in text

    def test_it_warns_that_platform_tracks_are_not_guaranteed(self) -> None:
        text = (SKILL_ROOT / "SKILL.md").read_text(encoding="utf-8")
        assert "requested, not guaranteed" in text
        assert "429" in text

    def test_it_describes_the_plan_contract_and_saves_plans(self) -> None:
        """`run --plan` is the reproducible form; a plan left only in a pipe is not."""
        text = (SKILL_ROOT / "SKILL.md").read_text(encoding="utf-8")
        assert "plan --json" in text
        assert "run --plan" in text
        assert "plan.json" in text
        assert "feasible" in text
        assert "plan_id" in text

    def test_it_forbids_fallback_and_requires_replanning(self) -> None:
        """The whole architectural change: no runtime cross-capability fallback."""
        text = (SKILL_ROOT / "SKILL.md").read_text(encoding="utf-8")
        assert "没有运行期回退链" in text or "没有回退" in text
        assert "alternatives" in text
        assert "suggested_workflow" in text
        assert "重新 plan" in text or "重新规划" in text
        assert "VideoCaptioner" not in text, "the external CLI is no longer a backend"

    def test_it_requires_paid_consent(self) -> None:
        text = (SKILL_ROOT / "SKILL.md").read_text(encoding="utf-8")
        assert "allow-paid" in text
        assert "付费" in text
        assert "cost=paid" in text or "cost" in text

    def test_it_says_to_confirm_before_starting(self) -> None:
        text = (SKILL_ROOT / "SKILL.md").read_text(encoding="utf-8")
        assert "与用户确认" in text or "确认" in text

    def test_it_tells_the_agent_to_poll_rather_than_raise_the_timeout(self) -> None:
        """v0.1 told the agent to set a 1200-second bash timeout.

        That fails on a longer video and yields no progress when it does, which
        is why the job registry exists. The skill must not teach the old trick.
        """
        text = (SKILL_ROOT / "SKILL.md").read_text(encoding="utf-8")
        assert "jobs status" in text
        assert "1200" in text  # mentioned only to say not to do it
        assert "不要" in text

    def test_it_requires_a_quality_check(self) -> None:
        """Exit code 0 is not evidence of a usable release video."""
        text = (SKILL_ROOT / "SKILL.md").read_text(encoding="utf-8")
        assert "ffprobe" in text
        assert "质检" in text

    def test_every_relative_reference_resolves(self) -> None:
        """A skill that points at a file it does not ship is worse than silent."""
        text = (SKILL_ROOT / "SKILL.md").read_text(encoding="utf-8")
        targets = re.findall(r"\]\(((?:references|assets|scripts)/[^)]+)\)", text)
        assert targets, "SKILL.md references none of its own files"
        for target in targets:
            assert (SKILL_ROOT / target).is_file(), f"SKILL.md points at missing {target}"

    def test_it_says_the_skill_does_not_install_an_mcp_server(self) -> None:
        """The assumption that would silently break the MCP path."""
        text = (SKILL_ROOT / "SKILL.md").read_text(encoding="utf-8")
        assert "不会" in text and "MCP" in text

    def test_cli_is_canonical_and_every_frontend_shares_the_plan(self) -> None:
        """CLI stays the reference contract; MCP/WebUI execute the same plan."""
        text = (SKILL_ROOT / "SKILL.md").read_text(encoding="utf-8")
        assert "CLI" in text and "MCP" in text and "WebUI" in text
        assert "WorkflowPlan" in text
        assert "porter.core.plan" not in text
        assert "未迁移" not in text

    def test_it_documents_webui_operations(self) -> None:
        text = (SKILL_ROOT / "references" / "WEBUI.md").read_text(encoding="utf-8")
        for tab in ("工作台", "任务总览", "配置", "体检"):
            assert tab in text
        assert "AWAITING_USER" in text
        assert "WorkflowPlan" in text
        assert "未迁移" not in text, "the WebUI now runs the same plan"


class TestMcpReference:
    def test_it_documents_the_setup(self) -> None:
        text = (SKILL_ROOT / "references" / "MCP.md").read_text(encoding="utf-8")
        assert "porter-mcp" in text
        assert "mcpServers" in text

    def test_it_documents_the_migrated_plan_model(self) -> None:
        """MCP runs the new plan model; the old one must not survive in docs."""
        text = (SKILL_ROOT / "references" / "MCP.md").read_text(encoding="utf-8")
        assert "WorkflowPlan" in text
        assert "execute_plan" in text
        assert "porter.core.plan" not in text
        assert "未迁移" not in text

    def test_it_states_the_no_fallback_rule(self) -> None:
        """The one property an agent must not assume away."""
        text = (SKILL_ROOT / "references" / "MCP.md").read_text(encoding="utf-8")
        assert "运行期回退链" in text

    def test_it_records_that_config_is_read_only(self) -> None:
        text = (SKILL_ROOT / "references" / "MCP.md").read_text(encoding="utf-8")
        assert "只读" in text

    def test_the_tool_mapping_covers_every_exposed_tool(self) -> None:
        """The table must not drift from the server's real tool set.

        Imported from the server rather than hardcoded, so adding a tool without
        documenting it fails here instead of silently going unnoticed.
        """
        pytest.importorskip("fastmcp", reason="requires the [mcp] extra")
        import asyncio

        from fastmcp import Client

        from porter.mcp.server import create_server

        async def _names() -> set[str]:
            async with Client(create_server()) as client:
                return {t.name for t in await client.list_tools()}

        names = asyncio.run(_names())
        text = (SKILL_ROOT / "references" / "MCP.md").read_text(encoding="utf-8")
        missing = sorted(n for n in names if n not in text)
        assert not missing, f"undocumented MCP tools: {missing}"


class TestPlanReference:
    """`references/PLAN.md` is the contract agents read before approving a plan."""

    def test_it_names_the_invariants(self) -> None:
        text = (SKILL_ROOT / "references" / "PLAN.md").read_text(encoding="utf-8")
        assert "一个阶段一个能力" in text
        assert "跨能力替换" in text

    def test_it_documents_the_request_and_result_shapes(self) -> None:
        text = (SKILL_ROOT / "references" / "PLAN.md").read_text(encoding="utf-8")
        for field in ("plan_id", "workflow", "stages", "feasible", "issues"):
            assert field in text
        for field in ("artifacts", "task_dir", "suggested_workflow", "alternatives"):
            assert field in text

    def test_it_names_every_capability(self) -> None:
        from porter.core.workflow.capabilities import CAPABILITY_IDS

        text = (SKILL_ROOT / "references" / "PLAN.md").read_text(encoding="utf-8")
        for capability in CAPABILITY_IDS:
            assert capability in text, f"PLAN.md does not document {capability}"


# ---------------------------------------------------------------------------
# assets/config.example.json must match the real models
# ---------------------------------------------------------------------------


class TestExampleConfig:
    def test_it_is_valid_json(self) -> None:
        json.loads((SKILL_ROOT / "assets" / "config.example.json").read_text(encoding="utf-8"))

    def test_every_key_exists_in_the_config_models(self) -> None:
        """The template is the first thing a user copies, so a typo is expensive.

        Validated against the pydantic models rather than a hand-written list:
        ``extra="ignore"`` means a misspelled key is accepted silently at runtime
        and the setting simply never applies, which is a genuinely nasty bug to
        chase.
        """
        from porter.core.config import (
            ASRConfig,
            FFmpegConfig,
            LLMConfig,
            PorterConfig,
            RefineConfig,
            SubtitleStyleConfig,
        )

        data = json.loads((SKILL_ROOT / "assets" / "config.example.json").read_text(encoding="utf-8"))
        sections = {
            "llm": LLMConfig,
            "refine": RefineConfig,
            "asr": ASRConfig,
            "ffmpeg": FFmpegConfig,
            "style": SubtitleStyleConfig,
        }

        top_level = set(PorterConfig.model_fields)
        for key in data:
            if key.startswith("_"):
                continue
            assert key in top_level, f"unknown top-level key {key!r}"

        for section, model in sections.items():
            assert section in data, f"the template omits the {section} section"
            valid = set(model.model_fields)
            for key in data[section]:
                if key.startswith("_"):
                    continue
                assert key in valid, f"unknown key {section}.{key}"

    def test_the_example_loads_into_the_engine(self, tmp_path: Path) -> None:
        """End to end: the template must actually be loadable, comments and all."""
        from porter.core.config import resolve

        path = tmp_path / "porter.json"
        path.write_text(
            (SKILL_ROOT / "assets" / "config.example.json").read_text(encoding="utf-8"),
            encoding="utf-8",
        )
        config = resolve(path)
        assert config.output_dir.name == "porter_output"


# ---------------------------------------------------------------------------
# porter plan (CLI)
# ---------------------------------------------------------------------------


def _stage_payload(payload: dict[str, object], stage: str) -> dict[str, object]:
    stages = payload["stages"]
    assert isinstance(stages, list)
    return next(item for item in stages if item["stage"] == stage)


class TestPlanCommand:
    """``porter plan`` emits the resolved :class:`WorkflowPlan` contract.

    The command used to print the old predictive ``porter.core.plan.Plan``. The
    refactor makes it the front half of execution: the JSON is accepted verbatim
    by ``porter run --plan``. The probes are stubbed so the assertions are about
    the CLI's contract, not about what this machine happens to have installed.
    """

    @pytest.fixture
    def offline(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
        from porter.core.workflow.capabilities import CAPABILITY_IDS
        from porter.core.workflow.model import (
            CapabilityDescriptor,
            SourceKind,
            SourceProbe,
            Stage,
        )

        media = tmp_path / "clip.mp4"
        media.write_bytes(b"x")
        probe = SourceProbe(
            kind=SourceKind.LOCAL_VIDEO,
            source=str(media),
            platform="local",
            has_video=True,
            has_audio=True,
        )
        monkeypatch.setattr(
            "porter.core.workflow.resolver.probe_source",
            lambda source, ctx: probe,
        )
        monkeypatch.setattr(
            "porter.core.workflow.resolver.capability_catalog",
            lambda ctx: tuple(
                CapabilityDescriptor(
                    id=capability,
                    stage=Stage(capability.split(".", 1)[0]),
                    available=True,
                )
                for capability in CAPABILITY_IDS
            ),
        )
        return media

    def test_it_is_registered(self) -> None:
        from porter.cli.app import SUBCOMMANDS

        assert "plan" in SUBCOMMANDS

    def test_it_appears_in_help(self) -> None:
        from porter.cli.app import build_parser

        help_text = build_parser().format_help()
        assert "plan" in help_text

    def test_a_missing_source_is_refused(self, capsys: pytest.CaptureFixture[str]) -> None:
        from porter.cli.app import main

        code = main(["plan", "/nope/gone.mp4"])
        assert code != 0
        assert "gone.mp4" in capsys.readouterr().err

    def test_a_local_file_plans_speech_recognition(self, offline, capsys) -> None:
        """A local file has no platform track, so recognition is the planned route."""
        from porter.cli.app import main

        assert main(["plan", str(offline), "--json"]) == 0

        payload = json.loads(capsys.readouterr().out)
        assert payload["workflow"] == "local.asr"
        assert payload["feasible"] is True
        transcript = _stage_payload(payload, "transcript")
        assert transcript["capability"] == "transcript.whisper-local"

    def test_json_output_matches_the_engine_model(self, offline, capsys) -> None:
        from porter.cli.app import main
        from porter.core.workflow.model import WorkflowPlan

        assert main(["plan", str(offline), "--json"]) == 0

        plan = WorkflowPlan.model_validate_json(capsys.readouterr().out)
        assert plan.source_probe.kind.value == "local_video"
        assert plan.workflow.value == "local.asr"
        assert plan.plan_id.startswith("sha256:")

    def test_the_text_report_names_the_decision_fields(self, offline, capsys) -> None:
        """The lines that change a decision must survive formatting."""
        from porter.cli.app import main

        assert main(["plan", str(offline)]) == 0

        report = capsys.readouterr().out
        assert "Workflow" in report
        assert "Stages" in report
        assert "Feasible" in report

    def test_an_infeasible_plan_exits_non_zero(self, capsys: pytest.CaptureFixture[str]) -> None:
        """A prediction of failure is a successful plan, but callers script on it."""
        from porter.cli.app import main

        code = main(["plan", "/nope/gone.mp4"])
        assert code != 0
        assert "SOURCE_NOT_FOUND" in capsys.readouterr().err
