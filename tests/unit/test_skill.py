"""Tests for the redesigned self-contained ``skills/porter-workflow`` skill."""

from __future__ import annotations

import importlib.util
import json
import re
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SKILL_DIR = REPO_ROOT / "skills" / "porter-workflow"
SCRIPT_PATH = SKILL_DIR / "scripts" / "porter_cli.py"


def _load_cli() -> Any:
    spec = importlib.util.spec_from_file_location("porter_skill_cli", SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TestSkillAssets:
    def test_frontmatter_and_layout(self) -> None:
        skill_md = (SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")
        assert skill_md.startswith("---\n")
        assert "\nname: porter-workflow\n" in skill_md
        frontmatter = skill_md.split("---", 2)[1]
        assert "\nversion:" not in frontmatter, "the skill is not versioned separately"
        assert SCRIPT_PATH.is_file()

    def test_every_relative_markdown_link_resolves(self) -> None:
        link_re = re.compile(r"\[[^\]]+\]\(([^)]+)\)")
        for md in sorted(SKILL_DIR.rglob("*.md")):
            text = md.read_text(encoding="utf-8")
            for target in link_re.findall(text):
                if target.startswith(("http://", "https://", "#")):
                    continue
                path_part = target.split("#", 1)[0]
                resolved = (md.parent / path_part).resolve()
                assert resolved.exists(), f"broken link {target!r} in {md}"


class TestSkillCli:
    def test_host_check_with_state_dir(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cli = _load_cli()
        state = tmp_path / ".state"
        monkeypatch.setenv("PORTER_SKILL_STATE_DIR", str(state))
        assert cli.main(["--host-check"]) == 0
        assert (state / "porter-cache").is_dir()

    def test_relative_state_dir_is_rejected(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cli = _load_cli()
        monkeypatch.setenv("PORTER_SKILL_STATE_DIR", "relative/path")
        assert cli.main(["--host-check"]) == 2

    def test_uv_bootstrap_reexec_command(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cli = _load_cli()
        captured: dict[str, Any] = {}

        def fake_spawn(command: list[str], env: dict[str, str]) -> int:
            captured["command"] = command
            captured["env"] = env
            return 0

        monkeypatch.setattr(cli, "_spawn", fake_spawn)
        monkeypatch.setenv("PORTER_SKILL_FORCE_UV", "1")
        monkeypatch.setenv("PORTER_SKILL_SPEC", "porter-workflow==0.4.0")
        assert cli.main(["version"]) == 0
        cmd = captured["command"]
        assert "--no-project" in cmd
        assert "porter-workflow==0.4.0" in cmd
        assert cmd[-2:] == [str(SCRIPT_PATH.resolve()), "version"]
        assert captured["env"]["_PORTER_SKILL_BOOTSTRAPPED"] == "1"

    def test_version_capabilities_probe_plan_and_run_in_process(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cli = _load_cli()
        cfg = tmp_path / "cfg.json"
        cfg.write_text("{}\n", encoding="utf-8")
        monkeypatch.setenv("PORTER_CACHE_DIR", str(tmp_path / "cache"))
        monkeypatch.setenv("PORTER_CONFIG", str(cfg))

        assert cli.main(["version"]) == 0
        ver = json.loads(capsys.readouterr().out)
        assert ver["name"] == "porter-workflow"

        assert cli.main(["capabilities", "--stage", "translate"]) == 0
        caps = json.loads(capsys.readouterr().out)
        assert any(c["id"] == "translate.bing" for c in caps)

        srt = tmp_path / "sample.srt"
        srt.write_text("1\n00:00:00,000 --> 00:00:01,000\nHello\n", encoding="utf-8")

        assert cli.main(["probe", str(srt)]) == 0
        probed = json.loads(capsys.readouterr().out)
        assert probed["kind"] == "subtitle"

        plan_file = tmp_path / "plan.json"
        assert (
            cli.main(["plan", str(srt), "--task", "probe", "--out", str(plan_file)]) == 0
        )
        planned = json.loads(capsys.readouterr().out)
        assert planned["feasible"] is True
        assert plan_file.is_file()

        # Also prove UTF-16 BOM plans (PowerShell `>`) load cleanly.
        utf16_plan = tmp_path / "plan_utf16.json"
        utf16_plan.write_bytes(plan_file.read_text(encoding="utf-8").encode("utf-16"))
        result_file = tmp_path / "result.json"
        assert cli.main(["run", "--plan", str(utf16_plan), "--out", str(result_file)]) == 0
        res = json.loads(capsys.readouterr().out)
        assert res["state"] == "done"
        assert res["plan_id"] == planned["plan_id"]
        assert result_file.is_file()

        assert cli.main(["jobs", "list", "--all"]) == 0
        jobs = json.loads(capsys.readouterr().out)
        assert len(jobs) == 1 and jobs[0]["state"] == "done"

        assert cli.main(["config", "set", "video_quality=720p"]) == 0
        capsys.readouterr()
        assert cli.main(["config", "show"]) == 0
        shown = json.loads(capsys.readouterr().out)
        assert shown["video_quality"] == "720p"
