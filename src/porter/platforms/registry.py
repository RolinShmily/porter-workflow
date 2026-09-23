"""Platform extractor registry.

Replaces ``v0.1``'s import side effects
--------------------------------------
``v0.1`` registered extractors with a ``@register_extractor`` decorator that
appended to a module-level ``_EXTRACTORS`` list, relying on
``extractors/__init__.py`` being imported for the decorators to run. That has
three failure modes:

1. **Double registration.** Importing the module a second time under a different
   name (a test harness, a reload, a frozen bundle) appends duplicates, and
   ``get_extractor`` then returns an arbitrary one of them.
2. **Import-order coupling.** Forget one import and the platform silently
   disappears — the failure surfaces at runtime as "unsupported URL".
3. **Non-determinism.** "First match wins" depends on registration order, which
   depends on import order.

This module keeps registration explicit and idempotent:

* Registration happens in :func:`porter.platforms.register_builtins`, called
  once from the package ``__init__``, not as a decorator side effect.
* Registering a name twice **replaces** the previous entry, so imports and
  reloads cannot create duplicates.
* Lookup order is the insertion order of the final mapping, which is a literal
  in :func:`register_builtins` rather than an accident of import order.

Adding a platform is therefore a visible two-line change, not an implicit one.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from porter.errors import UnsupportedPlatformError
from porter.logging import get_logger

__all__ = [
    "PlatformRegistry",
    "canonicalize",
    "get_extractor",
    "identify_platform",
    "register",
    "register_builtins",
    "registry",
]

_logger = get_logger(__name__)


@runtime_checkable
class UrlHandler(Protocol):
    """Minimal structural contract the registry needs.

    Kept deliberately narrow so the registry does not depend on the extractor
    implementation, and so tests can register a stub.

    ``name`` is a read-only property rather than a settable attribute, because
    the real implementations derive it (``YtDlpExtractor.name`` forwards to its
    spec) and a settable declaration would reject them.
    """

    @property
    def name(self) -> str:
        """Stable platform identifier, also the registry key."""
        ...

    def can_handle(self, url: str) -> bool:
        """Return True when this handler recognises ``url``."""
        ...


class PlatformRegistry:
    """An ordered, idempotent mapping of platform name -> handler."""

    def __init__(self) -> None:
        self._handlers: dict[str, UrlHandler] = {}

    def register(self, handler: UrlHandler) -> UrlHandler:
        """Add ``handler``, replacing any existing handler with the same name.

        Returns the handler so this can be used as a decorator when convenient,
        while remaining safe if it is called again.
        """
        name = handler.name
        if name in self._handlers and self._handlers[name] is not handler:
            _logger.debug("platform %r re-registered; replacing previous handler", name)
        self._handlers[name] = handler
        return handler

    def unregister(self, name: str) -> None:
        """Remove ``name`` if present. Used mainly to isolate tests."""
        self._handlers.pop(name, None)

    def clear(self) -> None:
        """Drop every handler. Used mainly to isolate tests."""
        self._handlers.clear()

    def names(self) -> tuple[str, ...]:
        """Registered platform names, in lookup order."""
        return tuple(self._handlers)

    def handlers(self) -> tuple[UrlHandler, ...]:
        """Registered handlers, in lookup order."""
        return tuple(self._handlers.values())

    def identify(self, url: str) -> str | None:
        """Return the name of the handler that recognises ``url``, or None."""
        for handler in self._handlers.values():
            if handler.can_handle(url):
                return handler.name
        return None

    def canonicalize(self, url: str, *, timeout: float = 8.0) -> str:
        """Normalise ``url`` and strip the verified platform's tracking params.

        Expansion before identification: a shortener host says nothing about the
        destination, and the parameters worth removing belong to the destination.

        The strip set is platform-scoped because parameter names collide across
        platforms — ``t`` is a start offset on YouTube and share noise on X. An
        unidentified URL is only scheme-repaired, never parameter-stripped: with
        no platform to consult, guessing which names are safe to drop could
        discard a functional parameter.
        """
        # Imported here, not at module scope: `base` registers itself through
        # this module, so a top-level import would be circular.
        from porter.platforms.base import YtDlpExtractor
        from porter.platforms.urls import CAMPAIGN_PARAMS, clean_url, expand_short_url

        expanded = expand_short_url(url, timeout=timeout)
        handler = self.find_or_none(expanded)
        if not isinstance(handler, YtDlpExtractor):
            # No platform: strip only the never-functional parameters.
            return clean_url(expanded, strip=CAMPAIGN_PARAMS)
        return clean_url(expanded, strip=handler.spec.stripped_query_params)

    def find_or_none(self, url: str) -> UrlHandler | None:
        """Like :meth:`find` but returns None instead of raising."""
        for handler in self._handlers.values():
            if handler.can_handle(url):
                return handler
        return None

    def find(self, url: str) -> UrlHandler:
        """Return the handler for ``url``.

        Raises:
            UnsupportedPlatformError: If no handler recognises the URL.
        """
        for handler in self._handlers.values():
            if handler.can_handle(url):
                return handler
        raise UnsupportedPlatformError(url, supported=self.names())

    def __len__(self) -> int:
        return len(self._handlers)

    def __contains__(self, name: object) -> bool:
        return name in self._handlers


_REGISTRY = PlatformRegistry()

#: Set once so repeated package imports cannot re-register.
_builtins_registered = False


def registry() -> PlatformRegistry:
    """Return the process-wide registry, with the built-ins registered.

    Registration is re-asserted here rather than relying on
    ``porter.platforms.__init__`` having run. That import does happen in
    practice, but only because importing ``porter.platforms.registry`` pulls in
    the parent package first — an accident of import order, not a guarantee. It
    fails loudly when it breaks: the registry is empty, every URL is reported
    unsupported, and the error says ``supported platforms: none registered``.

    :func:`register_builtins` is idempotent, so calling it on every access is a
    flag check, not a reload.
    """
    register_builtins()
    return _REGISTRY


def register(handler: UrlHandler) -> UrlHandler:
    """Register ``handler`` in the process-wide registry."""
    return _REGISTRY.register(handler)


def register_builtins() -> PlatformRegistry:
    """Register the built-in platform handlers exactly once.

    Called from :mod:`porter.platforms`, so importing the package is enough.
    Idempotent: a second call is a no-op, which keeps reloads and repeated
    imports from duplicating entries.
    """
    global _builtins_registered
    if _builtins_registered:
        return _REGISTRY

    # Imported here rather than at module scope so the registry stays importable
    # (and testable with stubs) without the concrete platforms or yt-dlp.
    from porter.platforms import bilibili, instagram, tiktok, x, youtube
    from porter.platforms.base import YtDlpExtractor

    # Order is explicit and meaningful: it is the tie-break for URLs claimed by
    # more than one pattern. YouTube last because its patterns are the broadest.
    for module in (bilibili, x, instagram, tiktok, youtube):
        _REGISTRY.register(YtDlpExtractor(module.SPEC))

    _builtins_registered = True
    return _REGISTRY


def canonicalize(url: str, *, timeout: float = 8.0) -> str:
    """Normalise ``url`` using the registered platform specs."""
    return registry().canonicalize(url, timeout=timeout)


def identify_platform(url: str) -> str | None:
    """Identify the platform of ``url``, or None if unsupported."""
    return _REGISTRY.identify(url)


def get_extractor(url: str) -> UrlHandler:
    """Return the handler for ``url``.

    Raises:
        UnsupportedPlatformError: If no registered handler recognises it.
    """
    return _REGISTRY.find(url)
