"""yt-dlp construction must never let a byte reach stdout.

If these fail, ``porter-mcp`` corrupts the JSON-RPC protocol the first time it
downloads anything — a failure that surfaces in the *client* as a parse error,
far from the cause. That is why the options are asserted directly as well as
behaviourally.
"""

from __future__ import annotations

import io
import sys

import pytest

from porter.events import Phase, ProgressUpdated, collect
from porter.logging import configure
from porter.platforms import ydl
from porter.platforms.ydl import (
    JS_RUNTIME_PRIORITY,
    YdlLogRouter,
    YdlPolicy,
    available_js_runtimes,
    build_ydl,
    download_progress_hook,
)


def _opts(ydl) -> dict:
    """yt-dlp keeps the merged options in ``params``."""
    return ydl.params


class TestStdoutSafetyOptions:
    def test_progress_printer_is_disabled(self) -> None:
        """``noprogress`` turns the printer into ``QuietMultilinePrinter``."""
        with build_ydl() as ydl:
            assert _opts(ydl)["noprogress"] is True

    def test_screen_output_goes_to_stderr(self) -> None:
        with build_ydl() as ydl:
            assert _opts(ydl)["quiet"] is True

    def test_the_third_stdout_channel_is_redirected(self) -> None:
        """``quiet`` alone is NOT enough.

        ``YoutubeDL.__init__`` sets ``out = sys.stderr if logtostderr else
        sys.stdout``, and ``out`` is what the progress printer writes to. Without
        ``logtostderr`` the download progress lands on stdout even when
        ``quiet=True``.
        """
        with build_ydl() as ydl:
            assert _opts(ydl)["logtostderr"] is True

    def test_a_logger_is_always_attached(self) -> None:
        with build_ydl() as ydl:
            assert isinstance(_opts(ydl)["logger"], YdlLogRouter)

    def test_out_files_never_point_at_stdout(self) -> None:
        """Assert the *effect*, not just the flags.

        ``_out_files`` is computed from the flags, so this is the assertion that
        actually proves stdout is safe, independent of how the flags are named.
        """
        with build_ydl() as ydl:
            assert ydl._out_files.out is not sys.stdout
            assert ydl._out_files.screen is not sys.stdout
            assert ydl._out_files.error is not sys.stdout


class TestProtectedOptions:
    """A caller must not be able to silently re-enable stdout."""

    @pytest.mark.parametrize("key", ["quiet", "noprogress", "logtostderr"])
    def test_disabling_via_extra_raises(self, key: str) -> None:
        with pytest.raises(ValueError, match="cannot be overridden"):
            build_ydl(extra={key: False})

    def test_disabling_via_policy_extra_raises(self) -> None:
        with pytest.raises(ValueError, match="cannot be overridden"):
            build_ydl(YdlPolicy(extra={"quiet": False}))

    def test_removing_the_logger_raises(self) -> None:
        with pytest.raises(ValueError, match="logger"):
            build_ydl(extra={"logger": None})

    def test_error_message_explains_why(self) -> None:
        with pytest.raises(ValueError, match="JSON-RPC"):
            build_ydl(extra={"quiet": False})


class TestPolicyOptions:
    def test_defaults_are_quiet_and_retrying(self) -> None:
        with build_ydl() as ydl:
            assert _opts(ydl)["retries"] == 3
            assert _opts(ydl)["extract_flat"] is False

    def test_cookies_file(self, tmp_path) -> None:
        jar = tmp_path / "cookies.txt"
        with build_ydl(YdlPolicy(cookies_file=str(jar))) as ydl:
            assert _opts(ydl)["cookiefile"] == str(jar)

    def test_cookies_browser_is_a_tuple(self) -> None:
        """yt-dlp expects ``(browser,)``, not a bare string."""
        with build_ydl(YdlPolicy(cookies_browser="chrome")) as ydl:
            assert _opts(ydl)["cookiesfrombrowser"] == ("chrome",)

    def test_remote_components_use_the_documented_form(self) -> None:
        """Regression: v0.1 passed ``{"ejs": "github"}``, which is silently dropped.

        yt-dlp does ``set(params['remote_components'])``, so a dict collapses to
        its keys (``{"ejs"}``), fails the supported-set check, and is removed —
        leaving YouTube's JS challenge unsolved. The documented form is a list of
        ``"name:source"`` strings.
        """
        policy = YdlPolicy()
        assert policy.remote_components == ("ejs:github",)
        assert not isinstance(policy.remote_components, dict)

    def test_remote_components_survive_yt_dlp_validation(self) -> None:
        """Assert the effect: yt-dlp normalises to a non-empty set.

        This is the assertion that would have caught the v0.1 bug — a dropped
        component yields an empty set here.
        """
        with build_ydl() as ydl:
            assert ydl.params["remote_components"] == {"ejs:github"}

    def test_remote_components_can_be_disabled(self) -> None:
        with build_ydl(YdlPolicy(remote_components=())) as ydl:
            assert not _opts(ydl).get("remote_components")

    def test_js_runtimes_left_to_yt_dlp_by_default(self) -> None:
        """The *policy* default stays ``None``; the resolved options do not.

        See :class:`TestJsRuntimeHandover` for what actually reaches yt-dlp.
        """
        assert YdlPolicy().js_runtimes is None

    def test_js_runtimes_can_be_pinned(self) -> None:
        with build_ydl(YdlPolicy(js_runtimes={"deno": {}})) as ydl:
            assert _opts(ydl)["js_runtimes"] == {"deno": {}}

    def test_player_clients_are_passed_through(self) -> None:
        with build_ydl(YdlPolicy(player_clients=("web", "ios"))) as ydl:
            args = _opts(ydl)["extractor_args"]
            assert args["youtube"]["player_client"] == ["web", "ios"]

    def test_player_clients_do_not_clobber_platform_args(self) -> None:
        """Regression: assigning extractor_args wholesale lost sibling keys."""
        policy = YdlPolicy(
            player_clients=("web",),
            extractor_args={"bilibili": {"prefer_multi_flv": True}},
        )
        with build_ydl(policy) as ydl:
            args = _opts(ydl)["extractor_args"]
        assert args["youtube"]["player_client"] == ["web"]
        assert args["bilibili"] == {"prefer_multi_flv": True}

    def test_policy_is_not_mutated_by_a_build(self) -> None:
        policy = YdlPolicy(extractor_args={"bilibili": {"x": 1}})
        with build_ydl(policy):
            pass
        assert policy.extractor_args == {"bilibili": {"x": 1}}


