"""Doctor probes — P3.2.

Each probe is tested with its collaborators injected, so nothing here spawns
ffmpeg, touches the network, or depends on what happens to be installed.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from porter.config import PorterConfig
from porter.doctor import GUIDES, guide_for
from porter.doctor.probes import (
    CapabilityReport,
    Finding,
    ProbeContext,
    Severity,
    probe_all,
    probe_asr_route,
    probe_encoder,
    probe_ffmpeg,
    probe_font,
    probe_js_runtime,
    probe_libass,
    probe_output_dir,
    probe_python,
    probe_translation_route,
    probe_yt_dlp,
)
from porter.errors import MediaError
from porter.media.encode import HARDWARE_PROFILES, EncoderSelector
from porter.media.ffmpeg import FFmpegTools


class FakeRunner:
    """Answers the questions the probes ask, including the trial encode."""

    def __init__(
        self,
        *,
        has_ass: bool = True,
        version: str = "ffmpeg version n9.0.1",
        encoders: dict[str, tuple[int, str]] | None = None,
    ) -> None:
        self.has_ass = has_ass
        self._version = version
        self.encoders = encoders if encoders is not None else {"h264_nvenc": (0, "")}

    def has_filter(self, name: str) -> bool:
        assert name == "ass"
        return self.has_ass

    def version(self) -> str:
        return self._version

    def run(self, args, *, what, check=True, timeout=None):
        encoder = args[args.index("-c:v") + 1]
        code, stderr = self.encoders.get(encoder, (1, "unknown encoder"))
        return SimpleNamespace(returncode=code, stdout="", stderr=stderr)


class FailingRunner(FakeRunner):
    def version(self) -> str:
        raise MediaError("boom")

    def has_filter(self, name: str) -> bool:
        raise MediaError("boom")

    def run(self, *args, **kwargs):
        raise MediaError("boom")


def _which_for(*present: str, path: str = "/usr/bin/x"):
    def lookup(name: str) -> str | None:
        return f"{path}/{name}" if name in present else None

    return lookup


@pytest.fixture
def config(tmp_path) -> PorterConfig:
    return PorterConfig(output_dir=tmp_path / "out")


@pytest.fixture
def present_tools(tmp_path) -> FFmpegTools:
    """A pair of binaries that really exist on disk.

    ``probe_ffmpeg`` checks the real filesystem even when the runner is faked:
    ``FFmpegTools.missing()`` calls ``shutil.which`` and ``Path.is_file``. Left
    at its default the doctor resolves the *host's* tools, so on a machine
    without ffmpeg (a CI runner, say) the report short-circuits to a single
    ffmpeg blocker and every test that claims to cover a dependent probe
    silently stops covering it. Pointing the tools at these empty files keeps
    the report honest instead. Nothing executes them — the runner stays fake.
    """
    ffmpeg = tmp_path / "ffmpeg"
    ffprobe = tmp_path / "ffprobe"
    ffmpeg.write_text("")
    ffprobe.write_text("")
    return FFmpegTools(ffmpeg=str(ffmpeg), ffprobe=str(ffprobe))


# ----------------------------------------------------------------------
# Finding constructors
# ----------------------------------------------------------------------


class TestFindingConstructors:
    """`ok` and `severity` must not be settable independently.

    A finding reporting ``ok=False, severity=INFO`` would be treated as fine by
    any caller that only inspects severity, which is the whole report.
    """

    def test_passed_is_info_and_ok(self) -> None:
        finding = Finding.passed("k", "label", "good")
        assert finding.ok is True
        assert finding.severity is Severity.INFO
        assert finding.remediation_key is None

    def test_info_is_ok(self) -> None:
        assert Finding.info("k", "label").ok is True

    def test_degraded_is_not_ok_and_names_a_guide(self) -> None:
        finding = Finding.degraded("k", "label", "detail", "guide_key")
        assert finding.ok is False
        assert finding.severity is Severity.DEGRADED
        assert finding.remediation_key == "guide_key"

    def test_blocked_is_not_ok_and_names_a_guide(self) -> None:
        finding = Finding.blocked("k", "label", "detail", "guide_key")
        assert finding.ok is False
        assert finding.severity is Severity.BLOCKER

    def test_blocker_outranks_degraded_outranks_info(self) -> None:
        assert Severity.BLOCKER.rank > Severity.DEGRADED.rank > Severity.INFO.rank

    def test_severity_is_a_plain_string_for_json(self) -> None:
        import json

        assert json.dumps(Severity.BLOCKER) == '"blocker"'


class TestCapabilityReport:
    def test_degradation_alone_is_not_a_failure_to_run(self) -> None:
        """"No GPU" and "no ffmpeg" must not read the same."""
        report = CapabilityReport(
            findings=[Finding.degraded("x", "X", "d", "k"), Finding.passed("y", "Y")]
        )
        assert report.ok is True
        assert report.failures == [report.findings[0]]

    def test_a_blocker_makes_it_not_ok(self) -> None:
        report = CapabilityReport(findings=[Finding.blocked("x", "X", "d", "k")])
        assert report.ok is False
        assert [f.key for f in report.blockers] == ["x"]

    def test_to_dict_is_json_serialisable(self) -> None:
        import json

        report = CapabilityReport(findings=[Finding.degraded("x", "X", "d", "k")])
        payload = report.to_dict()

        assert json.loads(json.dumps(payload)) == payload
        assert payload["degradations"] == ["x"]
        assert payload["findings"][0]["severity"] == "degraded"

    def test_get_looks_up_by_key(self) -> None:
        report = CapabilityReport(findings=[Finding.passed("ffmpeg", "FFmpeg")])
        assert report.get("ffmpeg") is not None
        assert report.get("nope") is None


# ----------------------------------------------------------------------
# Individual probes
# ----------------------------------------------------------------------


class TestPythonProbe:
    def test_a_supported_version_passes(self) -> None:
        assert probe_python(version_info=(3, 10, 0)).ok is True
        assert probe_python(version_info=(3, 14, 0)).ok is True

    def test_an_old_version_blocks(self) -> None:
        finding = probe_python(version_info=(3, 9, 18))
        assert finding.ok is False
        assert finding.severity is Severity.BLOCKER
        assert "3.9.18" in finding.detail
        assert guide_for(finding.remediation_key) is not None


class TestFfmpegProbe:
    def test_both_binaries_present(self, present_tools) -> None:
        assert probe_ffmpeg(present_tools).ok is True

    def test_a_missing_binary_blocks(self) -> None:
        tools = FFmpegTools(ffmpeg="ffmpeg-not-here", ffprobe="ffprobe-not-here")
        finding = probe_ffmpeg(tools)
        assert finding.severity is Severity.BLOCKER
        assert "ffmpeg, ffprobe" in finding.detail, "the tool kinds are named"
        assert "ffmpeg-not-here" in finding.detail, "and the path actually searched"

    def test_it_reports_what_is_missing_not_just_that_something_is(self, present_tools) -> None:
        tools = FFmpegTools(ffmpeg=present_tools.ffmpeg, ffprobe="ffprobe-not-here")
        finding = probe_ffmpeg(tools)
        assert "ffprobe" in finding.detail
        assert "ffmpeg," not in finding.detail


class TestLibassProbe:
    """v0.1's version searched for the substring "ass", which matches "pass"."""

    def test_the_filter_present_passes(self) -> None:
        assert probe_libass(FakeRunner(has_ass=True)).ok is True

    def test_absence_degrades_rather_than_blocks(self) -> None:
        """Soft subtitles still work, so the job is not impossible."""
        finding = probe_libass(FakeRunner(has_ass=False))
        assert finding.severity is Severity.DEGRADED
        assert "ass" in finding.detail
        assert guide_for(finding.remediation_key) is not None

    def test_a_probe_failure_does_not_raise(self) -> None:
        """A probe that raises on a broken machine is useless when needed."""
        finding = probe_libass(FailingRunner())
        assert finding.ok is False
        assert finding.severity is Severity.DEGRADED


