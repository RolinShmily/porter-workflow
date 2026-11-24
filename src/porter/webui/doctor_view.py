"""System diagnostics formatting for the WebUI doctor tab."""

from __future__ import annotations

from porter.core.doctor import run_doctor

__all__ = ["get_doctor_table_data"]


def get_doctor_table_data() -> tuple[str, list[list[str]]]:
    """Probe system health against the effective config and format as rows."""
    report = run_doctor()

    if not report.ok or len(report.blockers) > 0:
        summary = (
            "### 🩺 System Diagnostics: ❌ Critical Blockers Detected\n"
            "> Core dependencies missing; pipeline cannot run. Please refer to guides below."
        )
    elif len(report.degradations) > 0:
        summary = (
            "### 🩺 System Diagnostics: ⚠️ Functional Degradations\n"
            "> Pipeline operational, but some hardware encoders or features are disabled."
        )
    else:
        summary = (
            "### 🩺 System Diagnostics: ✅ All Systems Ready\n"
            "> All media processing, transcription, translation, and burn components are operational."
        )

    rows: list[list[str]] = []
    for f in report.findings:
        icon = {
            "passed": "✅ OK",
            "info": "ℹ️ Info",
            "degraded": "⚠️ Degraded",
            "blocked": "❌ Blocked",
        }.get(f.severity.value, "•")
        rows.append([f.label, icon, f.detail or "Operational", f.remediation_key or "-"])

    return summary, rows