class TestLogRouter:
    def test_routes_to_stderr_not_stdout(self, capsys: pytest.CaptureFixture[str]) -> None:
        configure(level="DEBUG")
        router = YdlLogRouter()
        router.debug("screen message")
        router.error("stderr message")

        captured = capsys.readouterr()
        assert captured.out == "", "yt-dlp logging reached stdout"
        assert "screen message" in captured.err
        assert "stderr message" in captured.err

    def test_accepts_an_injected_logger(self) -> None:
        seen: list[str] = []

        class Sink:
            def debug(self, msg, *a) -> None:
                seen.append(f"debug:{msg % a if a else msg}")

            info = warning = error = debug

        YdlLogRouter(Sink()).debug("hello")
        assert seen == ["debug:yt-dlp: hello"]


class TestProgressHook:
    def _hook(self, sink, **kw):
        return download_progress_hook(sink, **kw)

    def test_emits_on_meaningful_change(self) -> None:
        events: list = []
        hook = self._hook(events.append)

        hook({"status": "downloading", "downloaded_bytes": 0, "total_bytes": 1000})
        hook({"status": "downloading", "downloaded_bytes": 400, "total_bytes": 1000})
        hook({"status": "downloading", "downloaded_bytes": 900, "total_bytes": 1000})

        percents = [e.percent for e in events if isinstance(e, ProgressUpdated)]
        assert percents == [0.0, 40.0, 90.0]

    def test_throttles_sub_percent_churn(self) -> None:
        """yt-dlp fires this many times a second; a client does not need that."""
        events: list = []
        hook = self._hook(events.append)

        calls = 0
        for downloaded in range(0, 1000, 5):  # 200 calls in 0.5% steps
            hook({"status": "downloading", "downloaded_bytes": downloaded, "total_bytes": 1000})
            calls += 1

        emitted = len([e for e in events if isinstance(e, ProgressUpdated)])
        assert calls == 200
        # One event per 1% bucket, i.e. at most 100 — deterministically.
        assert emitted == 100

    def test_coarser_throttle_emits_fewer_events(self) -> None:
        def count(throttle: float) -> int:
            events: list = []
            hook = self._hook(events.append, throttle_percent=throttle)
            for downloaded in range(0, 1001, 5):
                hook(
                    {
                        "status": "downloading",
                        "downloaded_bytes": downloaded,
                        "total_bytes": 1000,
                    }
                )
            return len([e for e in events if isinstance(e, ProgressUpdated)])

        assert count(10.0) < count(1.0)

    def test_always_emits_completion(self) -> None:
        events: list = []
        hook = self._hook(events.append)
        hook({"status": "downloading", "downloaded_bytes": 1000, "total_bytes": 1000})
        hook({"status": "finished"})

        assert events[-1].percent == 100.0
        assert events[-1].message == "download finished"

    def test_uses_estimated_total_when_exact_is_absent(self) -> None:
        events: list = []
        hook = self._hook(events.append)
        hook({"status": "downloading", "downloaded_bytes": 500, "total_bytes_estimate": 1000})
        assert events[0].percent == 50.0

    def test_unknown_total_is_ignored_rather_than_guessed(self) -> None:
        events: list = []
        hook = self._hook(events.append)
        hook({"status": "downloading", "downloaded_bytes": 500})
        assert events == []

    def test_non_download_statuses_are_ignored(self) -> None:
        events: list = []
        hook = self._hook(events.append)
        hook({"status": "error"})
        hook({})
        assert events == []

    def test_phase_is_configurable(self) -> None:
        events: list = []
        hook = self._hook(events.append, phase=Phase.PREPARE)
        hook({"status": "downloading", "downloaded_bytes": 1, "total_bytes": 1})
        assert events[0].phase is Phase.PREPARE

    def test_integrates_with_a_real_event_sink(self) -> None:
        events: list = []
        sink = collect(events)
        hook = download_progress_hook(sink)
        hook({"status": "downloading", "downloaded_bytes": 50, "total_bytes": 100})
        assert events and events[0].type == "progress_updated"


