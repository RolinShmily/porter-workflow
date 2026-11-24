"""``porter probe`` — inspect a source without planning or downloading it.

``plan`` answers "what would run"; ``probe`` answers "what is there". Separating
them is what lets an agent show a user the facts before a strategy exists, and it
means the probe can be used on inputs no workflow supports yet (an audio file, a
subtitle) without inventing a plan for them.
"""

from __future__ import annotations

import argparse

from porter.cli import render

__all__ = ["configure", "run"]


def configure(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = subparsers.add_parser(
        "probe",
        help="Describe a source: kind, media facts and subtitle tracks.",
        description=(
            "Inspect a URL, local video, audio file or subtitle file and report "
            "what porter can see. For a URL this fetches metadata but downloads "
            "no media. Exit 1 when the source itself is unusable."
        ),
    )
    parser.add_argument("source", metavar="SOURCE", help="URL, local media path, or subtitle file.")
    parser.add_argument("--cookies", metavar="FILE", help="Netscape cookies.txt for gated links.")
    parser.add_argument(
        "--cookies-from-browser",
        metavar="BROWSER",
        help="Read cookies from a browser profile.",
    )
    parser.add_argument(
        "--json",
        dest="as_json",
        action="store_true",
        help="Emit the probe as JSON.",
    )
    parser.set_defaults(handler=run)


def run(args: argparse.Namespace) -> int:
    from porter.core.config import resolve
    from porter.core.context import RunContext
    from porter.core.errors import PorterError
    from porter.core.models.request import JobOptions
    from porter.core.workflow.probe import probe_source

    config = resolve(args.config_path)
    options = JobOptions(
        output_dir=config.output_dir,
        cookies_file=args.cookies,
        cookies_browser=args.cookies_from_browser,
    )
    ctx = RunContext(job_id="probe", options=options, config=config)

    try:
        probe = probe_source(args.source, ctx)
    except PorterError as exc:
        render.warn(exc.message)
        return render.EXIT_ERROR

    if args.as_json:
        render.emit_json(probe.model_dump(mode="json"))
    else:
        render.info(render.banner("PORTER SOURCE PROBE"))
        render.value(_format_probe(probe))

    if any(issue.severity == "blocker" for issue in probe.warnings):
        return render.EXIT_ERROR
    return render.EXIT_OK


def _format_probe(probe: object) -> str:
    """Render the decision-relevant fields, not every field.

    ``--json`` exists for the full structure; a human reading the table wants to
    know what the resolver will decide and what is wrong.
    """
    from porter.core.workflow.model import SourceProbe

    if not isinstance(probe, SourceProbe):  # pragma: no cover - caller passes the model
        raise TypeError(f"expected SourceProbe, got {type(probe).__name__}")

    lines = [
        f"Source   : {probe.source}",
        f"Kind     : {probe.kind.value}",
    ]
    if probe.platform:
        lines.append(f"Platform : {probe.platform}")
    if probe.title:
        lines.append(f"Title    : {probe.title}")
    if probe.duration_seconds:
        lines.append(f"Duration : {probe.duration_seconds:.1f}s")
    if probe.width and probe.height:
        orientation = "vertical" if probe.is_vertical else "horizontal"
        lines.append(f"Media    : {probe.width}x{probe.height} ({orientation})")
    streams = [
        name
        for name, present in (("video", probe.has_video), ("audio", probe.has_audio))
        if present
    ]
    lines.append(f"Streams  : {', '.join(streams) if streams else 'none measured'}")
    if probe.tracks:
        lines.append(
            "Tracks   : "
            + ", ".join(
                f"{track.role}={track.lang}"
                + (" (auto)" if track.auto_generated else "")
                + (" verified" if track.verified else " listed")
                for track in probe.tracks
            )
        )
    else:
        lines.append("Tracks   : none")
    for issue in probe.warnings:
        label = "blocker" if issue.severity == "blocker" else "note"
        lines.append(f"{label:<9}: {issue.message}")
    return "\n".join(lines)