class TestFontProbe:
    def test_a_resolving_font_passes(self, config) -> None:
        finding = probe_font(config, matcher=lambda family: "Microsoft YaHei,微软雅黑")
        assert finding.ok is True
        assert "Microsoft YaHei" in finding.detail

    def test_a_substituted_font_degrades(self, config) -> None:
        """fc-match never fails; it silently substitutes, so compare the answer."""
        finding = probe_font(config, matcher=lambda family: "Verdana")
        assert finding.ok is False
        assert finding.severity is Severity.DEGRADED
        assert "Verdana" in finding.detail
        assert guide_for(finding.remediation_key) is not None

    def test_the_configured_family_is_what_is_checked(self, tmp_path) -> None:
        cfg = PorterConfig(style={"zh_font": "Noto Sans CJK SC"})
        seen: list[str] = []

        def matcher(family: str) -> str:
            seen.append(family)
            return family

        probe_font(cfg, matcher=matcher)
        assert seen == ["Noto Sans CJK SC"]

    def test_no_fontconfig_is_informational_not_a_failure(self, config) -> None:
        """`fc-match` is Linux-specific; its absence must not fail other platforms."""
        finding = probe_font(config, matcher=lambda family: None)
        assert finding.ok is True
        assert "cannot verify" in finding.detail

    def test_matching_is_case_insensitive(self, config) -> None:
        assert probe_font(config, matcher=lambda f: "MICROSOFT YAHEI").ok is True

    def test_a_multi_family_match_counts_as_present(self, config) -> None:
        """fontconfig returns localised names comma-joined."""
        assert probe_font(config, matcher=lambda f: "Other, Microsoft YaHei").ok is True


