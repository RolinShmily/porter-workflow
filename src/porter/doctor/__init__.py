"""Capability probing and remediation guidance.

The v0.1 ``env_check`` module mixed two concerns in one type: ``CheckResult``
carried both machine-readable status **and** multi-paragraph Chinese
installation instructions. That works for a terminal, but the MCP frontend needs
structured data and cannot show a wall of text.

The split:

``probes.py``
    :class:`~porter.doctor.probes.CapabilityReport` — pure facts. Each finding
    carries a ``severity`` (BLOCKER / DEGRADED / INFO) and a
    ``remediation_key``, never prose.
``guides.py``
    The remediation text, keyed by that same ``remediation_key``. Rendered by the
    CLI; exposed as a resource by the MCP frontend.

The two directions are deliberately one-way: a finding names a key, and the text
looks it up. A missing guide degrades to the finding's ``detail``, so adding a
probe cannot break the renderer.

Three checks v0.1 was missing or got wrong, all of them because the failure is
*silent*:

* ``probe_libass`` — v0.1 searched ``ffmpeg -filters`` output for the substring
  ``ass``, which matches ``pass`` and ``classes`` too, so it passed on builds
  with no libass at all.
* ``probe_font`` — v0.1 checked nothing. A missing CJK font renders every
  character as an empty box: the job succeeds and the video is unusable.
* ``probe_js_runtime`` — yt-dlp cannot fully extract YouTube without an external
  JS runtime, and degrades by returning fewer formats rather than by failing.

The pipeline does not run ``doctor``. Each phase asserts only the capabilities it
actually needs, so ``--burn skip`` works on a machine with no libass.
"""

from porter.doctor.guides import GUIDES, Remediation, guide_for
from porter.doctor.probes import (
    CapabilityReport,
    Finding,
    ProbeContext,
    Severity,
    probe_all,
)

__all__ = [
    "GUIDES",
    "CapabilityReport",
    "Finding",
    "ProbeContext",
    "Remediation",
    "Severity",
    "guide_for",
    "probe_all",
]
