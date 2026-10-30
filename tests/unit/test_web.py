"""Unit tests for the Gradio-based web interface frontend (``porter_web``)."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from porter_web import app
from porter_web.i18n import TRANSLATIONS_EN, TRANSLATIONS_ZH, create_i18n, translate


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
        monkeypatch.setattr(app, "get_config_target", lambda: target)

        data = {"llm": {"api_key": "sk-123456", "model": "deepseek-chat"}}
        app.save_config_dict(data, target)

        assert target.is_file()
        read_back, path = app.read_config_dict()
        assert path == target
        assert read_back["llm"]["api_key"] == "sk-123456"
        assert read_back["llm"]["model"] == "deepseek-chat"


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
        monkeypatch.setattr(app, "get_config_target", lambda: target)
        data = {
            "llm": {"api_key": "{{SECRET_8r7j001r}}", "model": "deepseek-chat"},
            "cookies_browser": "chrome",
            "cookies_file": "/path/to/cookies.txt",
        }
        app.save_config_dict(data, target)

        fake_gr = MagicMock()
        handlers: list[tuple[object, list[object]]] = []

        def fake_click(fn: object, inputs: object = None, outputs: list[object] | None = None) -> MagicMock:
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

