"""Resources and a prompt: the reference material an agent reads up front.

Every *list* in this module is read out of the engine at call time -- the
stages, the workflows, the capabilities and the platforms -- because a
hand-maintained list is wrong the moment someone adds an entry. The prose about
*why* the plan is shaped this way is authored; there is nothing to derive it
from.

Deliberately absent: live availability. ``porter://docs/architecture`` reports
what capabilities *exist* and how the plan chooses among them; it does not
probe. Probing is ``porter_doctor``'s and ``porter_capabilities``' job, and a
resource read that quietly opened network connections or ran ffmpeg would be a
side effect in something a client fetches to render documentation.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from porter.core.config import resolve
from porter.core.errors import PorterError
from porter.mcp.stdout_guard import protect

if TYPE_CHECKING:
    from fastmcp import FastMCP

__all__ = ["ARCHITECTURE_URI", "CONFIG_URI", "PROMPT_NAME", "register"]

ARCHITECTURE_URI = "porter://docs/architecture"
CONFIG_URI = "porter://config"

#: The contract spelled this ``porter://prompts/localize-video``. It is registered as an
#: MCP *prompt* named ``localize-video`` instead of a resource with that URI:
#: prompts are the primitive clients surface as reusable commands, which is
#: exactly what "guide the agent through the loop" means. A resource would have
#: to be fetched and re-read by hand.
PROMPT_NAME = "localize-video"

_WORKFLOW = """\
# Localising a video with porter

Follow these steps in order. Each is cheaper than the one after it, so a bad
input is rejected before anything expensive runs.

## 1. `porter_doctor` -- is this machine capable?

Run it before blaming the source. It reports ffmpeg/libass, a JS runtime for
yt-dlp, the Chinese font, the encoder, and whether any speech-to-text or
translation route exists. `guidance` carries step-by-step fixes for failures.
A `degraded` finding does not block; a `blocker` does.

## 2. `porter_capabilities` -- what can each stage actually run?

One row per peer capability, with `available`, `cost` and `egress`. Use it to
choose between peers (local Whisper vs the paid Whisper API) and to explain a
blocked plan. Unavailable entries are listed too, so "not installed" is
distinguishable from "not supported". `egress=audio` means audio leaves the
machine; say so before using it.

## 3. `porter_probe` -- what is this input?

Returns `kind` (url / local_video / audio / subtitle), platform, title, media
facts and the subtitle tracks the platform lists. Tracks are **requested, not guaranteed**:
when a source lists tracks the probe adds a `TRACKS_UNVERIFIED` warning, and a
caption fetch can still fail afterwards (rate limits, HTTP 429). An unusable
source is `ok: true` with a warning -- do not retry those.

## 4. `porter_plan` -- what will actually run?

Returns an executable `WorkflowPlan`:

- `workflow`: url.subtitled / url.asr / local.subtitled / local.asr /
  subtitle.only / audio.asr
- `stages`: exactly one capability per stage. There is no candidate list and
  no fallback chain.
- `feasible` and `issues`: `blocker` issues stop the job; `warning` issues do
  not. Read `fix` and `alternatives`; they are the next planning move.
- `runtime`: output directory, target language, burn setting.

A named capability that is unavailable blocks the plan. `allow_paid` is the only
way a paid capability is chosen without being named, and an API key in the
environment is not consent by itself.

## 5. Confirm with the user before starting

A job can download hundreds of megabytes and run for tens of minutes. Report
the workflow, stages, cost and egress, and get agreement. If `feasible` is
false, report the blocking issue and its fix instead of starting.

## 6. `porter_job_start(plan=...)` -- start it and return immediately

Pass the plan unchanged. Planning arguments are rejected alongside a plan,
because the plan already fixed them. Starting returns a `job_id` in well under
a second.

Do not wait. Long work over MCP must be polled, not blocked on: a tool call has
a timeout of a minute or two, while a burn takes tens of minutes.

## 7. `porter_job_status` -- poll until terminal

