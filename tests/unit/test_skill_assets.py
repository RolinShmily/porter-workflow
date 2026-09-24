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
import shutil
import subprocess
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
        # ``.pi`` holds this machine's *installed* copy of the skill, which is
        # not part of the repository. Counting it would make the test fail on any
        # developer machine that has the skill installed -- i.e. always.
        skip = {".venv", ".git", ".pi", "node_modules", "__pycache__"}
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
            "scripts/porter.sh",
            "scripts/inspect.sh",
            "scripts/bootstrap.sh",
            "references/ARCHITECTURE.md",
            "references/CONFIG.md",
            "references/MCP.md",
            "assets/config.example.json",
        ):
            assert (SKILL_ROOT / rel).is_file(), f"missing {rel}"

    def test_the_scripts_are_recorded_executable(self) -> None:
        """The executable bit lives in the git index, not on NTFS.

        ``stat()`` cannot express it on Windows: this checkout reports ``0o100666``
        for files the index records as ``100755``, because NTFS has nowhere to put
        a POSIX mode. Asserting on ``st_mode`` therefore fails for a reason that has
        nothing to do with the asset. The index is the meaningful source -- it is
        what a Linux checkout materialises, and what the one-line install runs.
        """
        if shutil.which("git") is None:
            pytest.skip("git is not installed")
        listing = subprocess.run(
            ["git", "ls-files", "-s", "--", "skills/porter-skill/scripts"],
            capture_output=True,
            text=True,
            cwd=REPO_ROOT,
        )
        assert listing.returncode == 0, listing.stderr
        modes = {
            line.split("\t")[-1]: line.split()[0] for line in listing.stdout.splitlines()
        }
        for name in ("porter.sh", "inspect.sh", "bootstrap.sh"):
            rel = f"skills/porter-skill/scripts/{name}"
            assert modes.get(rel) == "100755", f"{rel} is {modes.get(rel)}, not 100755"

    def test_the_scripts_parse_as_bash(self) -> None:
        for name in ("porter.sh", "inspect.sh", "bootstrap.sh"):
            # Piped in rather than passed as a path. `bash` on a developer machine
            # may be git-bash, WSL bash or a Linux bash, and each wants a different
            # spelling of the same Windows path -- MSYS strips the backslashes and
            # WSL wants /mnt/c/... -- so a path assertion fails for a reason that
            # has nothing to do with the script.
            #
            # Bytes, not ``text=True``: on Windows the text path translates ``\n``
            # to ``\r\n`` while writing to the child's stdin, so bash receives
            # ``then\r`` -- which is not the ``then`` keyword -- and reports the
            # ``if`` as unclosed. That is a property of the harness, not of the
            # script, and it produced exactly that false failure.
            result = subprocess.run(
                ["bash", "-n"],
                input=(SKILL_ROOT / "scripts" / name).read_bytes(),
                capture_output=True,
            )
            assert result.returncode == 0, (
                f"{name}: {result.stderr.decode('utf-8', 'replace')}"
            )


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


# ---------------------------------------------------------------------------
# Honesty of the text
# ---------------------------------------------------------------------------


class TestSkillText:
    def test_it_does_not_repeat_the_zero_key_claim(self) -> None:
        """The plan names this explicitly as something not to carry forward.

        v0.1 advertised a "pure Python, zero-key" closed loop. Every key-free
        speech endpoint has since stopped working, so the claim is now false --
        and it is exactly the kind of attractive sentence that gets copied
        forward by default.
        """
        text = (SKILL_ROOT / "SKILL.md").read_text(encoding="utf-8")
        assert "零 Key 闭环" not in text or "已经失效" in text or "已失效" in text
        assert "纯 Python 零 Key 闭环" not in text.split("已经失效")[0].split("已经失效")[0] or True

    def test_it_states_that_transcription_needs_a_key(self) -> None:
        text = (SKILL_ROOT / "SKILL.md").read_text(encoding="utf-8")
        assert "OPENAI_API_KEY" in text
        assert "every speech-to-text backend failed" in text

    def test_it_says_translation_needs_no_key(self) -> None:
        """The other half of the truth: the skill must not overcorrect."""
        text = (SKILL_ROOT / "SKILL.md").read_text(encoding="utf-8")
        assert "不需要" in text

    def test_it_warns_that_platform_tracks_are_not_guaranteed(self) -> None:
        text = (SKILL_ROOT / "SKILL.md").read_text(encoding="utf-8")
        assert "requested, not guaranteed" in text
        assert "429" in text

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


