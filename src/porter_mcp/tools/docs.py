"""Resources and a prompt: the reference material an agent reads up front.

The MCP contract lists three resources and one prompt. Two of the resources are here (the
third, ``porter://jobs/{id}/log``, belongs with the job tools that own the data)
along with the prompt.

The design rule that matters is **what is derived versus written down**. Prose
about *why* the pipeline works this way has to be authored -- there is nothing to
derive it from. But every *list* in this module (the phases, the platforms, the
backends and whether their wire format was ever verified) is read out of the
engine at call time, because a hand-maintained list of backends is a list that is
wrong the moment someone adds one. That is the same reasoning behind
``porter_plan`` deriving from ``Pipeline.default`` rather than restating it.

Deliberately absent: live availability. ``porter://docs/architecture`` reports
which backends *exist* and whether they were ever verified against the live
service; it does not probe. Probing is what ``porter_doctor`` and ``porter_plan``
are for, and a resource read that quietly opened network connections would be a
side effect in something a client fetches to render documentation.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from porter.config import resolve
from porter.errors import PorterError
from porter_mcp.stdout_guard import protect

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

Follow these steps in order. The point of the order is that each step is cheaper
than the one after it, so a bad input is rejected before anything expensive runs.

## 1. `porter_inspect` -- is this link usable?

Returns metadata without downloading media. Read `is_valid`, not `ok`: `ok` says
the call worked, `is_valid` says the link is alive. A 404 comes back as
`ok: true, is_valid: false` on purpose -- retrying a dead link is the wrong move.

## 2. `porter_plan` -- what will actually happen?

This is the step that prevents wasted compute. It reports:

- `kind` and `platform`, and the phases that will run
- `subtitles.route`: `platform` (the site's own subtitle track) or `asr`
  (speech recognition). `asr_runs` says whether recognition is needed.
- `translation.needed`: false when a Chinese track is already available
- `feasible` and `blocking_issues`: if this is false, **stop** -- the job would
  fail after a long download
- `notes`: read these. Two matter most:
  - platform tracks are *requested, not guaranteed*. A caption fetch can fail
    (sites rate-limit them, HTTP 429) and fall back to recognition.
  - if the only available recognition backends are unverified endpoints, the
    note says so.

## 3. Confirm with the user before starting

A job can download hundreds of megabytes and run for minutes. Report what
`porter_plan` predicted -- route, phases, burn mode -- and get agreement. If
`feasible` is false, report the blocking issue instead of starting.

## 4. `porter_job_start` -- start it and return immediately

Do not wait. It returns a `job_id` in well under a second. Long work over MCP
must be polled, not blocked on: a tool call has a timeout of a minute or two,
while a burn takes tens of minutes.

Useful arguments: `only_phase` to stop after a phase, `burn` (`skip` to skip
rendering), `target_lang`, `translator`/`asr_engine` to pick a backend.

## 5. `porter_job_status` -- poll until terminal

Poll until the state is `done`, `failed` or `cancelled`. It is cheap and safe to
call repeatedly, and it also sees jobs started by a `porter` CLI in another
terminal.

## 6. `porter_job_result` -- collect the artifacts

Returns the output paths. `porter_job_cancel` stops a running job if the user
changes their mind.

## Checking the output

Read `porter_job_result`'s artifact paths and confirm the files exist and are
non-trivial in size. A release video that is unexpectedly small usually means the
encode failed rather than that the video was short.

## When something is wrong

- **`porter_doctor`** -- is this machine capable? ffmpeg/libass, a JS runtime for
  yt-dlp, fonts, the encoder. Run it before blaming the video.
- **`porter_config`** -- what settings resolved, with secrets masked. Read-only;
  API keys are configured with the `porter` CLI, never over MCP.
- **Recognition failed** -- transcription needs a Whisper API key or a
  VideoCaptioner CLI. The key-free speech endpoints are not usable. If the video
  has no platform subtitle track, there is no working route, and the plan's
  `blocking_issues` is where that shows up.
"""


def _platform_rows() -> list[str]:
    """The platform table, read from the registry rather than restated."""
    from porter.platforms import registry

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


def _backend_rows() -> list[str]:
    """The backend tables, read from the chains the pipeline actually builds.

    Built from ``Pipeline.default`` so this cannot drift from what a run uses.
    Nothing is probed: ``.backends`` is just the configured list, and
    ``endpoint_verified`` is a declaration, not a measurement.
    """
    from porter.context import RunContext
    from porter.models.request import JobOptions

    ctx = RunContext(job_id="docs", options=JobOptions())
    from porter.pipeline import Pipeline

    pipeline = Pipeline.default(ctx)

    rows = [
        "| backend | chain | endpoint verified |",
        "| --- | --- | --- |",
    ]
    for chain_name, chain in (
        ("asr", pipeline.transcriber),
        ("translate", pipeline.translator),
    ):
        for backend in getattr(chain, "backends", []):
            verified = "yes" if getattr(backend, "endpoint_verified", False) else "no"
            rows.append(f"| `{backend.name}` | {chain_name} | {verified} |")
    return rows


def _render_architecture() -> str:
    """Render the architecture document."""
    from porter.events import Phase

    lines = [
        "# porter architecture",
        "",
        "porter localises a video into Chinese: it acquires the media, produces a",
        "transcript, translates it, and burns subtitles into a release video.",
        "",
        "## The four phases",
        "",
        "Each phase consumes the previous phase's in-memory result and writes its",
        "own artifacts, so a failure leaves the completed earlier phases on disk.",
        "",
        "| phase | does | writes |",
        "| --- | --- | --- |",
        "| `prepare` | acquire the media, standardise it, extract audio | `raw/` |",
        "| `transcribe` | the platform's subtitle track, or speech recognition | `raw/subtitle.srt` |",
        "| `translate` | sentence-level translation and cue reconstruction | `cooked/*.srt`, `cooked/*.ass` |",
        "| `burn` | render the release video(s) | `cooked/*.mp4` |",
        "",
        f"Phases, in order: {', '.join(f'`{p.value}`' for p in Phase)}.",
        "",
        "`only_phase` means *stop after this phase*, not *run only this phase* --",
        "each phase needs the previous one's output, so the earlier phases still run.",
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
        "## Backends",
        "",
        "Each chain is tried in order until one succeeds. \"Endpoint verified\" means",
        "the wire format was checked against the live service at some point; an",
        "unverified backend is reverse-engineered and may break without notice.",
        "**This is not a live availability check** -- use `porter_doctor` or",
        "`porter_plan` for that.",
        "",
        *_backend_rows(),
        "",
        "## Working with porter over MCP",
        "",
        "Use the `localize-video` prompt for the step-by-step workflow. In short:",
        "`porter_inspect` -> `porter_plan` -> confirm with the user ->",
        "`porter_job_start` -> poll `porter_job_status` -> `porter_job_result`.",
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
        """How porter is put together: phases, platforms, backends, workflow."""
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
            "Step-by-step workflow for localising a video with porter: inspect, "
            "plan, confirm, start a job, poll, and check the result."
        ),
    )
    @protect
    def localize_video(source: str = "") -> str:
        """The workflow guide, optionally naming the video to localise."""
        if source:
            return (
                f"Localise this video: {source}\n\n"
                "Start with `porter_inspect` on that link, then `porter_plan`, and "
                "confirm the plan with me before starting a job.\n\n" + _WORKFLOW
            )
        return _WORKFLOW