class TestJsRuntimeProbe:
    def test_deno_is_reported_as_recommended(self) -> None:
        finding = probe_js_runtime(which=_which_for("deno"))
        assert finding.ok is True
        assert "recommended" in finding.detail

    def test_node_is_accepted(self) -> None:
        assert probe_js_runtime(which=_which_for("node")).ok is True

    def test_preference_order_puts_deno_first(self) -> None:
        finding = probe_js_runtime(which=_which_for("node", "deno", "bun"))
        assert "deno" in finding.detail

    def test_none_present_degrades(self) -> None:
        """It degrades silently in yt-dlp, which is why it is worth reporting."""
        finding = probe_js_runtime(which=_which_for())
        assert finding.severity is Severity.DEGRADED
        assert guide_for(finding.remediation_key) is not None


class TestYtDlpProbe:
    def test_yt_dlp_is_importable_here(self) -> None:
        assert probe_yt_dlp().ok is True


class TestOutputDirProbe:
    """The probe answers a question; it must not change anything.

    It used to ``mkdir`` the output directory. Because the default output
    directory is ``./porter_output``, that made running the test suite create a
    directory in the repository root -- and worse, ``porter doctor`` on a user's
    machine silently created a directory wherever they happened to be standing.
    """

    def test_a_writable_directory_passes(self, config) -> None:
        assert probe_output_dir(config).ok is True

    def test_it_does_not_create_a_missing_directory(self, tmp_path) -> None:
        """Regression: the probe used to create the directory it was checking.

        Asserted both ways round, because "did not create it" is only meaningful
        together with "still passed": a probe that refused to answer for a
        missing directory would also pass the first assertion.
        """
        cfg = PorterConfig(output_dir=tmp_path / "a" / "b" / "c")

        finding = probe_output_dir(cfg)

        assert finding.ok is True
        assert not (tmp_path / "a").exists(), "the probe must not create directories"

    def test_a_missing_directory_reports_the_ancestor_it_probed(self, tmp_path) -> None:
        """The detail must say which directory was actually tested.

        Otherwise "X is writable" is a confusing thing to read about a directory
        that does not exist.
        """
        cfg = PorterConfig(output_dir=tmp_path / "a" / "b" / "c")

        finding = probe_output_dir(cfg)

        assert str(tmp_path) in finding.detail
        assert str(tmp_path / "a" / "b" / "c") in finding.detail

    def test_the_probe_file_is_cleaned_up(self, config) -> None:
        Path(config.output_dir).mkdir(parents=True, exist_ok=True)

        probe_output_dir(config)

        assert list(Path(config.output_dir).iterdir()) == []

    def test_a_deep_missing_path_probes_the_nearest_existing_ancestor(self, tmp_path) -> None:
        """Not the root: the nearest one is the directory whose permissions matter."""
        near = tmp_path / "exists"
        near.mkdir()
        cfg = PorterConfig(output_dir=near / "x" / "y")

        finding = probe_output_dir(cfg)

        assert finding.ok is True
        assert str(near) in finding.detail
        assert list(near.iterdir()) == []

    def test_an_unwritable_directory_blocks(self, tmp_path, monkeypatch) -> None:
        cfg = PorterConfig(output_dir=tmp_path / "out")

        def refuse(self, *args, **kwargs):
            raise OSError(30, "Read-only file system")

        monkeypatch.setattr(Path, "write_text", refuse)

        finding = probe_output_dir(cfg)
        assert finding.severity is Severity.BLOCKER
        assert "Read-only file system" in finding.detail
        assert guide_for(finding.remediation_key) is not None

    def test_a_directory_that_cannot_be_created_blocks(self, tmp_path) -> None:
        # A file where a directory is expected.
        blocker = tmp_path / "afile"
        blocker.write_text("x", encoding="utf-8")
        cfg = PorterConfig(output_dir=blocker / "sub")

        finding = probe_output_dir(cfg)
        assert finding.severity is Severity.BLOCKER
        assert str(blocker) in finding.detail


