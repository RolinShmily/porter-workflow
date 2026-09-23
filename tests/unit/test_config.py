"""Configuration resolution: layering, secrets, and error handling."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from porter.config import (
    ConfigError,
    default_config,
    find_project_config,
    load_config_file,
    mask_secret,
    resolve,
    save_key,
)


class TestDefaults:
    def test_built_in_defaults(self, project_dir) -> None:
        config = resolve()
        assert config.llm.model == "deepseek-chat"
        assert config.asr.engine is None
        assert config.output_dir == Path("./porter_output")
        assert "default" in config.source

    def test_default_config_matches_resolve_without_files(self, project_dir) -> None:
        assert default_config().llm.model == resolve().llm.model


class TestSecrets:
    @pytest.mark.parametrize(
        ("secret", "expected"),
        [
            (None, "<not set>"),
            ("", "<not set>"),
            ("short", "***"),
            ("sk-abcdefghijklmnop", "sk-...mnop"),
        ],
    )
    def test_mask_secret(self, secret: str | None, expected: str) -> None:
        assert mask_secret(secret) == expected

    def test_whisper_falls_back_to_llm_credentials(self, project_dir, monkeypatch) -> None:
        monkeypatch.setenv("OPENAI_API_KEY", "sk-llm-key-1234567")
        monkeypatch.setenv("OPENAI_BASE_URL", "https://example.invalid/v1")
        config = resolve()
        assert config.asr.whisper_api_key == "sk-llm-key-1234567"
        assert config.asr.whisper_api_base == "https://example.invalid/v1"

    def test_explicit_whisper_key_wins(self, project_dir, monkeypatch) -> None:
        monkeypatch.setenv("OPENAI_API_KEY", "sk-llm-key-1234567")
        monkeypatch.setenv("WHISPER_API_KEY", "sk-whisper-7654321")
        assert resolve().asr.whisper_api_key == "sk-whisper-7654321"

    def test_environment_beats_file(self, project_dir, write_config, monkeypatch) -> None:
        write_config({"llm": {"api_key": "from-file", "model": "file-model"}})
        monkeypatch.setenv("OPENAI_API_KEY", "from-env")
        config = resolve()
        assert config.llm.api_key == "from-env"
        assert config.llm.model == "file-model", "unrelated keys must still come from file"


class TestDiscovery:
    def test_project_config_in_cwd(self, project_dir, write_config) -> None:
        path = write_config({"llm": {"model": "project-model"}})
        config = resolve()
        assert config.llm.model == "project-model"
        assert config.source == str(path)

    def test_project_config_found_in_parent(self, project_dir, write_config) -> None:
        write_config({"llm": {"model": "parent-model"}})
        nested = project_dir / "a" / "b"
        nested.mkdir(parents=True)
        import os

        os.chdir(nested)
        assert resolve().llm.model == "parent-model"

    def test_porter_json_preferred_over_legacy_names(self, project_dir, write_config) -> None:
        write_config({"llm": {"model": "legacy"}}, name="config.json")
        write_config({"llm": {"model": "preferred"}}, name="porter.json")
        assert resolve().llm.model == "preferred"

    def test_env_config_path_wins_over_project_file(
        self, project_dir, write_config, monkeypatch
    ) -> None:
        write_config({"llm": {"model": "from-project"}})
        elsewhere = write_config({"llm": {"model": "from-env-path"}}, name="elsewhere.json")
        monkeypatch.setenv("PORTER_CONFIG", str(elsewhere))
        assert resolve().llm.model == "from-env-path"

    def test_explicit_path_wins_over_everything(
        self, project_dir, write_config, monkeypatch
    ) -> None:
        write_config({"llm": {"model": "from-project"}})
        monkeypatch.setenv("PORTER_CONFIG", str(write_config({"llm": {"model": "x"}})))
        explicit = write_config({"llm": {"model": "explicit"}}, name="explicit.json")
        assert resolve(explicit).llm.model == "explicit"

    def test_missing_explicit_path_is_an_error(self, tmp_path) -> None:
        """An explicit --config that does not exist must not fall back silently."""
        with pytest.raises(ConfigError, match="not found"):
            resolve(tmp_path / "absent.json")

    def test_missing_env_config_path_is_an_error(self, project_dir, tmp_path, monkeypatch) -> None:
        monkeypatch.setenv("PORTER_CONFIG", str(tmp_path / "absent.json"))
        with pytest.raises(ConfigError, match="PORTER_CONFIG"):
            resolve()

    def test_find_project_config_returns_none_when_absent(self, tmp_path) -> None:
        assert find_project_config(tmp_path) is None


class TestParsing:
    def test_malformed_json_raises(self, project_dir, tmp_path) -> None:
        broken = tmp_path / "porter.json"
        broken.write_text("{ not json", encoding="utf-8")
        with pytest.raises(ConfigError, match="invalid JSON"):
            resolve()

    def test_non_mapping_top_level_raises(self, tmp_path) -> None:
        path = tmp_path / "porter.json"
        path.write_text("[1, 2, 3]", encoding="utf-8")
        with pytest.raises(ConfigError, match="mapping"):
            load_config_file(path)

    def test_toml_is_supported(self, project_dir, tmp_path) -> None:
        path = tmp_path / "porter.toml"
        path.write_text('[llm]\nmodel = "toml-model"\n', encoding="utf-8")
        assert resolve().llm.model == "toml-model"

    def test_unknown_keys_are_ignored(self, project_dir, write_config) -> None:
        write_config({"llm": {"model": "m", "not_a_real_key": 1}})
        assert resolve().llm.model == "m"

    def test_output_dir_from_env(self, project_dir, tmp_path, monkeypatch) -> None:
        elsewhere = tmp_path / "elsewhere"
        monkeypatch.setenv("PORTER_OUTPUT_DIR", str(elsewhere))
        assert resolve().output_dir == elsewhere


class TestSaveKey:
    def test_creates_nested_structure(self, tmp_path) -> None:
        target = tmp_path / "nested" / "config.json"
        save_key("llm.api_key", "sk-xyz", target=target)
        assert json.loads(target.read_text(encoding="utf-8")) == {"llm": {"api_key": "sk-xyz"}}

    def test_merges_with_existing_content(self, tmp_path) -> None:
        target = tmp_path / "config.json"
        target.write_text(json.dumps({"llm": {"model": "keep-me"}}), encoding="utf-8")
        save_key("asr.engine", "bcut", target=target)
        data = json.loads(target.read_text(encoding="utf-8"))
        assert data["llm"]["model"] == "keep-me"
        assert data["asr"]["engine"] == "bcut"

    def test_replaces_a_scalar_in_the_middle_of_a_path(self, tmp_path) -> None:
        target = tmp_path / "config.json"
        target.write_text(json.dumps({"llm": "oops"}), encoding="utf-8")
        save_key("llm.model", "fixed", target=target)
        assert json.loads(target.read_text(encoding="utf-8"))["llm"]["model"] == "fixed"

    def test_empty_key_is_rejected(self, tmp_path) -> None:
        with pytest.raises(ConfigError, match="empty"):
            save_key("", "x", target=tmp_path / "c.json")