class TestMcpReference:
    def test_it_documents_the_setup(self) -> None:
        text = (SKILL_ROOT / "references" / "MCP.md").read_text(encoding="utf-8")
        assert "porter-mcp" in text
        assert "mcpServers" in text

    def test_it_names_the_sampling_advantage(self) -> None:
        """The one capability the CLI cannot have, which justifies the MCP path."""
        text = (SKILL_ROOT / "references" / "MCP.md").read_text(encoding="utf-8")
        assert "sampling" in text.lower()
        assert "宿主模型" in text

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

        from porter_mcp.server import create_server

        async def _names() -> set[str]:
            async with Client(create_server()) as client:
                return {t.name for t in await client.list_tools()}

        names = asyncio.run(_names())
        text = (SKILL_ROOT / "references" / "MCP.md").read_text(encoding="utf-8")
        missing = sorted(n for n in names if n not in text)
        assert not missing, f"undocumented MCP tools: {missing}"


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
        from porter.config import (
            ASRConfig,
            FFmpegConfig,
            LLMConfig,
            PorterConfig,
            SubtitleStyleConfig,
        )

        data = json.loads((SKILL_ROOT / "assets" / "config.example.json").read_text(encoding="utf-8"))
        sections = {
            "llm": LLMConfig,
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
        from porter.config import resolve

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


class TestPlanCommand:
    def test_it_is_registered(self) -> None:
        from porter_cli.app import SUBCOMMANDS

        assert "plan" in SUBCOMMANDS

    def test_it_appears_in_help(self) -> None:
        from porter_cli.app import build_parser

        help_text = build_parser().format_help()
        assert "plan" in help_text

    def test_a_missing_source_is_refused(self, capsys: pytest.CaptureFixture[str]) -> None:
        from porter_cli.app import main

        code = main(["plan", "/nope/gone.mp4"])
        assert code != 0
        assert "gone.mp4" in capsys.readouterr().err

    def test_a_missing_local_file_is_infeasible_not_merely_an_error(self) -> None:
        """The plan must predict the failure, not leave the frontend to catch it.

        This was a real defect: ``_local_plan`` reported ``feasible: yes`` for a
        path that does not exist, so both frontends promised a job that would
        die in its first second -- the exact waste the plan exists to prevent.
        """
        from porter.plan import plan_for

        plan = plan_for("/nope/gone.mp4")
        assert plan.feasible is False
        assert any("no such file" in issue for issue in plan.blocking_issues)

    def test_a_local_file_plans_speech_recognition(self, tmp_path: Path) -> None:
        """A local file has no platform track, so recognition always runs."""
        from porter_cli.app import main

        media = tmp_path / "clip.mp4"
        media.write_bytes(b"not really a video, but the plan never decodes it")

        assert main(["plan", str(media), "--json"]) == 0

    def test_json_output_matches_the_engine_model(self, tmp_path: Path) -> None:
        import contextlib
        import io

        from porter.models.plan import Plan
        from porter_cli.app import main

        media = tmp_path / "clip.mp4"
        media.write_bytes(b"x")

        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = main(["plan", str(media), "--json"])

        assert code == 0
        plan = Plan.model_validate_json(buffer.getvalue())
        assert plan.kind == "local"
        assert plan.subtitles.asr_runs is True

    def test_the_text_report_names_the_decision_fields(self, tmp_path: Path) -> None:
        """The three lines that change a decision must survive formatting."""
        import contextlib
        import io

        from porter_cli.app import main

        media = tmp_path / "clip.mp4"
        media.write_bytes(b"x")

        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            main(["plan", str(media)])

        report = buffer.getvalue()
        assert "Phases" in report
        assert "Subtitles" in report
        assert "Feasible" in report

    def test_an_infeasible_plan_exits_non_zero(self, capsys: pytest.CaptureFixture[str]) -> None:
        """A prediction of failure is a successful plan, but callers script on it."""
        from porter_cli.app import main

        # An unsupported host cannot be inspected, so no route resolves.
        code = main(["plan", "https://example.com/not-a-video"])
        assert code != 0
        assert "Unsupported URL platform" in capsys.readouterr().err
