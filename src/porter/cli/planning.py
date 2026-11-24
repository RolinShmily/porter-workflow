"""Shared command-line surface for ``plan`` and ``run``.

Both commands accept the same planning choices by design: a run resolves a plan
with the flags it was given, and a plan can be executed by a run. Keeping the
arguments in one place means ``porter run --transcript whisper-api`` and
``porter plan --transcript whisper-api`` cannot drift into different meanings.
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import TYPE_CHECKING

from porter.core.models.request import BurnMode

if TYPE_CHECKING:
    from porter.core.config import PorterConfig
    from porter.core.workflow.model import Task
    from porter.core.workflow.resolver import PlanOverrides

__all__ = ["add_arguments", "burn_mode", "overrides_from_args", "task_from_args"]


def burn_mode(text: str) -> BurnMode:
    """Accept ``zh-only`` and ``zh_only``.

    The old parser advertised hyphens and passed the hyphenated string straight
    to ``BurnMode``, whose values use underscores, so ``--burn zh-only`` raised
    ``ValueError``. Accepting both spellings fixes the documented form without
    breaking the enum's own.
    """
    try:
        return BurnMode(text.strip().lower().replace("-", "_"))
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            f"invalid burn mode {text!r}; choose from "
            + ", ".join(mode.value for mode in BurnMode)
        ) from exc


def add_arguments(parser: argparse.ArgumentParser) -> None:
    """Add the flags that shape a plan to ``parser``."""
    parser.add_argument(
        "-o",
        "--output-dir",
        metavar="DIR",
        help="Task directory root. Defaults to the configured output_dir.",
    )
    parser.add_argument(
        "--task",
        choices=("probe", "transcribe", "subtitle", "release", "convert", "audio"),
        help="Stop after this result. Defaults to the full release.",
    )
    parser.add_argument(
        "--burn",
        type=burn_mode,
        metavar="MODE",
        help="Release videos to plan: dual, zh_only, bilingual_only, skip.",
    )
    parser.add_argument(
        "--target-lang",
        metavar="LANG",
        help="Target subtitle language. Defaults to zh-Hans.",
    )
    parser.add_argument(
        "--transcript",
        "--asr-engine",
        dest="transcript",
        metavar="CAPABILITY",
        help=(
            "Select exactly this transcript capability: whisper-local, "
            "whisper-api, platform-track, supplied. No fallback is planned."
        ),
    )
    parser.add_argument(
        "--translate",
        "--translator",
        dest="translate",
        metavar="CAPABILITY",
        help="Select exactly this translation capability: bing, google, mymemory, llm.",
    )
    parser.add_argument(
        "--render",
        metavar="CAPABILITY",
        help="Select exactly this render capability. Currently ffmpeg-libass.",
    )
    parser.add_argument(
        "--subtitle-file",
        metavar="FILE",
        help="Use this .srt/.vtt as the source track; plans the subtitled workflow.",
    )
    parser.add_argument(
        "--llm-model",
        metavar="MODEL",
        help="LLM model for an explicitly selected translate.llm capability.",
    )
    parser.add_argument("--cookies", metavar="FILE", help="Netscape cookies.txt for gated links.")
    parser.add_argument(
        "--cookies-from-browser",
        metavar="BROWSER",
        help="Read cookies from a browser profile.",
    )
    parser.add_argument(
        "--no-denoise",
        action="store_true",
        default=None,
        help="Plan without the ASR vocal-enhancement pass.",
    )
    parser.add_argument(
        "--allow-paid",
        action="store_true",
        help=(
            "Permit a paid capability when no free peer is available. A paid "
            "capability named explicitly is always permitted."
        ),
    )
    parser.add_argument(
        "--force-translate",
        action="store_true",
        help="Translate even when the source already offers a target-language track.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Ignore cached stage results when the plan executes.",
    )
    parser.add_argument(
        "--only-phase",
        choices=("prepare", "transcribe", "translate", "burn"),
        help=(
            "Deprecated; map onto --task. prepare=audio, transcribe=transcribe, "
            "translate=subtitle, burn=release."
        ),
    )


def task_from_args(args: argparse.Namespace) -> Task:
    from porter.core.workflow.model import Task

    if args.task:
        return Task(args.task)
    if args.only_phase:
        return {
            "prepare": Task.AUDIO,
            "transcribe": Task.TRANSCRIBE,
            "translate": Task.SUBTITLE,
            "burn": Task.RELEASE,
        }[args.only_phase]
    return Task.RELEASE


def overrides_from_args(args: argparse.Namespace, config: PorterConfig) -> PlanOverrides:
    """Build :class:`PlanOverrides` from parsed arguments and resolved config."""
    from porter.core.workflow.resolver import PlanOverrides

    return PlanOverrides(
        task=task_from_args(args),
        output_dir=Path(args.output_dir) if args.output_dir else Path(config.output_dir),
        target_lang=args.target_lang or "zh-Hans",
        burn=args.burn or BurnMode.DUAL,
        transcript=args.transcript,
        translate=args.translate,
        render=args.render,
        subtitle_file=Path(args.subtitle_file) if args.subtitle_file else None,
        cookies_file=Path(args.cookies) if args.cookies else None,
        cookies_browser=args.cookies_from_browser,
        llm_model=args.llm_model,
        force=args.force,
        audio_denoise=config.asr.audio_denoise if args.no_denoise is None else False,
        allow_paid=args.allow_paid,
        force_translate=args.force_translate,
    )