Poll until the state is `done`, `failed` or `cancelled`. It is cheap and safe to
call repeatedly, and it also sees jobs started by a `porter` CLI in another
terminal.

## 8. `porter_job_result` -- collect the stage results

Owned jobs return the full `WorkflowResult`: per-stage capability, status,
outputs, and on failure an `error` with `retryable`, `alternatives` and
`suggested_workflow`. Jobs started by another process return the flat registry
record and say so.

## When a stage fails

There is no runtime fallback. If `retryable` is true, re-running the same plan
is reasonable. Otherwise choose one of the reported `alternatives`, or follow
`suggested_workflow`, and call `porter_plan` again with that choice. Do not
expect the executor to switch engines on its own.

## Checking the output

Read the artifact paths and confirm the files exist and are non-trivial in size.
A release video that is unexpectedly small usually means the encode failed, not
that the video was short.

## Other tools

- **`porter_config`** -- resolved settings, secrets masked. Read-only; keys are
  configured with the `porter` CLI, never over MCP.
- **`porter_translate` / `porter_transcribe` / `porter_burn`** -- artifact-level
  operations on files you already have, not pipeline runs. A URL belongs in a
  job.
"""


def _platform_rows() -> list[str]:
    """The platform table, read from the registry rather than restated."""
    from porter.core.platforms import registry

    rows = ["| platform | name | platform subtitle track |", "| --- | --- | --- |"]
    for handler in registry().handlers():
        spec = getattr(handler, "spec", None)
        if spec is None:  # pragma: no cover - every registered handler has one
            continue
        track = "yes" if spec.subtitles.remote else "no"
        if spec.subtitles.prefer_existing_chinese:
            track += " (Chinese reused when present)"
        rows.append(f"| `{spec.name}` | {spec.display_name} | {track} |")
    return rows


def _stage_rows() -> list[str]:
    """The stage table, derived from the enum and the capability ids."""
    from porter.core.workflow.capabilities import CAPABILITY_IDS
    from porter.core.workflow.model import Stage

    purposes = {
        Stage.PROBE: "read source facts",
        Stage.ACQUIRE: "bring the media in",
        Stage.NORMALIZE: "master video/audio",
        Stage.TRANSCRIPT: "source-language transcript",
        Stage.TRANSLATE: "target-language subtitles",
        Stage.WRITE: "write SRT/ASS",
        Stage.RENDER: "burn hardsubs",
    }
    rows = ["| stage | does | capabilities |", "| --- | --- | --- |"]
    for stage in Stage:
        ids = [capability for capability in CAPABILITY_IDS if capability.startswith(f"{stage.value}.")]
        rows.append(
            f"| `{stage.value}` | {purposes[stage]} | "
            + ", ".join(f"`{capability}`" for capability in ids)
            + " |"
        )
    return rows


def _workflow_rows() -> list[str]:
    """The workflow names, derived from the enum."""
    from porter.core.workflow.model import WorkflowId

    rows = ["| workflow | source | transcript route |", "| --- | --- | --- |"]
    for workflow in WorkflowId:
        source = "URL" if workflow.value.startswith("url.") else "local"
        route = {
            "url.subtitled": "platform track",
            "url.asr": "speech recognition",
            "local.subtitled": "supplied/platform track",
            "local.asr": "speech recognition",
            "subtitle.only": "supplied subtitle",
            "audio.asr": "speech recognition",
        }[workflow.value]
        rows.append(f"| `{workflow.value}` | {source} | {route} |")
    return rows


def _render_architecture() -> str:
    """Render the architecture document."""
    lines = [
        "# porter architecture",
        "",
        "porter localises a video (or processes a local subtitle/audio file) by",
        "resolving one workflow plan and executing exactly that plan. The plan is",
        "the contract: every frontend builds the same model and the executor obeys",
        "it.",
        "",
        "## Stages",
        "",
        "A stage is one unit of work. A plan contains **exactly one capability per",
        "stage**; capabilities are peers, not a fallback chain. Whether a stage is",
        "present is decided while planning, so execution has no conditional",
        "branches.",
        "",
        *_stage_rows(),
        "",
        "## Workflows",
        "",
        "The resolver chooses a workflow from the observed source facts, not from",
        "the platform name.",
        "",
        *_workflow_rows(),
        "",
        "## Capability policy",
        "",
        "- `cost=free` needs nothing external beyond the local package.",
        "- `cost=network` is free but requires a connection.",
        "- `cost=paid` is excluded by default. It is selected only when named",
        "  explicitly, or when the plan was allowed to spend (`allow_paid`).",
        "- `egress=audio` uploads audio to a third party; `egress=text` sends",
        "  subtitle text. Both are reported before the job starts.",
        "",
        "Use `porter_capabilities` for live availability, cost and egress. This",
        "document does not probe -- opening sockets or running ffmpeg to render",
        "documentation would be a side effect a resource read must not have.",
        "",
        "## Platforms",
        "",
        "Only these hosts are recognised; anything else is rejected before any work",
        "starts. \"Platform subtitle track\" means the site's own captions, which are",
        "better than recognition when they exist (exact spelling, punctuation and",
        "proper nouns, and free).",
        "",
        *_platform_rows(),
        "",
        "A platform with no track always runs recognition, whatever the video's",
        "metadata claims about captions.",
        "",
        "## Working with porter over MCP",
        "",
        "Use the `localize-video` prompt for the step-by-step workflow. In short:",
        "`porter_doctor` -> `porter_capabilities` -> `porter_probe` ->",
        "`porter_plan` -> confirm with the user -> `porter_job_start(plan=...)` ->",
        "poll `porter_job_status` -> `porter_job_result`.",
        "",
        "The job API is the reliable path for anything long. The stage tools",
        "(`porter_translate`, `porter_burn`) operate on files you already have, and",
        "`porter_transcribe` takes a local file -- for a URL, use the job API.",
        "",
        "## Further reading",
        "",
        f"- `{ARCHITECTURE_URI}` -- this document",
        f"- `{CONFIG_URI}` -- the resolved configuration, secrets masked",
        "- `porter://doctor/guides` -- remediation steps for capability failures",
        "- `porter://jobs/{job_id}/log` -- a running job's log",
        "",
    ]
    return "\n".join(lines).rstrip() + "\n"


def register(server: FastMCP) -> None:
    """Attach the documentation resources and the workflow prompt to ``server``."""

    @server.resource(ARCHITECTURE_URI)
    @protect
    def architecture() -> str:
        """How porter is put together: stages, workflows, capabilities, workflow."""
        try:
            return _render_architecture()
        except PorterError as exc:  # pragma: no cover - assembly needs no config
            return f"# porter architecture\n\nCould not assemble: {exc.message}\n"

    @server.resource(CONFIG_URI)
    @protect
    def config() -> str:
        """The resolved configuration as JSON, with every secret masked.

        Masking is the engine's ``PorterConfig.masked`` -- the same call
        ``porter_config`` makes, so the resource and the tool cannot disagree
        about what is safe to show.
        """
        import json

        try:
            resolved = resolve(None)
        except PorterError as exc:
            return json.dumps(
                {"ok": False, "error": f"configuration could not be resolved: {exc}"},
                ensure_ascii=False,
                indent=2,
            )
        return json.dumps(resolved.masked(), ensure_ascii=False, indent=2)

    @server.prompt(
        name=PROMPT_NAME,
        description=(
            "Step-by-step workflow for localising a video with porter: check the "
            "machine and capabilities, probe, plan, confirm, start the plan, poll, "
            "and read the stage results."
        ),
    )
    @protect
    def localize_video(source: str = "") -> str:
        """The workflow guide, optionally naming the video to localise."""
        if source:
            return (
                f"Localise this video: {source}\n\n"
                "Start with `porter_doctor`, then `porter_capabilities`, then "
                "`porter_probe` on that source. Call `porter_plan` and confirm the "
                "plan with me before starting a job.\n\n" + _WORKFLOW
            )
        return _WORKFLOW
