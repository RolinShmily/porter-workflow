"""Declarative description of a supported platform.

``v0.1``'s five extractors totalled 2565 lines, and the pipeline inside each
``extract_raw_materials`` was the same sequence of steps pasted five times:
fetch info, download streams, remux to ``raw/video.mp4``, extract a 16 kHz WAV,
enhance it for ASR, fetch a cover, write subtitles, save metadata, clean up.

What actually differed between platforms was **data**, not control flow:

* which URLs belong to the platform,
* which yt-dlp format selector and player clients to use,
* how to turn platform metadata into a title,
* where subtitles come from and how to convert them,
* whether the video is vertical by default,
* whether to retry on a rate limit.

:class:`PlatformSpec` holds exactly that data. The behaviour lives once in
:class:`porter.platforms.base.YtDlpExtractor`, which reads a spec. Adding a
platform should mean writing a spec, not copying a pipeline.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from porter.subtitles.srt import select_chinese_lang, select_source_lang

__all__ = ["PlatformSpec", "SubtitleSource", "default_title"]


def default_title(raw_title: str | None, video_id: str) -> str:
    """Fallback title cleaner: the platform's title, else the video id."""
    return (raw_title or "").strip() or video_id


@dataclass(frozen=True)
class SubtitleSource:
    """Where a platform's subtitles come from and how to read them.

    Attributes:
        remote: Whether yt-dlp should fetch subtitle tracks itself. ``False``
            means **no subtitle is fetched at all** -- ``plan_subtitles`` returns
            an empty plan -- so this is not a "fetch it some other way" flag. x
            and instagram set it because they have no usable track.

            Bilibili used to set it, on the theory that its CC track was fetched
            over HTTP instead. Nothing implemented that, so bilibili silently
            discarded its own exact Chinese track and paid for ASR on every job.
            yt-dlp exposes nothing for bilibili anonymously, but it may with
            cookies, so the request is made and simply finds nothing when there
            is nothing to find.
        prefer_existing_chinese: Reuse a platform-provided Chinese track instead
            of paying for translation. Saves a whole LLM round trip.
    """

    remote: bool = True
    prefer_existing_chinese: bool = True


# eq=False on purpose: `extractor_args` is a mapping, so the generated
# __hash__ would raise TypeError the first time a spec landed in a set or was
# used as a dict key. Identity semantics cost nothing here and remove the trap.
@dataclass(frozen=True, eq=False)
class PlatformSpec:
    """Everything that differs between platforms.

    A spec satisfies :class:`porter.platforms.registry.UrlHandler`, so the
    registry can hold specs directly and URL identification works without
    constructing an extractor.

    Attributes:
        name: Stable identifier, also the registry key.
        display_name: Human-facing name for reports.
        url_patterns: Anchored patterns matched with :meth:`re.Pattern.search`.
        url_hosts: Substrings used as a fallback when no pattern matches, which
            keeps unusual deep links working.
        video_id_group: Named group in ``url_patterns`` holding the video id.
        default_vertical: Assume a 9:16 frame when metadata omits dimensions.
            True for TikTok, whose API often reports nothing useful.
        clean_title: ``(raw_title, video_id) -> title``.
        format_selector: yt-dlp ``format`` string.
        player_clients: YouTube player clients to try, in order.
        subtitles: Subtitle acquisition policy.
        retries_on_rate_limit: Retry once with a different format when the
            platform rejects the first attempt.
        extractor_args: Extra yt-dlp ``extractor_args`` for this platform.
        notes: Free-form remark surfaced by ``porter doctor`` / ``porter inspect``.
    """

    name: str
    display_name: str
    url_patterns: tuple[re.Pattern[str], ...]
    url_hosts: tuple[str, ...]
    video_id_group: str = "id"
    default_vertical: bool = False
    clean_title: Callable[[str | None, str], str] = default_title
    format_selector: str = "bestvideo*+bestaudio/best"
    player_clients: tuple[str, ...] | None = None
    subtitles: SubtitleSource = field(default_factory=SubtitleSource)
    retries_on_rate_limit: bool = False
    extractor_args: dict[str, Any] = field(default_factory=dict)
    #: Query parameters that look like tracking noise globally but are
    #: functional here. YouTube's ``t=15s`` is the motivating case: it is a
    #: start timestamp, while X's ``t=`` is share-tracking. A set rather than a
    #: comparison because ``t`` is not the only ambiguous short name.
    keep_query_params: frozenset[str] = frozenset()
    notes: str = ""

    def can_handle(self, url: str) -> bool:
        """Return True when ``url`` belongs to this platform.

        Patterns are tried first; the host-substring fallback catches URLs that
        are valid for the platform but do not match a known shape (share
        variants, regional domains, deep links).
        """
        if any(pattern.search(url) for pattern in self.url_patterns):
            return True
        return any(host in url for host in self.url_hosts)

    def video_id(self, url: str) -> str | None:
        """Extract the platform's video id from ``url``, or None.

        Only meaningful when a pattern matched; the host fallback has no id to
        offer and the caller must get it from yt-dlp's metadata instead.
        """
        for pattern in self.url_patterns:
            match = pattern.search(url)
            if match and (found := match.groupdict().get(self.video_id_group)):
                return found
        return None

    def select_source_lang(
        self,
        subtitles: dict[str, Any],
        *,
        is_auto: bool = False,
        declared_lang: str | None = None,
    ) -> str | None:
        """Choose the source-language track, per the shared priority list."""
        return select_source_lang(subtitles, is_auto=is_auto, declared_lang=declared_lang)

    def select_chinese_lang(self, subtitles: dict[str, Any]) -> str | None:
        """Choose an existing Chinese track, per the shared priority list."""
        return select_chinese_lang(subtitles)

    @property
    def stripped_query_params(self) -> frozenset[str]:
        """Tracking parameters to remove from this platform's URLs.

        Derived from the global set minus :attr:`keep_query_params`, so a new
        platform exception is data instead of a branch in the cleaning loop.
        """
        from porter.platforms.urls import TRACKING_PARAMS

        return TRACKING_PARAMS - self.keep_query_params
