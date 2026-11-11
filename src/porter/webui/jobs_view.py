"""Job registry formatting helpers for the WebUI tables and dropdowns."""

from __future__ import annotations

from pathlib import Path

from porter.core.events import JobState
from porter.core.jobs.records import JobRecord, JobRegistry

__all__ = [
    "format_source_label",
    "format_source_link",
    "get_active_job_details",
    "get_job_id_choices",
    "get_jobs_dataframe_rows",
]


def get_active_job_details(registry: JobRegistry) -> tuple[bool, str, str, str, str, float]:
    """Return (is_active, job_id, phase, status, message, percent) for native widgets."""
    records: list[JobRecord] = registry.read(reap=True)
    active = [
        r for r in records if r.state in (JobState.RUNNING.value, JobState.AWAITING_USER.value)
    ]
    if not active:
        return False, "", "", "", "", 0.0

    job = active[0]
    status_label = "AWAITING_USER" if job.state == JobState.AWAITING_USER.value else "RUNNING"
    phase_label = (job.phase or "initializing").capitalize()
    msg_label = (
        job.interaction_required
        if job.state == JobState.AWAITING_USER.value
        else (job.message or "Running...")
    ) or ""

    return True, job.job_id, phase_label, status_label, msg_label, job.percent


def format_source_label(source: str) -> str:
    """Format long source URL into concise text label."""
    if not source:
        return "-"
    if "youtube.com" in source or "youtu.be" in source:
        vid = ""
        if "v=" in source:
            vid = source.split("v=")[1].split("&")[0]
        elif "youtu.be/" in source:
            vid = source.split("youtu.be/")[1].split("?")[0]
        return f"YouTube ({vid})" if vid else "YouTube"
    if "instagram.com" in source:
        return "Instagram"
    if "bilibili.com" in source:
        return "Bilibili"
    if "x.com" in source or "twitter.com" in source:
        return "X/Twitter"
    if source.startswith("file://"):
        return f"Local: {Path(source).name}"

    return source if len(source) <= 24 else source[:21] + "..."


def format_source_link(source: str) -> str:
    """Format source into clickable markdown link for Dataframe."""
    label = format_source_label(source)
    if source.startswith("http"):
        return f"[{label}]({source})"
    return label


def get_jobs_dataframe_rows(registry: JobRegistry) -> list[list[str]]:
    """Return clean single-line rows for jobs Dataframe with clickable markdown links."""
    records: list[JobRecord] = registry.read(reap=True)
    rows: list[list[str]] = []
    for r in records:
        st_icon = {
            "done": "✅ Done",
            "running": "▶️ Running",
            "awaiting_user": "⏸️ Awaiting User",
            "failed": "❌ Failed",
            "cancelled": "⏹️ Cancelled",
        }.get(r.state, r.state)
        phase_str = (r.phase or "-").capitalize()
        pct_str = f"{r.percent:.0f}%" if r.percent else "-"
        action_str = r.interaction_required or "-"
        src_link = format_source_link(r.source)

        rows.append([r.job_id, st_icon, phase_str, pct_str, action_str, src_link])

    return rows


def get_job_id_choices(registry: JobRegistry) -> list[tuple[str, str]]:
    """Return choices tuple list [(label, job_id), ...] for dropdowns."""
    records: list[JobRecord] = registry.read(reap=True)
    choices: list[tuple[str, str]] = []
    for r in records:
        src_label = format_source_label(r.source)
        label = f"{r.job_id} ({r.state} - {src_label})"
        choices.append((label, r.job_id))
    return choices
