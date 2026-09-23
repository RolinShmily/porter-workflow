"""MCP tools for porter.

Tool modules and their planned contents (``docs/REFACTOR_PLAN.md`` §8.1):

``meta.py``
    ``porter_version`` — implementation detail, always available. *(built)*
``inspect.py``
    ``porter_inspect`` — pre-flight probe, no side effects. *(built)*
``plan.py``
    ``porter_plan`` — the resolved execution plan (which extractor, native
    subtitle or ASR, which translator, what is broken) so an agent can confirm
    before committing compute. *(built)*
``jobs.py``
    ``porter_job_start`` / ``_status`` / ``_result`` / ``_cancel`` / ``_list``,
    plus the ``porter://jobs/{id}/log`` resource. *(built)* Long work is
    job-based because encoding a 1080p video takes tens of minutes while MCP tool
    calls time out far sooner.
``stages.py``
    ``porter_transcribe`` / ``_translate`` / ``_burn`` — one phase at a time.
``doctor.py``
    ``porter_doctor`` — the structured capability report, plus the
    ``porter://doctor/guides`` resource. *(built)*

Every tool body is wrapped in :func:`porter_mcp.stdout_guard.protect` so a
stray write to stdout becomes a logged warning instead of a protocol failure.

Tool *names* are a public API for agents; rename only with a migration note.
"""

from __future__ import annotations

from typing import Any

from porter_mcp.tools import config, docs, doctor, inspect, jobs, meta, plan, stages

__all__ = ["register_all"]

#: Modules that contribute tools, in registration order.
_MODULES = (meta, docs, doctor, config, inspect, plan, jobs, stages)


def register_all(server: Any) -> None:
    """Attach every implemented tool to ``server``.

    Modules are imported explicitly rather than discovered, so that adding a
    tool is a deliberate one-line change and the registration order is stable.
    """
    for module in _MODULES:
        module.register(server)