class TestEncoderProbe:
    def test_hardware_is_reported_as_a_pass(self) -> None:
        selector = EncoderSelector(FakeRunnerNvenc({"h264_nvenc": (0, "")}))
        finding = probe_encoder(selector)
        assert finding.ok is True
        assert "hardware" in finding.detail

    def test_software_fallback_is_info_not_degraded(self) -> None:
        """It works. Calling it degraded would put "no GPU" beside "no ffmpeg"."""
        selector = EncoderSelector(FakeRunnerNvenc({}), cpu_count=4)
        finding = probe_encoder(selector)

        assert finding.severity is Severity.INFO
        assert finding.ok is True
        assert "libx264" in finding.detail

    def test_the_rejection_reasons_are_surfaced(self) -> None:
        """An operator asking "why not NVENC" needs that answer, not a generic one."""
        selector = EncoderSelector(
            FakeRunnerNvenc({"h264_nvenc": (1, "Cannot load libcuda.so.1")}), cpu_count=4
        )
        finding = probe_encoder(selector)

        assert "CUDA driver" in finding.detail, "the actual reason must be in the report"

    def test_encoder_profiles_are_all_reported(self) -> None:
        selector = EncoderSelector(FakeRunnerNvenc({}), cpu_count=4)
        assert len(selector.report()) == len(HARDWARE_PROFILES)


class UnusedRunner(FakeRunner):
    """A runner for reports whose probes do not need ffmpeg to answer."""


class FakeRunnerNvenc:
    """Minimal FFmpegRunner stand-in for encoder probing."""

    def __init__(self, verdicts: dict[str, tuple[int, str]]) -> None:
        self.verdicts = verdicts

    def run(self, args, *, what, check=True, timeout=None):
        encoder = args[args.index("-c:v") + 1]
        code, stderr = self.verdicts.get(encoder, (1, "unknown encoder"))
        return SimpleNamespace(returncode=code, stdout="", stderr=stderr)


# ----------------------------------------------------------------------
# probe_all
# ----------------------------------------------------------------------


