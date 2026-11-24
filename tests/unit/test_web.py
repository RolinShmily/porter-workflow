"""Unit tests for the Gradio-based web interface frontend (``porter.webui``)."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from porter.webui import app, config_io
from porter.webui.i18n import TRANSLATIONS_EN, TRANSLATIONS_ZH, create_i18n, translate


class TestGradioDependencyCheck:
    def test_missing_gradio_exits_with_clear_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """When gradio is not installed, check_gradio_installed exits with code 1."""
        monkeypatch.setattr("builtins.__import__", self._raise_import_error)
        with pytest.raises(SystemExit) as exc_info:
            app.check_gradio_installed()
        assert exc_info.value.code == 1

    @staticmethod
    def _raise_import_error(name: str, *args: object, **kwargs: object) -> object:
        if name == "gradio":
            raise ImportError("No module named 'gradio'")
        return __import__(name, *args, **kwargs)

    def test_installed_gradio_returns_module(self, monkeypatch: pytest.MonkeyPatch) -> None:
        fake_gr = MagicMock()
        monkeypatch.setattr(
            app,
            "check_gradio_installed",
            lambda: fake_gr,
        )
        assert app.check_gradio_installed() is fake_gr


class TestAppAssemblyWithMockGradio:
    def test_create_app_assembles_all_tabs(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """create_app builds a Blocks context and configures three tabs."""
        fake_gr = MagicMock()
        fake_blocks_ctx = MagicMock()
        fake_gr.Blocks.return_value.__enter__.return_value = fake_blocks_ctx

        monkeypatch.setattr(app, "check_gradio_installed", lambda: fake_gr)

        instance = app.create_app()
        assert instance is not None
        assert fake_gr.Blocks.called
        # Check tabs were created
        assert fake_gr.Tab.call_count >= 4


class TestConfigManagement:
    def test_save_and_read_config(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        target = tmp_path / "custom_config.json"
        monkeypatch.setattr(config_io, "get_config_target", lambda: target)

        data = {"llm": {"api_key": "sk-123456", "model": "deepseek-chat"}}
        app.save_config_dict(data, target)

        assert target.is_file()
        read_back, path = app.read_config_dict()
        assert path == target
        assert read_back["llm"]["api_key"] == "sk-123456"
        assert read_back["llm"]["model"] == "deepseek-chat"

    def test_a_toml_project_config_is_never_overwritten(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The settings tab writes JSON; writing it into porter.toml would break it."""
        project = tmp_path / "porter.toml"
        original = "[llm]\nmodel = 'x'\n"
        project.write_text(original, encoding="utf-8")
        user_config = tmp_path / "user-config.json"
        monkeypatch.setattr(config_io, "find_project_config", lambda: project)
        monkeypatch.setattr(config_io, "config_file", lambda: user_config)

        assert config_io.get_config_target() == user_config
        assert project.read_text(encoding="utf-8") == original


class TestCollectTaskMaterials:
    def test_none_or_missing_directory(self) -> None:
        raws, cookeds, r_zip, c_zip, md = app.collect_task_materials(None)
        assert raws == []
        assert cookeds == []
        assert r_zip is None
        assert c_zip is None
        assert "暂无" in md

        raws, cookeds, r_zip, c_zip, md = app.collect_task_materials("/non/existent/path/abc")
        assert raws == []
        assert cookeds == []
        assert r_zip is None
        assert c_zip is None
        assert "不存在" in md

    def test_collects_both_raw_and_cooked_files(self, tmp_path: Path) -> None:
        task_dir = tmp_path / "test_job"
        raw_dir = task_dir / "raw"
        cooked_dir = task_dir / "cooked"
        raw_dir.mkdir(parents=True)
        cooked_dir.mkdir(parents=True)

        (raw_dir / "audio.wav").write_bytes(b"audio content")
        (raw_dir / "video.mp4").write_bytes(b"video content")
        (cooked_dir / "video_bilingual.mp4").write_bytes(b"bilingual video")

        raws, cookeds, r_zip, c_zip, md = app.collect_task_materials(task_dir)
        assert len(raws) == 2
        assert len(cookeds) == 1
        assert r_zip is not None and Path(r_zip).is_file()
        assert c_zip is not None and Path(c_zip).is_file()
        assert "audio.wav" in md
        assert "video_bilingual.mp4" in md


class TestI18nSupport:
    def test_i18n_dictionary_parity(self) -> None:
        """Every translation key in ZH should exist in EN and vice versa."""
        zh_keys = set(TRANSLATIONS_ZH.keys())
        en_keys = set(TRANSLATIONS_EN.keys())
        assert zh_keys == en_keys, f"Key mismatch: {zh_keys ^ en_keys}"

    def test_create_i18n_restricts_to_zh_and_en(self) -> None:
        fake_gr = MagicMock()
        create_i18n(fake_gr)
        fake_gr.I18n.assert_called_once_with(
            zh=TRANSLATIONS_ZH,
            en=TRANSLATIONS_EN,
        )

    def test_translate_fallback(self) -> None:
        assert "工作台" in translate("header_title", "zh")
        assert "Workbench" in translate("header_title", "en")
        # Unknown key returns key itself
        assert translate("non_existent_key", "zh") == "non_existent_key"


