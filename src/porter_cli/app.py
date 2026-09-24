"""Argument parser and dispatch for the ``porter`` CLI.

Command tree::

    porter <URL>                      # shorthand for `porter run <URL>`
    porter run <URL> [options]
    porter inspect <URL>
    porter doctor
    porter config {list,get,set,path}
    porter jobs {list,status,cancel}

The bare-URL shorthand is preserved from v0.1 so existing scripts keep working;
:func:`_inject_run` rewrites it before argparse sees the arguments.
"""

from __future__ import annotations

import argparse
from collections.abc import Sequence

from porter import __version__
from porter.logging import configure as configure_logging
from porter_cli import render
from porter_cli.commands import config as config_cmd
from porter_cli.commands import doctor as doctor_cmd
from porter_cli.commands import inspect as inspect_cmd
from porter_cli.commands import jobs as jobs_cmd
from porter_cli.commands import plan as plan_cmd
from porter_cli.commands import run as run_cmd

__all__ = ["SUBCOMMANDS", "build_parser", "dispatch", "main"]

SUBCOMMANDS = ("run", "inspect", "plan", "doctor", "config", "jobs")

#: Global options that consume the following token, so the bare-URL detector
#: does not mistake that token for a subcommand.
_GLOBAL_VALUE_FLAGS = frozenset({"--config", "--log-level"})


def build_parser() -> argparse.ArgumentParser:
    """Construct the full argument parser."""
    parser = argparse.ArgumentParser(
        prog="porter",
        description=(
            "Porter Workflow — automated video localization: download, "
            "transcribe, translate, and burn bilingual/Chinese hardsubs."
        ),
        epilog="Run 'porter doctor' to check system dependencies.",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )
    parser.add_argument(
        "--config",
        dest="config_path",
        metavar="PATH",
        help="Explicit configuration file (JSON or TOML). Overrides $PORTER_CONFIG.",
    )
    parser.add_argument(
        "--log-level",
        metavar="LEVEL",
        help="Log verbosity: debug, info, warning, error. Defaults to $PORTER_LOG_LEVEL.",
    )

    subparsers = parser.add_subparsers(dest="command", metavar="<command>")
    for module in (run_cmd, inspect_cmd, plan_cmd, doctor_cmd, config_cmd, jobs_cmd):
        module.configure(subparsers)
    return parser


def _inject_run(argv: Sequence[str]) -> list[str]:
    """Rewrite ``porter <URL>`` into ``porter run <URL>``.

    Skips leading global flags (and their values) so that
    ``porter --config c.json <URL>`` still works.
    """
    index = 0
    while index < len(argv):
        token = argv[index]
        if token in _GLOBAL_VALUE_FLAGS:
            index += 2
            continue
        if token.startswith("-"):
            index += 1
            continue
        break
    else:
        return list(argv)

    if argv[index] in SUBCOMMANDS:
        return list(argv)
    return [*argv[:index], "run", *argv[index:]]


def dispatch(args: argparse.Namespace) -> int:
    """Run the handler selected by ``args``."""
    handler = getattr(args, "handler", None)
    if handler is None:
        build_parser().print_help()
        return render.EXIT_MISUSE
    result: int = handler(args)
    return result


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point (also the ``porter`` console script)."""
    import sys

    # Before anything can print. sys.stdout defaults to errors="strict", so on a
    # non-UTF-8 locale any CJK byte in a video title raised UnicodeEncodeError and
    # aborted the command -- see render.make_stdout_safe.
    render.make_stdout_safe()

    try:
        raw = list(sys.argv[1:] if argv is None else argv)
        parser = build_parser()
        args = parser.parse_args(_inject_run(raw))

        configure_logging(args.log_level)
        return dispatch(args)
    except KeyboardInterrupt:
        # Ctrl+C. Without this the user gets a raw traceback ending in whatever
        # library call happened to be running -- socket, ssl, ffmpeg's pipe --
        # which tells them nothing and reads as a crash. Commands that own a job
        # record it themselves (see commands/run.py); this is the net for the rest
        # (inspect, plan, doctor, config, jobs) and for an interrupt during
        # argument parsing.
        render.warn("interrupted")
        return render.EXIT_CANCELLED
