"""Remediation text, keyed by the ``remediation_key`` a finding carries.

This module exists so that :mod:`porter.doctor.probes` can be free of prose. The
split is not stylistic:

* The **fact** model has to be stable and machine-readable, because the MCP
  frontend answers questions over a protocol with no terminal.
* The **text** has to be free to change, because installation instructions
  change constantly and one of them (ffmpeg on Windows) has a multi-paragraph
  answer that no JSON schema wants to carry.

So the dependency points one way. A finding names a key; the CLI looks up text.
A key with no guide is not an error: it degrades to the finding's ``detail``,
which is why adding a probe cannot break the renderer.

**The text is deliberately ASCII-only.** These strings are printed by the CLI and
shipped in JSON. Under ``LANG=C`` CPython resolves stdout to ASCII, so a single
em-dash raises ``UnicodeEncodeError`` and takes the whole ``porter doctor``
command down with it. A test enforces this.

v0.1 had this text inside ``CheckResult.guide`` as a single blob per check, with
a separate hand-written ``get_windows_*_guide`` / ``get_linux_*_guide`` pair per
platform. Here the alternatives live together under one key, because "install
ffmpeg" and "ffmpeg is not in PATH" are the same problem on two platforms and
reading both is cheaper than maintaining two call sites.
"""

from __future__ import annotations

import platform
from dataclasses import dataclass

__all__ = ["GUIDES", "Remediation", "guide_for", "keys"]


@dataclass(frozen=True)
class Remediation:
    """What to actually do, in the order to do it.

    ``steps`` are command lines or one-line instructions. ``note`` is for the
    thing people get wrong — the reason this text exists rather than a link.
    """

    summary: str
    steps: tuple[str, ...] = ()
    note: str | None = None
    url: str | None = None

    def render(self, *, verbose: bool = True) -> str:
        lines = [self.summary]
        if verbose:
            lines.extend(f"  {step}" for step in self.steps)
            if self.note:
                lines.append(f"  note: {self.note}")
            if self.url:
                lines.append(f"  {self.url}")
        return "\n".join(lines)


_FFMPEG_COMMON_NOTE = (
    "porter needs the 'ass' filter, which requires a build with --enable-libass. "
    "Distribution packages and the gyan.dev/BtbN Windows builds all include it; "
    "a hand-compiled minimal build often does not."
)

GUIDES: dict[str, Remediation] = {
    "python_version": Remediation(
        summary="porter requires Python 3.10 or newer.",
        steps=(
            "python3 --version",
            "Use pyenv, uv, or your distribution's python3.12 package to get a newer interpreter.",
        ),
        note="The engine uses match statements and PEP 604 unions throughout.",
    ),
    "ffmpeg": Remediation(
        summary="ffmpeg and ffprobe must both be installed and on PATH.",
        steps=(
            "Debian/Ubuntu:  sudo apt install ffmpeg",
            "Fedora:         sudo dnf install ffmpeg",
            "Arch:           sudo pacman -S ffmpeg",
            "macOS:          brew install ffmpeg",
            "Windows:        winget install Gyan.FFmpeg   (or scoop install ffmpeg)",
            "Verify:         ffmpeg -version && ffprobe -version",
        ),
        note=_FFMPEG_COMMON_NOTE,
        url="https://ffmpeg.org/download.html",
    ),
    "ffmpeg_libass": Remediation(
        summary="This ffmpeg build has no 'ass' filter, so hardsubs cannot be burned in.",
        steps=(
            "ffmpeg -hide_banner -filters | grep -w ass     # confirm it is missing",
            "Replace the build with one that includes libass (see below).",
        ),
        note=(
            "Porter still produces .srt and .ass files; only the burned-in MP4 is "
            "affected. On Debian/Ubuntu install 'libass9'; on a source build add "
            "--enable-libass."
        ),
    ),
    "js_runtime": Remediation(
        summary="yt-dlp needs an external JavaScript runtime for full YouTube extraction.",
        steps=(
            "Deno (recommended):  curl -fsSL https://deno.land/install.sh | sh",
            "Node (alternative):  sudo apt install nodejs   # must be >= 20",
            "Verify:              deno --version",
        ),
        note=(
            "Without one, extraction does not fail: it returns fewer formats, so "
            "the problem only shows up as an unexplained 'format not available'."
        ),
        url="https://github.com/yt-dlp/yt-dlp/wiki/EJS",
    ),
    "yt_dlp": Remediation(
        summary="The yt_dlp Python module is not importable.",
        steps=(
            "uv pip install yt-dlp",
            "or: pip install yt-dlp",
        ),
        note=(
            "porter drives yt-dlp as a library rather than as a subprocess, because "
            "a subprocess writing progress to stdout would corrupt the MCP "
            "frontend's protocol stream."
        ),
    ),
    "subtitle_font": Remediation(
        summary="The configured Chinese subtitle font is not installed.",
        steps=(
            "List what fontconfig can see:  fc-list : family | sort -u | grep -i yahei",
            "Ubuntu/Debian:  sudo apt install fonts-noto-cjk",
            "Or point porter at a font you have, in config.json:  style.zh_font = \"Noto Sans CJK SC\"",
        ),
        note=(
            "A missing CJK font does not fail. It renders every Chinese character as "
            "an empty box, so the job completes and the video is unusable."
        ),
    ),
    "output_dir": Remediation(
        summary="The configured output directory cannot be written to.",
        steps=(
            "Check the path exists and is writable:  ls -ld <output_dir>",
            "Override it:  porter run URL -o /some/writable/path",
            "Or set PORTER_OUTPUT_DIR in the environment.",
        ),
        note=(
            "Under WSL2 a Windows path such as /mnt/c/... can be read-only or "
            "reject certain filenames even when the mode bits look permissive."
        ),
    ),
}


def guide_for(key: str | None) -> Remediation | None:
    """Look up remediation text. ``None`` in, ``None`` out."""
    if not key:
        return None
    return GUIDES.get(key)


def keys() -> list[str]:
    """Every known key.

    Exposed so a test can assert that every ``remediation_key`` a probe can emit
    has text behind it. A key with no guide is safe at runtime but is a silent
    hole in the operator experience, which is the kind of thing that should fail
    a test rather than a user.
    """
    return sorted(GUIDES)


def platform_hint() -> str:
    """A short platform line, so rendered advice can be narrowed."""
    return f"{platform.system()} {platform.release()}"