class TestDoctorDashboard:
    def test_get_doctor_table_data(self) -> None:
        summary, rows = app.get_doctor_table_data()
        assert "System Diagnostics" in summary
        assert len(rows) > 0
        labels = [r[0] for r in rows]
        assert "ffmpeg & ffprobe" in labels


class TestActiveJobMonitor:
    def test_idle_when_no_active_jobs(self) -> None:
        fake_registry = MagicMock()
        fake_registry.read.return_value = []
        is_act, j_id, _ph, _st, _msg, pct = app.get_active_job_details(fake_registry)
        assert is_act is False
        assert j_id == ""
        assert pct == 0.0

    def test_displays_running_job(self) -> None:
        fake_registry = MagicMock()
        record = MagicMock()
        record.job_id = "run-test123"
        record.state = "running"
        record.phase = "transcribe"
        record.percent = 45.0
        record.source = "https://youtube.com/v/1"
        record.elapsed_seconds = 12.3
        record.message = "fetching subs"
        fake_registry.read.return_value = [record]

        is_act, j_id, ph, _st, _msg, pct = app.get_active_job_details(fake_registry)
        assert is_act is True
        assert j_id == "run-test123"
        assert ph == "Transcribe"
        assert pct == 45.0


class TestJobsTableFormatting:
    def test_format_source_labels_and_links(self) -> None:
        url = "https://www.youtube.com/watch?v=abc"
        assert app.format_source_label(url) == "YouTube (abc)"
        assert app.format_source_link(url) == "[YouTube (abc)](https://www.youtube.com/watch?v=abc)"
        assert "Instagram" in app.format_source_label("https://www.instagram.com/p/123/")
        assert "Local" in app.format_source_label("file:///tmp/video.mp4")

    def test_get_jobs_dataframe_rows_and_choices(self) -> None:
        fake_registry = MagicMock()
        record = MagicMock()
        record.job_id = "run-job1"
        record.state = "done"
        record.phase = "burn"
        record.percent = 100.0
        record.interaction_required = None
        record.source = "https://www.youtube.com/watch?v=abc"
        fake_registry.read.return_value = [record]

        rows = app.get_jobs_dataframe_rows(fake_registry)
        assert len(rows) == 1
        assert rows[0][0] == "run-job1"
        assert "[YouTube (abc)](https://www.youtube.com/watch?v=abc)" in rows[0][5]

        choices = app.get_job_id_choices(fake_registry)
        assert len(choices) == 1
        assert choices[0][1] == "run-job1"


