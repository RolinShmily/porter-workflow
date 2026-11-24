"""``porter capabilities`` — what this installation can actually do.

The command is deliberately boring: one row per stage capability, with its
availability, cost and egress. It is the machine-readable answer to "can this
machine transcribe without a key?" that used to require reading ``doctor``
prose or trying a job and watching it fail.

Unavailable capabilities are still listed. Hiding them would make the catalog
look like a menu of things that work, and an agent choosing from it would have
no way to distinguish "not installed" from "not supported".
"""

from __future__ import annotations

import argparse

from porter.cli import render

__all__ = ["configure", "run"]


def configure(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = subparsers.add_parser(
        "capabilities",
        help="List the capabilities this installation can execute.",
        description=(
            "Show every stage capability with its probe result, cost and what "
            "leaves the machine. Useful before planning a job: a missing ffmpeg "
            "or ASR engine is a blocker, and a paid endpoint is never selected "
            "unless named or allowed explicitly."
        ),
    )
    parser.add_argument(
        "--available",
        action="store_true",
        help="Only list capabilities whose probe succeeded.",
    )
    parser.add_argument(
        "--stage",
        choices=("probe", "acquire", "normalize", "transcript", "translate", "write", "render"),
        help="Only list capabilities for one stage.",
    )
    parser.add_argument(
        "--json",
        dest="as_json",
        action="store_true",
        help="Emit the catalog as JSON.",
    )
    parser.set_defaults(handler=run)


def run(args: argparse.Namespace) -> int:
    from porter.core.config import resolve
    from porter.core.context import RunContext
    from porter.core.models.request import JobOptions
    from porter.core.workflow.capabilities import capability_catalog
    from porter.core.workflow.model import Stage

    config = resolve(args.config_path)
    ctx = RunContext(
        job_id="capabilities",
        options=JobOptions(output_dir=config.output_dir),
        config=config,
    )
    catalog = capability_catalog(ctx)
    if args.stage:
        wanted = Stage(args.stage)
        catalog = tuple(item for item in catalog if item.stage is wanted)
    if args.available:
        catalog = tuple(item for item in catalog if item.available)

    payload = [item.model_dump(mode="json") for item in catalog]
    if args.as_json:
        render.emit_json(payload)
        return render.EXIT_OK

    render.info(render.banner("PORTER CAPABILITIES"))
    for item in catalog:
        state = "available" if item.available else "MISSING"
        egress = f" egress={item.egress.value}" if item.egress.value != "none" else ""
        download = f" download={item.download_mb}MB" if item.download_mb else ""
        requires = f" requires={','.join(item.requires)}" if item.requires else ""
        render.value(
            f"{item.stage.value:<10} {item.id:<26} {state:<9} "
            f"cost={item.cost.value}{egress}{download}{requires}"
        )
    return render.EXIT_OK
