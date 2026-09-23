"""Ported from ``main:tests/test_extractors.py`` — the parts not covered by the
other ported files.

``test_extractors.py`` held eight tests. Their disposition:

============================================  ==================================
v0.1 test                                     where it went
============================================  ==================================
``test_sanitize_filename``                    ``tests/unit/test_utils_text.py``
``test_youtube_extractor_can_handle``         ``tests/unit/test_platforms.py``
``test_get_extractor_factory``                **here**
``test_youtube_extractor_subtitles_selection``  ``test_subtitle_conversion.py``
``test_youtube_extractor_chinese_subtitles_selection``  ``test_subtitle_conversion.py``
``test_convert_vtt_to_srt``                   ``test_subtitle_conversion.py``
``test_youtube_extractor_mock_run``           ``test_fetch_pipeline.py`` (superseded)
``test_enhance_audio_for_asr``                ``test_fetch_pipeline.py`` (superseded)
============================================  ==================================

"Superseded" means the harness changed shape rather than the assertion being
dropped: ``extract_raw_materials`` became ``fetch``, and ``enhance_audio_for_asr``
returned ``True``/``False`` where ``enhance_for_asr`` returns the output path or
``None``. The behaviours they covered — a complete ``raw/`` tree, an unenhanced
fallback for a missing input — are asserted in ``test_fetch_pipeline.py``.

The two divergences below are deliberate and are called out rather than hidden.
"""

from __future__ import annotations

import pytest

from porter.errors import UnsupportedPlatformError
from porter.models.materials import TaskLayout
from porter.platforms.base import YtDlpExtractor
from porter.platforms.registry import get_extractor
from porter.platforms.youtube import SPEC as YOUTUBE


def test_get_extractor_factory_returns_the_right_extractor() -> None:
    """From ``test_extractors.py::test_get_extractor_factory``, first half."""
    extractor = get_extractor("https://www.youtube.com/watch?v=abc12345678")
    assert isinstance(extractor, YtDlpExtractor)
    assert extractor.name == "youtube"


def test_get_extractor_factory_rejects_an_unsupported_host() -> None:
    """From ``test_extractors.py::test_get_extractor_factory``, second half.

    **Divergence 1 of 2.** v0.1 asserted::

        with pytest.raises(ValueError, match="Unsupported URL platform"):
            get_extractor("https://vimeo.com/12345678")

    v0.2 raises :class:`~porter.errors.UnsupportedPlatformError`, which is a
    :class:`~porter.errors.PorterError` and **not** a ``ValueError``.

    Why: ``ValueError`` means "you passed a malformed argument". An unsupported
    host is a well-formed URL this build has no handler for, and the two need
    different handling — one is a programming error, the other is a normal answer
    a caller should be able to catch and report. ``PorterError`` also carries
    ``code`` and ``exit_code``, which the CLI and MCP surface to callers.

    The assertion's intent — this URL is refused, with a message naming the
    problem — is unchanged. The message is also richer now: it lists the
    platforms that *would* work.
    """
    with pytest.raises(UnsupportedPlatformError) as excinfo:
        get_extractor("https://vimeo.com/12345678")

    # The class and code carry the "unsupported" meaning; the message states the
    # concrete fact and then the supported set.
    assert excinfo.value.code == "unsupported_platform"
    assert excinfo.value.url == "https://vimeo.com/12345678"
    assert "no platform" in str(excinfo.value).lower()
    assert "youtube" in str(excinfo.value), "the error should name what is supported"
    assert set(excinfo.value.supported) >= {"youtube", "bilibili", "x"}


class TestSafeTitle:
    """From ``test_extractors.py::test_youtube_extractor_mock_run``, which asserted
    ``result.metadata.safe_title == "Test_Video_Title"``.

    The surrounding mocking is superseded by ``test_fetch_pipeline.py``; the
    ``safe_title`` contract is kept here because it is what actually reaches the
    filesystem.
    """

    def test_safe_title_replaces_spaces_with_underscores(self, tmp_path) -> None:
        layout = TaskLayout.build(tmp_path, "test_id_123", "Test Video Title")
        assert layout.safe_title == "Test_Video_Title"
        assert layout.task_dir.name == "test_id_123_Test_Video_Title"

    def test_safe_title_is_derived_from_the_spec_cleaner(self) -> None:
        metadata = YtDlpExtractor(YOUTUBE)._build_metadata(
            "https://www.youtube.com/watch?v=abc12345678",
            {"id": "abc12345678", "title": "Some Video - YouTube", "formats": []},
        )
        # sanitize_filename, not the raw title: the raw string would put a space
        # and a hyphen straight into the directory name.
        assert metadata.title == "Some Video - YouTube"
        assert "/" not in metadata.safe_title
