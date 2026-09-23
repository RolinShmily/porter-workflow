"""``porter inspect`` — fast pre-flight probe on a link.

Answers "is this URL usable, and what will happen to it?" in a few seconds,
without downloading any media. Useful both to a human before committing to a long
job and to an agent deciding whether to start one.
"""

from __future__ import annotations

import argparse

from porter.errors import PorterError
from porter_cli import render

__all__ = ["configure", "run"]


def configure(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    """Register the ``inspect`` command."""
    parser = subparsers.add_parser(
        "inspect",
        help="Probe a link without downloading (platform, title, duration, aspect).",
        description=(
            "Resolve short links, strip tracking parameters, and report platform, "
            "duration, resolution, and available subtitle tracks."
        ),
    )
    parser.add_argument("url", help="Video URL to probe.")
    parser.add_argument(
        "--cookies",
        metavar="FILE",
        help="Netscape cookies.txt, for links that require authentication.",
    )
    parser.add_argument(
        "--cookies-from-browser",
        metavar="BROWSER",
        help="Read cookies from a browser profile.",
    )
    parser.add_argument(
        "--json",
        dest="as_json",
        action="store_true",
        help="Emit the inspection result as JSON instead of a formatted report.",
    )
    parser.set_defaults(handler=run)


def run(args: argparse.Namespace) -> int:
    """Probe the link and report the result.

    Exit codes follow the same contract as every other command: 0 for a usable
    link, 1 for a link this build cannot use. A *bad* link is still a successful
    inspection — the command did its job — but callers script against "did this
    work", so an unusable URL exits non-zero.
    """
    from porter.config import resolve
    from porter.context import RunContext
    from porter.models.request import JobOptions
    from porter.platforms.inspector import inspect_url

    config = resolve(args.config_path)
    options = JobOptions(
        output_dir=config.output_dir,
        cookies_file=args.cookies,
        cookies_browser=args.cookies_from_browser,
    )
    ctx = RunContext(job_id="inspect", options=options, config=config)

    try:
        result = inspect_url(args.url, ctx)
    except PorterError as exc:
        render.warn(str(exc))
        return render.EXIT_ERROR

    if args.as_json:
        render.emit_json(result.to_dict())
    else:
        # The report is the result, so it goes to stdout where it can be piped;
        # the human headings go to stderr.
        render.info(render.banner("PORTER LINK PRE-FLIGHT PROBE"))
        render.value(result.format_summary())

    if not result.is_valid:
        render.warn(result.error_message or "this link is not usable")
        return render.EXIT_ERROR
    return render.EXIT_OK
