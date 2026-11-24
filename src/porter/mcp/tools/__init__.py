"""MCP tools for porter.

Tool modules:

``meta.py``
    ``porter_version`` — implementation detail, always available.
``probe.py``
    ``porter_probe`` — source facts (URL, local media, audio, subtitle), no
    plan and no download.
``capabilities.py``
    ``porter_capabilities`` — the peer-level capability catalog with
    availability, cost and data egress.
``plan.py``
    ``porter_plan`` — the resolved ``WorkflowPlan``: workflow, exactly one
    capability per stage, blockers and fixes, executable by
    ``porter_job_start(plan=...)``.
``jobs.py``
    ``porter_job_start`` / ``_status`` / ``_result`` / ``_cancel`` / ``_list``,
    plus the ``porter://jobs/{id}/log`` resource. Long work is job-based because
    encoding a 1080p video takes tens of minutes while MCP tool calls time out
    far sooner.
``stages.py``
    ``porter_transcribe`` / ``_translate`` / ``_burn`` — one phase at a time.
``doctor.py``
    ``porter_doctor`` — the structured capability report, plus the
    ``porter://doctor/guides`` resource.

Every tool body is wrapped in :func:`porter.mcp.stdout_guard.protect` so a
stray write to stdout becomes a logged warning instead of a protocol failure.

Tool *names* are a public API for agents; rename only with a migration note.
"""

from __future__ import annotations

from typing import Any

from porter.mcp.tools import (
    capabilities,
    config,
    docs,
    doctor,
    jobs,
    meta,
    plan,
    probe,
    stages,
)

__all__ = ["register_all"]

#: Modules that contribute tools, in registration order.
_MODULES = (meta, docs, doctor, config, capabilities, probe, plan, jobs, stages)


def register_all(server: Any) -> None:
    """Attach every implemented tool to ``server``.

    Modules are imported explicitly rather than discovered, so that adding a
    tool is a deliberate one-line change and the registration order is stable.
    """
    for module in _MODULES:
        module.register(server)
