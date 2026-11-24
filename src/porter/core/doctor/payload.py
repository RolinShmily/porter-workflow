"""The JSON payload both frontends ship for a doctor report.

:meth:`~porter.core.doctor.probes.CapabilityReport.to_dict` stays facts-only on
purpose: the fact model must not grow prose just because a terminal wants it.
But an agent that reads ``doctor --json`` and finds a blocker needs the fix in
the same step -- a second round trip to a documentation resource is a step the
agent may not know to take, and over MCP there is no terminal to render a guide
into.

So the report payload is the report plus ``guidance``: the remediation text for
exactly the findings that failed, keyed by the same ``remediation_key`` the
findings already carry. Both the CLI's ``--json`` mode and the MCP tool build
their response here, so the two frontends cannot drift apart.
"""

from __future__ import annotations

from typing import Any

from porter.core.doctor.guides import guide_for
from porter.core.doctor.probes import CapabilityReport

__all__ = ["report_payload"]


def report_payload(report: CapabilityReport) -> dict[str, Any]:
    """The capability report plus remediation for everything that failed.

    Only failing keys are included. A passing capability's guide is irrelevant,
    and sending the whole table would bury the one fix that matters. The full
    table remains available as the ``porter://doctor/guides`` MCP resource.
    """
    payload = report.to_dict()

    guidance: dict[str, dict[str, Any]] = {}
    for finding in report.findings:
        if finding.ok or not finding.remediation_key:
            continue
        guide = guide_for(finding.remediation_key)
        if guide is None:
            continue
        guidance[finding.remediation_key] = {
            "summary": guide.summary,
            "steps": list(guide.steps),
            "note": guide.note,
            "url": guide.url,
        }

    payload["guidance"] = guidance
    return payload
