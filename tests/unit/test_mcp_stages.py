"""Tests for the stage tools and the read-only config tool.

Two layers, deliberately separated:

*Contract* tests pin what the tools expose and what they refuse. They are the
cheap ones, and they are the ones that catch a tool growing a parameter it should
not have -- ``porter_config`` acquiring a write action, or a stage tool quietly
accepting a URL and blocking on a download.

*Behaviour* tests stub the engine (``Pipeline.default``, ``burn_hardsub``) so the
tool's own logic -- path handling, error mapping, result shape -- is exercised
without a network round-trip or an encode. The real thing is covered by the two
``slow`` integration tests at the end, which is where a stubbed double could
disagree with reality.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("fastmcp", reason="requires the [mcp] extra")

from fastmcp import Client

from porter.errors import PorterError
from porter.models.subtitle import SubtitleItem, SubtitleSet
from porter_mcp.server import create_server

SAMPLE_SRT = """1
00:00:00,500 --> 00:00:02,500
We are no strangers to love

2
00:00:02,500 --> 00:00:04,500
You know the rules and so do I
"""


def _items(count: int = 2) -> list[SubtitleItem]:
    return [
        SubtitleItem(
            index=i + 1,
            start_ms=i * 2000,
            end_ms=i * 2000 + 1800,
            source_text=f"line {i + 1}",
            target_text=f"第 {i + 1} 行",
        )
        for i in range(count)
    ]


class _FakeTranslator:
    """Stands in for ``TranslationChain``, filling target text and writing files."""

    def __init__(self, *, drops: int = 0) -> None:
        self.drops = drops
        self.calls: list[tuple[int, str]] = []

    def translate(self, subtitles: SubtitleSet, target_lang: str, ctx: Any) -> SubtitleSet:
        self.calls.append((len(subtitles.items), target_lang))
        kept = subtitles.items[: len(subtitles.items) - self.drops] or subtitles.items[:1]
        for item in kept:
            item.target_text = f"[{target_lang}] {item.source_text}"
        subtitles.items = kept
        for path in (
            subtitles.subtitle_bilingual_srt,
            subtitles.subtitle_zh_srt,
            subtitles.transcript_txt_path,
        ):
            Path(path).write_text("stub", encoding="utf-8")
        return subtitles


class _FakePipeline:
    def __init__(self, translator: Any = None, result: Any = None) -> None:
        self.translator = translator or _FakeTranslator()
        self._result = result

    def run(self, request: Any, ctx: Any) -> Any:
        return self._result


@pytest.fixture
def stub_pipeline(monkeypatch: pytest.MonkeyPatch):
    """Replace ``Pipeline.default`` with a builder returning a scripted pipeline."""

    def _install(translator: Any = None, result: Any = None) -> _FakePipeline:
        pipeline = _FakePipeline(translator, result)

        def _default(ctx: Any) -> _FakePipeline:
            return pipeline

        monkeypatch.setattr("porter.pipeline.Pipeline.default", staticmethod(_default))
        return pipeline

    return _install


# ---------------------------------------------------------------------------
# porter_config
# ---------------------------------------------------------------------------


class TestConfigToolContract:
    async def test_the_tool_is_exposed(self) -> None:
        async with Client(create_server()) as client:
            names = {tool.name for tool in await client.list_tools()}
        assert "porter_config" in names

    async def test_the_schema_offers_no_write_action(self) -> None:
        """Only ``action`` and ``section``: there is no key to set anything with.

        A ``value`` or ``api_key`` parameter appearing here would be the whole
        §8.5 rule broken, and it would be broken silently -- the parameter would
        simply start accepting secrets into the transcript.
        """
        async with Client(create_server()) as client:
            tool = next(t for t in await client.list_tools() if t.name == "porter_config")
        assert set(tool.input_schema["properties"]) == {"action", "section"}


class TestConfigTool:
    async def test_list_names_the_sections_and_the_source(self) -> None:
        async with Client(create_server()) as client:
            data = (await client.call_tool("porter_config", {"action": "list"})).data

        assert data["ok"] is True
        assert data["sections"] == ["llm", "asr", "ffmpeg", "style"]
        assert data["source"]
        assert data["secrets_masked"] is True

    async def test_get_returns_the_whole_config(self) -> None:
        async with Client(create_server()) as client:
            data = (await client.call_tool("porter_config", {"action": "get"})).data

        assert data["ok"] is True
        assert set(data["config"]) >= {"llm", "asr", "ffmpeg", "style", "output_dir"}

    async def test_get_can_narrow_to_one_section(self) -> None:
        async with Client(create_server()) as client:
            data = (
                await client.call_tool("porter_config", {"action": "get", "section": "llm"})
            ).data

        assert data["ok"] is True
        assert data["section"] == "llm"
        assert "model" in data["config"]
        # Narrowed, not nested: asking for a section must not return the rest.
        assert "output_dir" not in data["config"]

    async def test_a_configured_key_is_masked_not_returned(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The one assertion that matters: a real key must not survive the call.

        ``OPENAI_API_KEY`` is the name the engine actually reads (see
        ``config._first_env``). Setting a variable the engine ignores would make
        this test pass for the wrong reason -- an unset key masks to nothing, so
        the assertion would hold even with masking deleted.
        """
        # Assembled rather than written as one literal so the linter does not read
        # this test fixture as a committed credential.
        secret = "sk-" + "live-do-not-leak-" + "0123456789"
        monkeypatch.setenv("OPENAI_API_KEY", secret)
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))

        async with Client(create_server()) as client:
            data = (
                await client.call_tool("porter_config", {"action": "get", "section": "llm"})
            ).data

        assert data["config"]["api_key"], "the key never reached the config, so this proves nothing"
        rendered = str(data)
        assert secret not in rendered, "the raw key reached the tool result"
        assert data["config"]["api_key"] != secret

    async def test_an_unknown_action_is_refused_by_name(self) -> None:
        async with Client(create_server()) as client:
            data = (await client.call_tool("porter_config", {"action": "set"})).data

        assert data["ok"] is False
        assert "set" in data["error"]

    async def test_an_unknown_section_is_refused_rather_than_empty(self) -> None:
        """An empty dict would read as "this section has no settings"."""
        async with Client(create_server()) as client:
            data = (
                await client.call_tool("porter_config", {"action": "get", "section": "nope"})
            ).data

        assert data["ok"] is False
        assert "nope" in data["error"]

    async def test_a_broken_config_is_reported_as_data(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _boom(_explicit: Any) -> Any:
            raise PorterError("config file is not valid JSON")

        monkeypatch.setattr("porter_mcp.tools.config.resolve", _boom)

        async with Client(create_server()) as client:
            data = (await client.call_tool("porter_config", {"action": "list"})).data

        assert data["ok"] is False
        assert "not valid JSON" in data["error"]


# ---------------------------------------------------------------------------
# porter_translate
# ---------------------------------------------------------------------------


class TestTranslateToolContract:
    async def test_the_tool_is_exposed(self) -> None:
        async with Client(create_server()) as client:
            names = {tool.name for tool in await client.list_tools()}
        assert "porter_translate" in names

    async def test_the_schema_takes_a_path_not_a_url(self) -> None:
        async with Client(create_server()) as client:
            tool = next(t for t in await client.list_tools() if t.name == "porter_translate")
        assert set(tool.input_schema["properties"]) == {
            "srt_path",
            "target_lang",
            "backend",
            "output_dir",
            "max_cues",
        }


class TestTranslateTool:
    async def test_a_missing_file_is_refused(self) -> None:
        async with Client(create_server()) as client:
            data = (
                await client.call_tool("porter_translate", {"srt_path": "/nope/missing.srt"})
            ).data

        assert data["ok"] is False
        assert "missing.srt" in data["error"]

    async def test_an_empty_file_is_refused_with_a_hint(self, tmp_path: Path) -> None:
        """An empty SRT usually means a failed download, not a silent video."""
        empty = tmp_path / "empty.srt"
        empty.write_text("", encoding="utf-8")

        async with Client(create_server()) as client:
            data = (await client.call_tool("porter_translate", {"srt_path": str(empty)})).data

        assert data["ok"] is False
        assert "no subtitle cues" in data["error"]

    async def test_translation_writes_beside_the_input_by_default(
        self, tmp_path: Path, stub_pipeline
    ) -> None:
        source = tmp_path / "input.srt"
        source.write_text(SAMPLE_SRT, encoding="utf-8")
        pipeline = stub_pipeline()

        async with Client(create_server()) as client:
            data = (await client.call_tool("porter_translate", {"srt_path": str(source)})).data

        assert data["ok"] is True
        assert data["cue_count"] == 2
        assert Path(data["subtitle_zh_srt"]).parent == tmp_path / "translated"
        assert pipeline.translator.calls == [(2, "zh-Hans")]

    async def test_an_explicit_output_dir_wins(self, tmp_path: Path, stub_pipeline) -> None:
        source = tmp_path / "input.srt"
        source.write_text(SAMPLE_SRT, encoding="utf-8")
        target = tmp_path / "elsewhere"
        stub_pipeline()

        async with Client(create_server()) as client:
            data = (
                await client.call_tool(
                    "porter_translate",
                    {"srt_path": str(source), "output_dir": str(target)},
                )
            ).data

        assert data["ok"] is True
        assert Path(data["subtitle_bilingual_srt"]).parent == target

    async def test_the_target_language_reaches_the_chain(
        self, tmp_path: Path, stub_pipeline
    ) -> None:
        source = tmp_path / "input.srt"
        source.write_text(SAMPLE_SRT, encoding="utf-8")
        pipeline = stub_pipeline()

        async with Client(create_server()) as client:
            data = (
                await client.call_tool(
                    "porter_translate", {"srt_path": str(source), "target_lang": "en"}
                )
            ).data

        assert data["ok"] is True
        assert data["target_lang"] == "en"
        assert pipeline.translator.calls == [(2, "en")]

    async def test_cues_carry_both_languages(self, tmp_path: Path, stub_pipeline) -> None:
        source = tmp_path / "input.srt"
        source.write_text(SAMPLE_SRT, encoding="utf-8")
        stub_pipeline()

        async with Client(create_server()) as client:
            data = (await client.call_tool("porter_translate", {"srt_path": str(source)})).data

        first = data["cues"][0]
        assert first["source"] == "We are no strangers to love"
        assert first["target"].startswith("[zh-Hans]")
        assert first["start_ms"] == 500

    async def test_max_cues_truncates_and_says_so(self, tmp_path: Path, stub_pipeline) -> None:
        """A silently shortened list would be read as "the transcript ends here"."""
        source = tmp_path / "input.srt"
        source.write_text(SAMPLE_SRT, encoding="utf-8")
        stub_pipeline()

        async with Client(create_server()) as client:
            data = (
                await client.call_tool(
                    "porter_translate", {"srt_path": str(source), "max_cues": 1}
                )
            ).data

        assert len(data["cues"]) == 1
        assert data["cues_truncated"] is True
        assert data["cue_count"] == 2

    async def test_merging_is_reported_rather_than_hidden(
        self, tmp_path: Path, stub_pipeline
    ) -> None:
        """The chain rebuilds sentences, so cue boundaries can move.

        An SRT whose cues were already sentence-length is exactly the input where
        that is surprising, so the count change and a note must come back with it.
        """
        source = tmp_path / "input.srt"
        source.write_text(SAMPLE_SRT, encoding="utf-8")
        stub_pipeline(translator=_FakeTranslator(drops=1))

        async with Client(create_server()) as client:
            data = (await client.call_tool("porter_translate", {"srt_path": str(source)})).data

        assert data["ok"] is True
        assert data["input_cue_count"] == 2
        assert data["cue_count"] == 1
        assert data["cues_merged"] is True
        assert "2 input cues became 1" in data["note"]

    async def test_no_note_when_nothing_merged(self, tmp_path: Path, stub_pipeline) -> None:
        source = tmp_path / "input.srt"
        source.write_text(SAMPLE_SRT, encoding="utf-8")
        stub_pipeline()

        async with Client(create_server()) as client:
            data = (await client.call_tool("porter_translate", {"srt_path": str(source)})).data

        assert data["cues_merged"] is False
        assert data["note"] is None

    async def test_a_backend_failure_is_reported_as_data(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        source = tmp_path / "input.srt"
        source.write_text(SAMPLE_SRT, encoding="utf-8")

        class _Failing:
            def translate(self, subtitles: Any, target_lang: str, ctx: Any) -> Any:
                raise PorterError("every translation backend failed", attempted=3)

        monkeypatch.setattr(
            "porter.pipeline.Pipeline.default", staticmethod(lambda ctx: _FakePipeline(_Failing()))
        )

        async with Client(create_server()) as client:
            data = (await client.call_tool("porter_translate", {"srt_path": str(source)})).data

        assert data["ok"] is False
        assert "every translation backend failed" in data["error"]


# ---------------------------------------------------------------------------
# porter_burn
# ---------------------------------------------------------------------------


class TestBurnToolContract:
    async def test_the_tool_is_exposed(self) -> None:
        async with Client(create_server()) as client:
            names = {tool.name for tool in await client.list_tools()}
        assert "porter_burn" in names

    async def test_the_schema_has_no_style_parameter(self) -> None:
        """An ``.ass`` carries its own styling, so a ``style`` here would be inert.

        §8.1 sketches ``style?``. Burning an authored ASS cannot apply one, and a
        parameter that silently does nothing is the ``as_source`` mistake again.
        """
        async with Client(create_server()) as client:
            tool = next(t for t in await client.list_tools() if t.name == "porter_burn")
        assert set(tool.input_schema["properties"]) == {"video", "ass", "output"}


class TestBurnTool:
    async def test_a_missing_video_is_refused(self, tmp_path: Path) -> None:
        ass = tmp_path / "sub.ass"
        ass.write_text("[Script Info]", encoding="utf-8")

        async with Client(create_server()) as client:
            data = (
                await client.call_tool(
                    "porter_burn", {"video": str(tmp_path / "gone.mp4"), "ass": str(ass)}
                )
            ).data

        assert data["ok"] is False
        assert "no such video" in data["error"]

    async def test_a_missing_subtitle_is_refused(self, tmp_path: Path) -> None:
        video = tmp_path / "v.mp4"
        video.write_bytes(b"x")

        async with Client(create_server()) as client:
            data = (
                await client.call_tool(
                    "porter_burn", {"video": str(video), "ass": str(tmp_path / "gone.ass")}
                )
            ).data

        assert data["ok"] is False
        assert "no such subtitle" in data["error"]

    async def test_the_default_output_sits_beside_the_video(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        video = tmp_path / "movie.mp4"
        video.write_bytes(b"x")
        ass = tmp_path / "sub.ass"
        ass.write_text("[Script Info]", encoding="utf-8")

        seen: dict[str, Any] = {}

        def _fake_burn(runner, video_input, subtitle, video_output, **kwargs) -> Path:
            seen.update(
                video=Path(video_input), subtitle=Path(subtitle), output=Path(video_output)
            )
            Path(video_output).write_bytes(b"burned")
            return Path(video_output)

        monkeypatch.setattr("porter.media.burn.burn_hardsub", _fake_burn)
        monkeypatch.setattr(
            "porter.media.encode.EncoderSelector.select", lambda self: type("P", (), {"name": "cpu"})()
        )

        async with Client(create_server()) as client:
            data = (
                await client.call_tool("porter_burn", {"video": str(video), "ass": str(ass)})
            ).data

        assert data["ok"] is True
        assert seen["output"] == tmp_path / "movie_hardsub.mp4"
        assert data["video"] == str(tmp_path / "movie_hardsub.mp4")
        assert data["encoder"] == "cpu"

    async def test_an_explicit_output_wins(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        video = tmp_path / "movie.mp4"
        video.write_bytes(b"x")
        ass = tmp_path / "sub.ass"
        ass.write_text("[Script Info]", encoding="utf-8")
        target = tmp_path / "out" / "final.mp4"

        monkeypatch.setattr(
            "porter.media.burn.burn_hardsub",
            lambda *a, **k: Path(a[3]).write_bytes(b"b") or Path(a[3]),
        )
        monkeypatch.setattr(
            "porter.media.encode.EncoderSelector.select", lambda self: type("P", (), {"name": "cpu"})()
        )

        async with Client(create_server()) as client:
            data = (
                await client.call_tool(
                    "porter_burn", {"video": str(video), "ass": str(ass), "output": str(target)}
                )
            ).data

        assert data["ok"] is True
        assert data["video"] == str(target)

    async def test_a_render_failure_is_reported_as_data(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from porter.errors import RenderError

        video = tmp_path / "movie.mp4"
        video.write_bytes(b"x")
        ass = tmp_path / "sub.ass"
        ass.write_text("[Script Info]", encoding="utf-8")

        def _fail(*a: Any, **k: Any) -> None:
            raise RenderError("ffmpeg exited 1: No such filter: ass")

        monkeypatch.setattr("porter.media.burn.burn_hardsub", _fail)
        monkeypatch.setattr(
            "porter.media.encode.EncoderSelector.select", lambda self: type("P", (), {"name": "cpu"})()
        )

        async with Client(create_server()) as client:
            data = (
                await client.call_tool("porter_burn", {"video": str(video), "ass": str(ass)})
            ).data

        assert data["ok"] is False
        assert "No such filter" in data["error"]


# ---------------------------------------------------------------------------
# porter_transcribe
# ---------------------------------------------------------------------------


class TestTranscribeToolContract:
    async def test_the_tool_is_exposed(self) -> None:
        async with Client(create_server()) as client:
            names = {tool.name for tool in await client.list_tools()}
        assert "porter_transcribe" in names

    async def test_a_url_is_refused_and_points_at_the_job_api(self) -> None:
        """The core honesty rule of this tool.

        Accepting a URL would mean downloading inside a blocking tool call, which
        is precisely the long task §8.1 says the job API exists for. A refusal an
        agent can act on beats a call that times out with no explanation.
        """
        async with Client(create_server()) as client:
            data = (
                await client.call_tool(
                    "porter_transcribe", {"source": "https://www.youtube.com/watch?v=abc"}
                )
            ).data

        assert data["ok"] is False
        assert "local file" in data["error"]
        assert "porter_job_start" in data["hint"]
        assert "only_phase" in data["hint"]

    async def test_a_file_url_is_treated_as_a_path(self, tmp_path: Path) -> None:
        """``file://`` is a path spelled long, not a download."""
        async with Client(create_server()) as client:
            data = (
                await client.call_tool(
                    "porter_transcribe", {"source": f"file://{tmp_path}/gone.mp4"}
                )
            ).data

        assert data["ok"] is False
        assert "no such file" in data["error"]

    async def test_a_missing_file_is_refused(self) -> None:
        async with Client(create_server()) as client:
            data = (
                await client.call_tool("porter_transcribe", {"source": "/nope/gone.mp4"})
            ).data

        assert data["ok"] is False
        assert "no such file" in data["error"]


class TestTranscribeTool:
    async def test_recognition_results_come_back_as_cues(
        self, tmp_path: Path, stub_pipeline
    ) -> None:
        from porter.models.request import JobResult, JobState

        media = tmp_path / "clip.mp4"
        media.write_bytes(b"x")

        subtitles = SubtitleSet(
            subtitle_bilingual_srt=tmp_path / "subtitle.srt",
            subtitle_bilingual_ass=tmp_path / "subtitle.ass",
            subtitle_zh_srt=tmp_path / "subtitle_zh.srt",
            subtitle_zh_ass=tmp_path / "subtitle_zh.ass",
            items=_items(3),
            transcript_json_path=tmp_path / "t.json",
            transcript_txt_path=tmp_path / "t.txt",
        )
        result = JobResult(
            job_id="j1", state=JobState.DONE, task_dir=tmp_path, subtitles=subtitles
        )
        stub_pipeline(result=result)

        async with Client(create_server()) as client:
            data = (await client.call_tool("porter_transcribe", {"source": str(media)})).data

        assert data["ok"] is True
        assert data["cue_count"] == 3
        assert data["cues"][1]["target"] == "第 2 行"
        assert data["task_dir"] == str(tmp_path)

    async def test_it_stops_after_recognition(self, tmp_path: Path, monkeypatch) -> None:
        """Otherwise the tool would translate and encode work nobody asked for."""
        from porter.events import Phase
        from porter.models.request import JobResult, JobState

        media = tmp_path / "clip.mp4"
        media.write_bytes(b"x")
        captured: dict[str, Any] = {}

        class _P:
            translator = _FakeTranslator()

            def run(self, request: Any, ctx: Any) -> Any:
                captured["only_phase"] = request.options.only_phase
                captured["burn"] = request.options.burn
                return JobResult(job_id="j", state=JobState.FAILED)

        monkeypatch.setattr("porter.pipeline.Pipeline.default", staticmethod(lambda ctx: _P()))

        async with Client(create_server()) as client:
            await client.call_tool("porter_transcribe", {"source": str(media)})

        assert captured["only_phase"] is Phase.TRANSCRIBE
        assert captured["burn"].value == "skip"

    async def test_a_failed_run_reports_the_engine_error(
        self, tmp_path: Path, stub_pipeline
    ) -> None:
        from porter.events import ErrorInfo, Phase
        from porter.models.request import JobResult, JobState

        media = tmp_path / "clip.mp4"
        media.write_bytes(b"x")
        result = JobResult(
            job_id="j1",
            state=JobState.FAILED,
            error=ErrorInfo(
                code="asr_failed",
                message="every speech-to-text backend failed",
                details={"phase": Phase.TRANSCRIBE.value},
            ),
        )
        stub_pipeline(result=result)

        async with Client(create_server()) as client:
            data = (await client.call_tool("porter_transcribe", {"source": str(media)})).data

        assert data["ok"] is False
        assert "every speech-to-text backend failed" in data["error"]
        assert data["state"] == "failed"

    async def test_max_cues_truncates_and_says_so(
        self, tmp_path: Path, stub_pipeline
    ) -> None:
        from porter.models.request import JobResult, JobState

        media = tmp_path / "clip.mp4"
        media.write_bytes(b"x")
        subtitles = SubtitleSet(
            subtitle_bilingual_srt=tmp_path / "s.srt",
            subtitle_bilingual_ass=tmp_path / "s.ass",
            subtitle_zh_srt=tmp_path / "z.srt",
            subtitle_zh_ass=tmp_path / "z.ass",
            items=_items(5),
            transcript_json_path=tmp_path / "t.json",
            transcript_txt_path=tmp_path / "t.txt",
        )
        stub_pipeline(
            result=JobResult(
                job_id="j", state=JobState.DONE, task_dir=tmp_path, subtitles=subtitles
            )
        )

        async with Client(create_server()) as client:
            data = (
                await client.call_tool(
                    "porter_transcribe", {"source": str(media), "max_cues": 2}
                )
            ).data

        assert len(data["cues"]) == 2
        assert data["cues_truncated"] is True
        assert data["cue_count"] == 5


# ---------------------------------------------------------------------------
# Integration: the real engine, real network, real ffmpeg
# ---------------------------------------------------------------------------


@pytest.mark.slow
class TestStageToolsIntegration:
    """These are the tests that keep the stubs honest.

    A stubbed ``Pipeline.default`` cannot disagree with the real chain about
    whether ``translated.items`` is the merged list, and a stubbed ``burn_hardsub``
    cannot disagree about whether the output path is the one ffmpeg wrote.
    """

    async def test_a_real_translation_writes_a_chinese_track(self, tmp_path: Path) -> None:
        source = tmp_path / "input.srt"
        source.write_text(SAMPLE_SRT, encoding="utf-8")

        async with Client(create_server()) as client:
            data = (
                await client.call_tool(
                    "porter_translate",
                    {"srt_path": str(source), "output_dir": str(tmp_path / "out")},
                )
            ).data

        if not data["ok"]:
            pytest.skip(f"no translation backend reachable: {data['error']}")

        assert data["cue_count"] >= 1
        # The engine's own writer produced the file, not the stub.
        zh = Path(data["subtitle_zh_srt"]).read_text(encoding="utf-8")
        assert "-->" in zh
        assert any("\u4e00" <= ch <= "\u9fff" for ch in zh), "no Chinese in the output"
        assert data["cues"][0]["target"]

    async def test_a_real_burn_puts_the_subtitle_on_the_pixels(self, tmp_path: Path) -> None:
        """Black video in, and the bottom band must stop being black.

        The point of checking pixels rather than the file's existence: v0.1
        published truncated encodes as finished releases, so "a file appeared" is
        not evidence that a burn happened.
        """
        import subprocess

        video = tmp_path / "black.mp4"
        make = subprocess.run(
            [
                "ffmpeg", "-v", "error", "-y",
                "-f", "lavfi", "-i", "color=black:size=320x240:rate=10:duration=4",
                "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
                str(video),
            ],
            capture_output=True,
        )
        if make.returncode != 0:
            pytest.skip("ffmpeg cannot encode a test clip here")

        ass = tmp_path / "sub.ass"
        ass.write_text(
            "[Script Info]\nScriptType: v4.00+\nPlayResX: 320\nPlayResY: 240\n"
            "[V4+ Styles]\n"
            "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, "
            "BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, "
            "BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n"
            "Style: Default,Arial,28,&H00FFFFFF,&H000000FF,&H00000000,&H00000000,0,0,0,0,"
            "100,100,0,0,1,2,1,2,10,10,10,1\n"
            "[Events]\n"
            "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
            "Dialogue: 0,0:00:00.50,0:00:03.50,Default,,0,0,0,,SUBTITLE BURNED HERE\n",
            encoding="utf-8",
        )

        async with Client(create_server()) as client:
            data = (
                await client.call_tool("porter_burn", {"video": str(video), "ass": str(ass)})
            ).data

        if not data["ok"]:
            pytest.skip(f"ffmpeg cannot burn here: {data['error']}")

        assert Path(data["video"]).exists()

        def _bottom_band(path: str) -> bytes:
            proc = subprocess.run(
                [
                    "ffmpeg", "-v", "error", "-ss", "2.0", "-i", path, "-frames:v", "1",
                    "-vf", "crop=iw:ih/3:0:ih*2/3,format=gray", "-f", "rawvideo", "-",
                ],
                capture_output=True,
            )
            return proc.stdout

        before = _bottom_band(str(video))
        after = _bottom_band(data["video"])
        assert before and after
        assert max(before) == 0, "the control clip was not black"
        assert max(after) > 60, "no subtitle pixels were burned into the video"
