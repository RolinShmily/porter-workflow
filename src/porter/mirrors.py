"""Mirrors for the two downloads that are slow or unreachable from China.

Two things stand between a Chinese user and a working install, and both default
to an endpoint that is painful from there:

* the Python packages, resolved by ``uv`` (or ``pip``) from PyPI;
* the faster-whisper weights, fetched from Hugging Face.

Both have well-run mirrors: USTC's PyPI index, and ModelScope, whose
``pengzhendong/faster-whisper-*`` repositories are copies of the official
weights -- verified, not assumed: its ``config.json`` has the same SHA-256 as
``Systran/faster-whisper-small``'s, and its ``model.bin`` is the same
CTranslate2 file faster-whisper loads locally.

Selection
---------

1. ``PORTER_MIRROR`` decides, when set: ``cn`` forces the mirrors on, ``off``
   forces them off. An unset or unrecognised value falls through.
2. Otherwise the environment is inspected, because needing to know this exists
   is exactly the burden the mirrors should remove.
3. A user's own choice always wins over any default set here -- ``UV_DEFAULT_INDEX``
   or ``HF_ENDPOINT`` in the environment is left alone.

What this module does *not* do is decide that a mirror is *reachable*. Preferring
a mirror is a default, not a promise; every caller keeps an official fallback,
because a university mirror being down must not be worse than not using it.
"""

from __future__ import annotations

import locale
import os
from datetime import datetime, timedelta

__all__ = [
    "ENV_OVERRIDE",
    "KNOWN_WHISPER_SIZES",
    "MODELSCOPE_ENDPOINT",
    "USTC_PYPI_INDEX",
    "detected",
    "forced",
    "modelscope_whisper_repo",
    "use_china_mirrors",
]

#: ``cn``/``off`` force the decision; anything else (including unset) auto-detects.
ENV_OVERRIDE = "PORTER_MIRROR"

#: USTC's PyPI index. A public service, no key, and fast from mainland China.
USTC_PYPI_INDEX = "https://mirrors.ustc.edu.cn/pypi/simple"

#: ModelScope's API host. Files come from the ordinary repo endpoint under it, so
#: no ``modelscope`` package is needed -- ``requests`` is already a dependency.
MODELSCOPE_ENDPOINT = "https://modelscope.cn"

#: ModelScope mirrors the official CTranslate2 conversions under this owner.
#: ``distil-*`` models are absent there, which is why this is a lookup with a
#: known answer rather than a template applied to anything.
_MODELSCOPE_REPO = "pengzhendong/faster-whisper-{model}"

#: Sizes with a ModelScope mirror. A path or a custom HF repo id is absent here
#: on purpose: those are the caller's own model, not something we can mirror.
KNOWN_WHISPER_SIZES = frozenset(
    {"tiny", "base", "small", "medium", "large-v2", "large-v3"}
)

_ON_VALUES = frozenset({"cn", "china", "1", "true", "yes", "on"})
_OFF_VALUES = frozenset({"off", "0", "false", "no", "none", "intl"})

#: Substrings that mark a China timezone. Compared against ``TZ`` and, on
#: Windows, a localised ``tzname()`` -- which reads "中国标准时间" on a Chinese
#: machine, so an English-only marker list would miss exactly the users this
#: exists for.
_TZ_MARKERS = (
    "china",
    "chinese",
    "shanghai",
    "chongqing",
    "harbin",
    "urumqi",
    "beijing",
    "prc",
    "中国",
)


def forced() -> bool | None:
    """``True``/``False`` when ``PORTER_MIRROR`` says so, else ``None``.

    ``None`` means "no opinion", which is what lets auto-detection run. An
    unrecognised value is treated the same as unset rather than as an error: a
    typo in an environment variable should not stop a transcription.
    """
    raw = os.environ.get(ENV_OVERRIDE, "").strip().lower()
    if raw in _ON_VALUES:
        return True
    if raw in _OFF_VALUES:
        return False
    return None


def _local_now() -> datetime:
    """The current time in this machine's own timezone.

    A function rather than an inline call so the timezone branches below can be
    exercised from a test without moving the machine.
    """
    return datetime.now().astimezone()


def _declared_tz() -> bool | None:
    """``True``/``False`` when ``TZ`` names a zone, else ``None``.

    An explicit ``TZ`` is believed in *both* directions, and it ends the
    question rather than only being one vote: someone who set
    ``America/New_York`` does not want a Chinese mirror even if the machine is
    in Shanghai, and the locale must not be consulted to second-guess them.
    Letting the two signals contradict each other quietly made the locale win.
    """
    tz = os.environ.get("TZ", "").strip().lower()
    if not tz:
        return None
    return any(marker in tz for marker in _TZ_MARKERS)


def _os_timezone_says_china() -> bool:
    """Judge by what the OS reports, for when ``TZ`` says nothing."""
    try:
        local = _local_now()
    except (OSError, ValueError, OverflowError):  # a broken tz database
        return False

    name = (local.tzname() or "").lower()
    if any(marker in name for marker in _TZ_MARKERS):
        return True

    # The offset is the signal that survives localisation and a missing TZ
    # database. UTC+8 is Greater China plus Singapore and Perth -- all of them
    # geographically closer to these mirrors than to the defaults.
    try:
        offset = local.utcoffset()
    except (OSError, ValueError, OverflowError):
        return False
    return offset == timedelta(hours=8)


def _locale_says_china() -> bool:
    """Judge by the system UI language.

    Both spellings are checked because the two platforms disagree:
    ``locale.getlocale()`` yields ``('zh_CN', ...)`` on POSIX but
    ``('Chinese (Simplified)_China', '936')`` on Windows, where a ``zh_`` prefix
    test would silently never match.
    """
    try:
        language, _encoding = locale.getlocale()
    except (locale.Error, ValueError, TypeError):
        return False
    if not language:
        return False
    lowered = language.lower()
    return lowered.startswith("zh") or "china" in lowered


def detected() -> bool:
    """Whether this machine looks like it is in China. Ignores the override."""
    declared = _declared_tz()
    if declared is not None:
        return declared
    return _os_timezone_says_china() or _locale_says_china()


def use_china_mirrors() -> bool:
    """Whether to prefer the mirrors, honouring ``PORTER_MIRROR`` first."""
    explicit = forced()
    if explicit is not None:
        return explicit
    return detected()


def modelscope_whisper_repo(model: str) -> str | None:
    """The ModelScope repo mirroring ``model``, or ``None`` if there is none.

    ``None`` is the answer for a local directory or a custom Hugging Face repo
    id as much as for an unmirrored size like ``distil-large-v3``. The caller
    then keeps its original, official path.
    """
    name = model.strip().lower()
    if name not in KNOWN_WHISPER_SIZES:
        return None
    return _MODELSCOPE_REPO.format(model=name)