class TestSettingsTabAssembly:
    def test_settings_tab_reload_all_parity(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Verify on_reload_all reloads all config fields including cookies_file, matching outputs."""
        target = tmp_path / "config.json"
        monkeypatch.setattr(config_io, "get_config_target", lambda: target)
        data = {
            "llm": {"api_key": "{{SECRET_8r7j001r}}", "model": "deepseek-chat"},
            "cookies_browser": "chrome",
            "cookies_file": "/path/to/cookies.txt",
        }
        app.save_config_dict(data, target)

        fake_gr = MagicMock()
        handlers: list[tuple[object, list[object]]] = []

        def fake_click(
            fn: object, inputs: object = None, outputs: list[object] | None = None
        ) -> MagicMock:
            handlers.append((fn, outputs or []))
            return MagicMock()

        btn_mock = MagicMock()
        btn_mock.click.side_effect = fake_click
        fake_gr.Button.return_value = btn_mock

        app.build_settings_tab(fake_gr)

        reload_entry = next(
            (h for h in handlers if getattr(h[0], "__name__", "") == "on_reload_all"),
            None,
        )
        assert reload_entry is not None, "on_reload_all handler not registered"
        fn, outputs = reload_entry
        res = fn()  # type: ignore[operator]
        assert len(res) == len(outputs) == 21
        assert res[-2] == "chrome"
        assert res[-1] == "/path/to/cookies.txt"

    def test_asr_engine_choices_are_capabilities_not_legacy_clis(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """The old VideoCaptioner route is gone; only real capabilities remain."""
        target = tmp_path / "config.json"
        monkeypatch.setattr(config_io, "get_config_target", lambda: target)

        fake_gr = MagicMock()
        app.build_settings_tab(fake_gr)

        choices = [
            choice
            for call in fake_gr.Dropdown.call_args_list
            for choice in (call.kwargs.get("choices") or [])
        ]
        values = {choice[1] if isinstance(choice, tuple) else choice for choice in choices}
        assert "videocaptioner" not in values
        assert {"whisper-local", "whisper-api"} <= values


class TestWorkbenchPlanHelpers:
    def test_any_planning_control_change_invalidates_the_plan_key(self) -> None:
        from porter.webui.tabs.workbench import _plan_key

        base = ("https://youtu.be/x", "release", "dual", "", "", "zh-Hans", "", False)
        variants = [
            ("https://youtu.be/y", "release", "dual", "", "", "zh-Hans", "", False),
            ("https://youtu.be/x", "subtitle", "dual", "", "", "zh-Hans", "", False),
            ("https://youtu.be/x", "release", "skip", "", "", "zh-Hans", "", False),
            ("https://youtu.be/x", "release", "dual", "whisper-local", "", "zh-Hans", "", False),
            ("https://youtu.be/x", "release", "dual", "", "bing", "zh-Hans", "", False),
            ("https://youtu.be/x", "release", "dual", "", "", "en", "", False),
            ("https://youtu.be/x", "release", "dual", "", "", "zh-Hans", "a.srt", False),
            ("https://youtu.be/x", "release", "dual", "", "", "zh-Hans", "", True),
        ]
        keys = {_plan_key(*base), *(_plan_key(*variant) for variant in variants)}
        assert len(keys) == len(variants) + 1

    def test_planning_takes_cookies_from_config_not_from_the_form(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        from porter.core.config import PorterConfig
        from porter.webui.tabs import workbench

        cookies = tmp_path / "cookies.txt"
        config = PorterConfig(
            output_dir=tmp_path,
            cookies_file=cookies,
            cookies_browser="chrome",
        )
        monkeypatch.setattr(workbench, "resolve", lambda explicit=None: config)

        captured: dict[str, object] = {}

        def fake_resolve_plan(source: str, overrides: object, ctx: object) -> object:
            captured["source"] = source
            captured["overrides"] = overrides
            captured["ctx"] = ctx
            return MagicMock()

        monkeypatch.setattr(workbench, "resolve_plan", fake_resolve_plan)

        workbench._resolve_plan(
            "https://youtu.be/x",
            task="release",
            burn="dual",
            transcript="whisper-local",
            translate="bing",
            target_lang="zh-Hans",
            subtitle_file="",
            allow_paid=True,
        )

        overrides = captured["overrides"]
        assert overrides.cookies_file == cookies  # type: ignore[attr-defined]
        assert overrides.cookies_browser == "chrome"  # type: ignore[attr-defined]
        assert overrides.transcript == "whisper-local"  # type: ignore[attr-defined]
        assert overrides.translate == "bing"  # type: ignore[attr-defined]
        assert overrides.allow_paid is True  # type: ignore[attr-defined]
        assert overrides.output_dir == tmp_path  # type: ignore[attr-defined]
        assert captured["source"] == "https://youtu.be/x"

    def test_plan_key_tracks_force_translate_and_force(self) -> None:
        from porter.webui.tabs.workbench import _plan_key

        base = _plan_key("https://youtu.be/x", "release", "dual", "", "", "zh-Hans", "", False)
        ft = _plan_key(
            "https://youtu.be/x",
            "release",
            "dual",
            "",
            "",
            "zh-Hans",
            "",
            False,
            force_translate=True,
        )
        fr = _plan_key(
            "https://youtu.be/x",
            "release",
            "dual",
            "",
            "",
            "zh-Hans",
            "",
            False,
            force=True,
        )
        assert len({base, ft, fr}) == 3

    def test_i18n_zh_and_en_keys_stay_in_parity(self) -> None:
        from porter.webui.i18n import TRANSLATIONS_EN, TRANSLATIONS_ZH

        assert set(TRANSLATIONS_ZH) == set(TRANSLATIONS_EN)

    def test_saving_blank_cookies_writes_null_not_empty_string(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        target = tmp_path / "config.json"
        monkeypatch.setattr(config_io, "get_config_target", lambda: target)

        fake_gr = MagicMock()
        app.build_settings_tab(fake_gr)

        save_fn = None
        for call in fake_gr.Button.return_value.click.call_args_list:
            fn = call.args[0]
            if getattr(fn, "__name__", "") == "on_save_form":
                save_fn = fn
                break
        assert save_fn is not None

        save_fn(
            "",
            "https://api.deepseek.com/v1",
            "deepseek-chat",
            "",
            "small",
            True,
            "",
            "https://api.openai.com/v1",
            "Microsoft YaHei",
            "Arial",
            52,
            34,
            3.5,
            1.5,
            90,
            35,
            "",
            "",
        )
        import json

        saved = json.loads(target.read_text(encoding="utf-8"))
        assert saved["cookies_browser"] is None
        assert saved["cookies_file"] is None
