"""``porter doctor`` — report which capabilities are available.

Exit code is 0 when no BLOCKER-severity capability is missing, 1 otherwise, so
the command is usable as a CI/setup gate.

Unlike v0.1 this reports structured capabilities (with a remediation *key*) and
the CLI does the prose rendering, so the same data can be served over MCP.

**Everything human-facing goes to stderr.** For most commands stdout carries the
result, but doctor's machine-readable answer is its exit code, and ``--json`` is
its only stdout payload. Keeping the human view on stderr means ``--json`` can
never be corrupted by a stray status line, and the two modes cannot be mixed by
accident.
"""

from __future__ import annotations

import argparse

from porter.doctor.probes import Finding, Severity
from porter.errors import PorterError
from porter_cli import render

__all__ = ["configure", "run"]


def configure(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    """Register the ``doctor`` command."""
    parser = subparsers.add_parser(
        "doctor",
        help="Check Python, FFmpeg/libass, yt-dlp + JS runtime, and credentials.",
        description=(
            "Diagnose the environment and print installation guidance for anything "
            "missing. Required capabilities: FFmpeg with libass, and for YouTube an "
            "external JavaScript runtime (Deno recommended)."
        ),
    )
    parser.add_argument(
        "--json",
        dest="as_json",
        action="store_true",
        help="Emit the capability report as JSON instead of a formatted report.",
    )
    parser.add_argument(
        "--install-binaries",
        action="store_true",
        help="Download static ffmpeg (with libass) and Deno into ~/.porter/bin/.",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Include detection paths and versions for every capability.",
    )
    parser.set_defaults(handler=run)


def run(args: argparse.Namespace) -> int:
    """Probe capabilities and report.

    The facts come from the engine and the prose comes from
    :mod:`porter.doctor.guides`; this function only decides what to show and
    where. stdout carries the report (so it can be piped or captured), stderr
    carries the remediation guidance (so it never pollutes a ``--json`` payload).
    """
    from porter.config import resolve
    from porter.doctor import guide_for, probe_all

    config = resolve(args.config_path)

    try:
        report = probe_all(config)
    except PorterError as exc:
        render.warn(str(exc))
        return render.EXIT_ERROR

    if args.as_json:
        render.emit_json(report.to_dict())
        return render.EXIT_OK if report.ok else render.EXIT_ERROR

    render.info(render.banner("PORTER ENVIRONMENT DOCTOR"))
    render.info(f"platform: {report.platform}")
    render.info("")

    for finding in report.findings:
        render.info(_line(finding, verbose=args.verbose))
        if finding.ok:
            continue
        guide = guide_for(finding.remediation_key)
        if guide:
            # Always the full text, never gated on --verbose. A guide is shown
            # only when something failed, and "you need a JavaScript runtime"
            # without the command that installs one is not remediation. What
            # --verbose governs is the *passing* findings' detail lines.
            for line in guide.render().splitlines():
                render.info(f"    {line}")

    render.info("")
    if report.ok:
        summary = f"{len(report.findings)} capabilities checked, none blocking"
        if report.degradations:
            summary += f" ({len(report.degradations)} degraded)"
        render.info(summary)
        return render.EXIT_OK

    render.warn(
        f"{len(report.blockers)} blocking "
        f"{'issue' if len(report.blockers) == 1 else 'issues'}: "
        + ", ".join(finding.label for finding in report.blockers)
    )
    return render.EXIT_ERROR


def _line(finding: Finding, *, verbose: bool) -> str:
    """One status line.

    ASCII markers, not the check/cross symbols: under ``LANG=C`` CPython resolves
    stdout to ASCII and a non-ASCII glyph raises ``UnicodeEncodeError``, which
    would take down the very command people run when something is broken.
    """
    if finding.ok:
        mark = "OK  "
    elif finding.severity is Severity.BLOCKER:
        mark = "FAIL"
    else:
        mark = "WARN"

    line = f"[{mark}] {finding.label}"
    if finding.detail and (verbose or not finding.ok):
        line = f"{line}: {finding.detail}"
    return line