class TestProbeAll:
    def _context(
        self, config: PorterConfig, present_tools: FFmpegTools, **kwargs: Any
    ) -> ProbeContext:
        defaults: dict[str, Any] = {
            "tools": present_tools,
            "runner": FakeRunner(),
            "which": _which_for("deno"),
            "cpu_count": 8,
        }
        defaults.update(kwargs)
        return ProbeContext(config=config, **defaults)

    def test_reports_every_capability(self, config, present_tools) -> None:
        report = probe_all(config, context=self._context(config, present_tools))
        keys = {f.key for f in report.findings}

        assert {
            "python",
            "ffmpeg",
            "ffmpeg_version",
            "libass",
            "encoder",
            "js_runtime",
            "font",
            "yt_dlp",
            "output_dir",
        } <= keys

    def test_a_missing_ffmpeg_produces_one_blocker_not_five(self, config, present_tools) -> None:
        """Every dependent probe would otherwise report its own misleading failure."""
        report = probe_all(
            config,
            context=self._context(
                config,
                present_tools,
                runner=None,
                tools=FFmpegTools(ffmpeg="nope-ffmpeg", ffprobe="nope-ffprobe"),
            ),
        )
        assert [f.key for f in report.blockers] == ["ffmpeg"]
        assert report.get("libass") is None, "no ffmpeg means the libass question is moot"

    def test_a_healthy_machine_reports_ok(self, config, present_tools) -> None:
        report = probe_all(config, context=self._context(config, present_tools))
        assert report.ok is True

    def test_a_hostile_machine_still_produces_a_report(self, config, present_tools) -> None:
        """Probes must describe a broken machine, not raise while describing it."""
        report = probe_all(
            config,
            context=self._context(config, present_tools, runner=FailingRunner(), which=_which_for()),
        )
        assert report.findings
        assert report.get("libass").ok is False

    def test_an_unusable_ffmpeg_is_degraded_not_blocked(self, config, present_tools) -> None:
        """ffmpeg is present but its filter/encoder questions cannot be answered.

        Still not a blocker: the phases needing no filter or hardware encoder can
        run. Recording this as a blocker would refuse work that would succeed,
        which is the mistake the severity split exists to prevent.
        """
        report = probe_all(
            config,
            context=self._context(config, present_tools, runner=FailingRunner(), which=_which_for()),
        )
        assert report.get("libass").severity is Severity.DEGRADED
        assert report.ok is True

    def test_encoder_selection_is_reused_when_supplied(self, config, present_tools) -> None:
        selector = EncoderSelector(FakeRunnerNvenc({"h264_nvenc": (0, "")}))
        report = probe_all(
            config, context=self._context(config, present_tools, selector=selector)
        )
        assert report.get("encoder").detail.endswith("(hardware)")

    def test_platform_is_recorded(self, config, present_tools) -> None:
        assert probe_all(config, context=self._context(config, present_tools)).platform


# ----------------------------------------------------------------------
# The fact/prose contract
# ----------------------------------------------------------------------


class TestGuideCoverage:
    """Every key a probe can emit must have text behind it.

    A missing guide is safe at runtime — the renderer falls back to the finding's
    detail — but it is a silent hole in the operator experience. That is the kind
    of thing that should fail a test rather than a user.
    """

    def test_every_remediation_key_has_a_guide(self, config) -> None:
        report = probe_all(
            config,
            context=ProbeContext(
                config=config,
                runner=FailingRunner(),
                which=_which_for(),
                cpu_count=1,
            ),
        )
        emitted = {f.remediation_key for f in report.findings if f.remediation_key}
        assert emitted, "the failing machine should name at least one key"
        for key in emitted:
            assert guide_for(key) is not None, f"no guide for {key}"

    def test_every_guide_is_reachable_from_a_probe(self) -> None:
        """The other direction: dead guides are prose nobody will ever read."""
        assert "ffmpeg_libass" in GUIDES
        assert "js_runtime" in GUIDES


class TestGuides:
    def test_every_guide_has_a_summary(self) -> None:
        for key, guide in GUIDES.items():
            assert guide.summary, f"{key} has no summary"

    def test_guides_are_plain_ascii(self) -> None:
        """They are printed to terminals of unknown encoding and shipped in JSON."""
        for key, guide in GUIDES.items():
            rendered = guide.render() + (guide.note or "") + (guide.url or "")
            assert rendered.isascii(), f"{key} contains non-ASCII"

    def test_render_hides_steps_when_not_verbose(self) -> None:
        guide = GUIDES["ffmpeg"]
        assert "apt install" not in guide.render(verbose=False)
        assert "apt install" in guide.render()

    def test_unknown_and_none_keys_return_none(self) -> None:
        assert guide_for("no-such-key") is None
        assert guide_for(None) is None

    def test_a_guide_can_render_without_optional_fields(self) -> None:
        from porter.doctor.guides import Remediation

        assert Remediation(summary="just this").render() == "just this"


class TestNoStdoutWrites:
    """stdout is the MCP transport; the doctor must not print to it."""

    def test_probing_writes_nothing_to_stdout(self, config, capsys) -> None:
        probe_all(
            config,
            context=ProbeContext(config=config, runner=FakeRunner(), which=_which_for("deno")),
        )
        assert capsys.readouterr().out == ""