class TestNothingReachesStdout:
    """The end-to-end property, asserted on the real streams."""

    def test_building_and_logging_writes_nothing_to_stdout(self) -> None:
        out = io.StringIO()
        original = sys.stdout
        sys.stdout = out
        try:
            with build_ydl() as ydl:
                ydl.to_screen("this would have corrupted the protocol")
                ydl.to_stderr("so would this")
        finally:
            sys.stdout = original

        assert out.getvalue() == "", f"yt-dlp wrote to stdout: {out.getvalue()!r}"


class TestJsRuntimeHandover:
    """A runtime on the machine has to be a runtime yt-dlp can actually use.

    yt-dlp's default is ``{'deno': {}}`` -- Deno *only*, hard coded in
    ``YoutubeDL.__init__`` -- and it detects nothing else. This class exists
    because of one measured case: the machine this repository is developed on has
    Node installed and no Deno, so yt-dlp had **no usable runtime at all** while
    ``porter doctor`` reported "JavaScript runtime: OK, node at ... (fully
    supported by yt-dlp)". The claim was true of yt-dlp and false of porter,
    because nothing here ever handed Node over.
    """

    def _resolved(self, monkeypatch: pytest.MonkeyPatch, installed: set[str]) -> dict:
        """The options yt-dlp receives, for a machine with ``installed`` present."""
        monkeypatch.setattr(
            ydl,
            "available_js_runtimes",
            lambda *_a, **_k: {name: {} for name in JS_RUNTIME_PRIORITY if name in installed},
        )
        with build_ydl(YdlPolicy()) as model:
            return _opts(model)

    def test_a_node_only_machine_hands_node_over(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # The regression: this used to resolve to nothing usable at all.
        assert self._resolved(monkeypatch, {"node"})["js_runtimes"] == {"node": {}}

    def test_deno_wins_when_it_is_present(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Upstream's preference survives: handing over a superset does not demote
        # Deno, because yt-dlp selects by its own priority among what it is given.
        resolved = self._resolved(monkeypatch, {"deno", "node"})["js_runtimes"]
        assert list(resolved) == ["deno", "node"]

    def test_finding_nothing_changes_nothing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A machine with no runtime behaves exactly as it did before this existed.

        The assertion is against yt-dlp's *own* fallback rather than the absence of
        the key, because the key is never absent: ``YoutubeDL.__init__`` runs
        ``params['js_runtimes'] = params.get('js_runtimes', {'deno': {}})`` and so
        injects ``{'deno': {}}`` whenever nameless. What matters is that porter
        contributed nothing to it.
        """
        assert self._resolved(monkeypatch, set())["js_runtimes"] == {"deno": {}}

    def test_an_explicit_policy_still_wins(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(ydl, "available_js_runtimes", lambda *_a, **_k: {"node": {}})
        with build_ydl(YdlPolicy(js_runtimes={"bun": {}})) as model:
            assert _opts(model)["js_runtimes"] == {"bun": {}}

    def test_the_priority_order_is_yt_dlps_own(self) -> None:
        # Pinned to yt-dlp's documented order. ``bun`` last, below ``quickjs``, is
        # the part intuition about speed gets backwards.
        assert JS_RUNTIME_PRIORITY == ("deno", "node", "quickjs", "bun")

    @pytest.mark.parametrize(
        ("installed", "expected"),
        [
            ({"deno"}, {"deno": {}}),
            ({"node"}, {"node": {}}),
            ({"quickjs"}, {"quickjs": {}}),
            ({"bun"}, {"bun": {}}),
            ({"deno", "bun"}, {"deno": {}, "bun": {}}),
            ({"node", "quickjs"}, {"node": {}, "quickjs": {}}),
            (set(), {}),
        ],
    )
    def test_only_what_is_on_path_is_listed(
        self, installed: set[str], expected: dict
    ) -> None:
        found = available_js_runtimes(
            which=lambda name: f"/usr/bin/{name}" if name in installed else None
        )
        assert found == expected
