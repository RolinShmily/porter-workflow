"""Per-URL dispatch to the platform extractor that claims it.

The pipeline's :class:`~porter.ports.Downloader` port is per-video
(``fetch(url, ctx)``), but the registry is a *collection* of extractors keyed by
URL pattern. Something has to bridge the two, and this is that bridge.

Why it is a separate object rather than methods on
:class:`~porter.platforms.registry.PlatformRegistry`:

* The registry's contract is deliberately narrow —
  :class:`~porter.platforms.registry.UrlHandler` is only ``name`` + ``can_handle``
  — so it can hold stubs in tests without yt-dlp. Adding ``fetch`` there would
  force every stub to implement downloading.
* Dispatch is a *pipeline* concern. Keeping it out of the registry leaves the
  registry usable by ``inspect``, which must never download.

The five v0.1 extractor modules each re-derived their own URL matching; a single
dispatcher means "which extractor handles this?" has exactly one answer.
"""

from __future__ import annotations

from porter.context import RunContext
from porter.errors import UnsupportedPlatformError
from porter.models.materials import RawMaterials
from porter.models.metadata import VideoMetadata
from porter.platforms.registry import PlatformRegistry, registry
from porter.ports import Downloader

__all__ = ["PlatformDownloader"]


class PlatformDownloader:
    """Implements :class:`~porter.ports.Downloader` over the platform registry.

    The registry is resolved lazily rather than in ``__init__`` so that
    assembling the pipeline does not trigger the yt-dlp import at CLI start-up.
    ``porter --help`` should not pay for it, and neither should ``porter doctor``
    on a machine with no yt-dlp installed.
    """

    name = "platforms"

    def __init__(self, platforms: PlatformRegistry | None = None) -> None:
        self._platforms = platforms

    @property
    def platforms(self) -> PlatformRegistry:
        """The registry, resolved from the process-wide one on first use."""
        if self._platforms is None:
            self._platforms = registry()
        return self._platforms

    def can_handle(self, url: str) -> bool:
        """Whether any registered extractor recognises ``url``."""
        return self.platforms.find_or_none(url) is not None

    def probe(self, url: str, ctx: RunContext) -> VideoMetadata:
        """Metadata-only inspection, delegated to the matching extractor."""
        return self._extractor(url).probe(url, ctx)

    def fetch(self, url: str, ctx: RunContext) -> RawMaterials:
        """Download and standardise, delegated to the matching extractor."""
        return self._extractor(url).fetch(url, ctx)

    # -- internals ----------------------------------------------------------

    def _extractor(self, url: str) -> Downloader:
        """The registered handler for ``url``, checked for download capability.

        The registry's ``find`` is typed to the narrow
        :class:`~porter.platforms.registry.UrlHandler`, which promises only
        ``can_handle``. Narrowing to :class:`~porter.ports.Downloader` here means
        a handler registered for matching only — a stub, or a future
        metadata-only platform — fails with a clear ``UnsupportedPlatformError``
        at the moment it is asked to download, rather than an ``AttributeError``
        from inside the call.
        """
        handler = self.platforms.find(url)
        if not isinstance(handler, Downloader):
            raise UnsupportedPlatformError(url, supported=self.platforms.names())
        return handler

    def __len__(self) -> int:
        return len(self.platforms)

    def __repr__(self) -> str:
        names = ", ".join(self.platforms.names()) or "none"
        return f"PlatformDownloader(platforms={names})"