class TestProbeSubprocessHygiene:
    """The helper processes must not be able to eat the MCP JSON-RPC stream."""

    def test_probes_pass_devnull_stdin(self, monkeypatch) -> None:
        from porter.doctor import probes

        captured: dict[str, Any] = {}

        def fake_run(cmd, **kwargs):
            captured.update(kwargs)
            return subprocess.CompletedProcess(cmd, 0, stdout="Microsoft YaHei", stderr="")

        monkeypatch.setattr(probes.subprocess, "run", fake_run)
        monkeypatch.setattr(probes.shutil, "which", lambda name: "/usr/bin/fc-match")

        probes._fc_match("Microsoft YaHei")

        assert captured["stdin"] is subprocess.DEVNULL
        assert captured["timeout"] is not None
        assert captured["check"] is False

    def test_a_helper_that_cannot_start_returns_none(self, monkeypatch) -> None:
        """Probes report; they never raise. A broken machine is their subject."""
        from porter.doctor import probes

        def explode(*args, **kwargs):
            raise OSError(2, "No such file or directory")

        monkeypatch.setattr(probes.subprocess, "run", explode)
        assert probes._run_probe(["fc-match", "x"]) is None

    def test_a_hanging_helper_times_out_without_raising(self, monkeypatch) -> None:
        from porter.doctor import probes

        def timeout(*args, **kwargs):
            raise subprocess.TimeoutExpired(cmd="fc-match", timeout=10)

        monkeypatch.setattr(probes.subprocess, "run", timeout)
        assert probes._run_probe(["fc-match", "x"]) is None


class TestRouteProbes:
    """The doctor answers "what will this job actually do", not just "is it installed".

    Without an LLM key the pipeline falls through to reverse-engineered, key-free
    endpoints. That is a real reliability difference and hiding it behind a plain
    pass would be the kind of silent downgrade this project keeps finding.
    """

    def test_no_keys_reports_that_the_free_path_is_broken(self) -> None:
        """Not just "unverified" any more: measured non-functional on 2026-09-22.

        Google Web returned ``{"result":[]}`` for every request across three
        speech segments and two language variants, so the message has to say the
        job will fail rather than implying it might work.
        """
        finding = probe_asr_route(PorterConfig(), which=_which_for())

        assert finding.ok is True, "it is not a failure, it is a fact"
        assert "do not transcribe" in finding.detail
        assert "key" in finding.detail
        assert "will fail" in finding.detail

    def test_a_whisper_key_selects_the_documented_path(self) -> None:
        config = PorterConfig(asr={"whisper_api_key": "sk-test"})
        finding = probe_asr_route(config, which=_which_for())

        assert "Whisper API" in finding.detail
        assert "unverified" not in finding.detail

    def test_an_llm_key_also_enables_whisper(self) -> None:
        """v0.1 fell back from the Whisper key to the LLM key; keep that order."""
        config = PorterConfig(llm={"api_key": "sk-test"})
        assert "Whisper API" in probe_asr_route(config, which=_which_for()).detail

    def test_the_videocaptioner_binary_is_noticed(self) -> None:
        finding = probe_asr_route(PorterConfig(), which=_which_for("videocaptioner"))
        assert "VideoCaptioner" in finding.detail

    def test_translation_without_a_key_reports_the_free_endpoints(self) -> None:
        finding = probe_translation_route(PorterConfig())

        assert finding.severity is Severity.INFO
        assert "Bing" in finding.detail
        assert "unverified" in finding.detail

    def test_translation_with_a_key_names_the_model(self) -> None:
        config = PorterConfig(llm={"api_key": "sk-test", "model": "deepseek-chat"})
        finding = probe_translation_route(config)

        assert "LLM" in finding.detail
        assert "deepseek-chat" in finding.detail

    def test_both_routes_appear_in_the_full_report(self, config) -> None:
        report = probe_all(
            config,
            context=ProbeContext(config=config, runner=UnusedRunner(), which=_which_for()),
        )
        assert report.get("asr_route") is not None
        assert report.get("translation_route") is not None

    def test_the_routes_never_block(self) -> None:
        """A configuration choice is not a broken machine."""
        report = CapabilityReport(
            findings=[probe_asr_route(PorterConfig(), which=_which_for()), probe_translation_route(PorterConfig())]
        )
        assert report.ok is True
