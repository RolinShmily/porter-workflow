"""Platform adapters: URL identification and raw-material extraction.

Layout
------
``spec.py``
    :class:`~porter.platforms.spec.PlatformSpec` — the declarative per-platform
    data (URLs, format selector, title cleaner, subtitle policy).
``base.py``
    :class:`~porter.platforms.base.YtDlpExtractor` — the one pipeline, driven by
    a spec.
``registry.py``
    Explicit, idempotent registration and lookup.
``ydl.py``
    The single :func:`~porter.platforms.ydl.build_ydl` construction point, with
    stdout safety enforced.
``titles.py``
    Title derivation shared by the caption-based platforms.
``{youtube,x,instagram,tiktok,bilibili}.py``
    One spec each. No pipeline code.

The five v0.1 extractors were 2565 lines of near-duplicate code; the target for
this package is one ~500-line template, one ~120-line spec module, and roughly
60 lines per platform.
"""

from porter.platforms.base import YtDlpExtractor
from porter.platforms.registry import (
    PlatformRegistry,
    get_extractor,
    identify_platform,
    register,
    register_builtins,
    registry,
)
from porter.platforms.spec import PlatformSpec, SubtitleSource
from porter.platforms.titles import bilibili_title, caption_to_title, tweet_to_title
from porter.platforms.ydl import YdlPolicy, build_ydl, download_progress_hook

__all__ = [
    "PlatformRegistry",
    "PlatformSpec",
    "SubtitleSource",
    "YdlPolicy",
    "YtDlpExtractor",
    "bilibili_title",
    "build_ydl",
    "caption_to_title",
    "download_progress_hook",
    "get_extractor",
    "identify_platform",
    "register",
    "register_builtins",
    "registry",
    "tweet_to_title",
]

# Populate the registry on first import, so callers never have to remember to.
# register_builtins() is idempotent and explicitly ordered, unlike v0.1's
# decorator side effects.
register_builtins()
