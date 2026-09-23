"""URL normalisation: scheme repair, shortener expansion, tracking removal.

Two functions with different failure modes, kept apart on purpose
---------------------------------------------------------------
``clean_url``
    **Pure.** No network, no I/O, deterministic. This is where every interesting
    rule lives (which parameters to strip, and the per-platform exception), so it
    is the part worth testing exhaustively — and the part that must never block.
``expand_short_url``
    **Network.** Optional and best-effort: when it fails, the pipeline proceeds
    with the short URL, which yt-dlp resolves itself anyway.

``resolve_and_clean_url`` composes the two and keeps ``v0.1``'s name and
behaviour.

The rule worth knowing about
----------------------------
Tracking parameters are stripped **globally**, with one deliberate exception:
``t`` is a timestamp on YouTube (``watch?v=...&t=15s``) and share-tracking noise
on X (``?s=20&t=abcdef``). v0.1 encoded this as an ``if is_youtube and k == "t"``
special case buried inside a loop; here a platform declares
:attr:`~porter.platforms.spec.PlatformSpec.keep_query_params` and the strip set
is derived, so adding a platform exception is data rather than a new branch.

``t`` is not the only ambiguous short parameter name, which is why the exception
is a set rather than a hardcoded comparison.
"""

from __future__ import annotations

from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

from porter.logging import get_logger

__all__ = [
    "AMBIGUOUS_PARAMS",
    "CAMPAIGN_PARAMS",
    "SHORTENER_HOSTS",
    "TRACKING_PARAMS",
    "clean_url",
    "expand_short_url",
    "has_tracking_params",
    "is_shortener",
]

_logger = get_logger(__name__)

#: Parameters that are noise on *every* platform: campaign attribution and
#: click identifiers. Safe to remove without knowing where the URL points,
#: which matters because an Instagram link shared through X arrives carrying
#: ``utm_*`` from the messenger rather than from Instagram.
CAMPAIGN_PARAMS: frozenset[str] = frozenset(
    {
        "utm_source",
        "utm_medium",
        "utm_campaign",
        "utm_term",
        "utm_content",
        "fbclid",
        "gclid",
        "igshid",
        "ref_src",
        "ref_url",
    }
)

#: Parameters that are noise on *some* platform and **functional on another**.
#: Removing one of these without knowing the destination is destructive:
#:
#: * ``t`` is a start offset on YouTube (``watch?v=...&t=15s``) and share noise
#:   on X (``?s=20&t=abcdef``).
#: * ``from``/``ts``/``rt`` are Bilibili share parameters, but plain words that
#:   other sites use for real routing.
#:
#: Applied only through :meth:`porter.platforms.registry.PlatformRegistry.canonicalize`,
#: which knows the platform. This split is the fix for v0.1's single global list,
#: which stripped ``t`` on YouTube and needed an ``if is_youtube`` special case to
#: undo its own damage.
AMBIGUOUS_PARAMS: frozenset[str] = frozenset(
    {
        "s",
        "t",
        "is_copy_url",
        "is_from_webapp",
        "sender_device",
        "_r",
        "tt_from",
        "spm_id_from",
        "from_spmid",
        "vd_source",
        "from_source",
        "from",
        "share_source",
        "share_medium",
        "share_plat",
        "share_session_id",
        "share_tag",
        "bbid",
        "ts",
        "unique_k",
        "rt",
    }
)

#: Everything removable when the platform is known.
TRACKING_PARAMS: frozenset[str] = CAMPAIGN_PARAMS | AMBIGUOUS_PARAMS

#: Hosts that serve nothing but a redirect. A superset of v0.1's list. Matching
#: is by exact host or by subdomain, so ``www.t.co`` is handled too.
SHORTENER_HOSTS: frozenset[str] = frozenset(
    {
        "t.co",
        "bit.ly",
        "tinyurl.com",
        "is.gd",
        "buff.ly",
        "ow.ly",
        "ift.tt",
        "ig.me",
        "vm.tiktok.com",
        "vt.tiktok.com",
        "b23.tv",
        "youtu.be",
    }
)

#: A browser-ish UA. Several of these redirectors serve a consent page or a 403
#: to unknown clients rather than the redirect.
_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
)


def is_shortener(url: str) -> bool:
    """Whether ``url``'s host is a pure redirector."""
    host = (urlparse(url).hostname or "").lower()
    return host in SHORTENER_HOSTS or any(host.endswith(f".{h}") for h in SHORTENER_HOSTS)


def has_tracking_params(url: str, *, strip: frozenset[str] = TRACKING_PARAMS) -> bool:
    """Whether cleaning ``url`` would actually change it."""
    query = parse_qs(urlparse(url).query, keep_blank_values=True)
    return any(key in strip for key in query)


def clean_url(url: str, *, strip: frozenset[str]) -> str:
    """Repair the scheme and drop the named query parameters.

    Pure and offline. A bare host such as ``youtu.be/abc`` gets an ``https://``
    prefix, because that is what people paste.

    Args:
        url: The URL to clean.
        strip: Parameter names to remove. **Required, with no default.** A
            default would have to be either :data:`TRACKING_PARAMS` (which
            silently destroys YouTube's ``t``) or :data:`CAMPAIGN_PARAMS` (which
            silently leaves X's ``t`` in place). Both are wrong for some caller,
            and a silent wrong answer is worse than a required argument.

    Returns:
        The cleaned URL. Byte-identical to the input when nothing applied.
    """
    url = url.strip()
    if not url:
        return url
    if not url.startswith(("http://", "https://")):
        url = f"https://{url}"

    parsed = urlparse(url)
    query = parse_qs(parsed.query, keep_blank_values=True)
    if not any(key in strip for key in query):
        # Nothing to do: return the input unchanged rather than re-encoding it,
        # so a URL we do not understand cannot be mangled.
        return url

    kept = {k: v for k, v in query.items() if k not in strip}
    return urlunparse(
        (
            parsed.scheme,
            parsed.netloc,
            parsed.path,
            parsed.params,
            urlencode(kept, doseq=True),
            parsed.fragment,
        )
    )


def expand_short_url(url: str, *, timeout: float = 8.0) -> str:
    """Follow a shortener redirect and return the final URL.

    Best effort: on any failure the input is returned unchanged, because yt-dlp
    resolves these hosts itself and a failed pre-flight expansion is not a reason
    to reject an otherwise good link.

    Uses ``GET`` with ``stream=True`` rather than ``HEAD``: several of these
    redirectors answer ``HEAD`` with 405 or serve a consent page instead of the
    redirect, and not reading the body keeps the cost identical.
    """
    if not is_shortener(url):
        return url

    import requests

    try:
        response = requests.get(
            url,
            allow_redirects=True,
            timeout=timeout,
            headers={"User-Agent": _USER_AGENT},
            stream=True,
        )
    except Exception as exc:  # noqa: BLE001 - best effort, see docstring
        _logger.debug("could not expand %s: %s", url, exc)
        return url

    final = response.url or url
    # Release the connection without downloading the body.
    response.close()
    if final != url:
        _logger.debug("expanded %s -> %s", url, final)
    return final
