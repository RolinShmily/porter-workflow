"""The doctor tab of the Porter WebUI."""

from __future__ import annotations

from typing import Any

from porter.webui.doctor_view import get_doctor_table_data
from porter.webui.i18n import resolve_translator

__all__ = ["build_doctor_tab"]


def build_doctor_tab(gr: Any, i18n: Any = None) -> None:
    """Tab 4: Hardware & Environment Diagnostics with balanced, full-width layout."""
    t = resolve_translator(i18n)

    with gr.Tab(t("tab_doctor")) as doctor_tab:
        init_summary, init_rows = get_doctor_table_data()

        with gr.Row(equal_height=False):
            # Left column: Diagnostics summary and refresh button
            with gr.Column(scale=4):
                doc_summary_box = gr.Markdown(init_summary)
                refresh_doctor_btn = gr.Button(t("btn_run_doctor"), variant="primary", size="md")

            # Right column: Full-width detailed component inspection table
            with gr.Column(scale=8):
                doctor_table = gr.Dataframe(
                    value=init_rows,
                    headers=["Component", "Status", "Details", "Guidance"],
                    datatype=["str", "str", "str", "str"],
                    column_widths=["22%", "14%", "48%", "16%"],
                    wrap=True,
                    interactive=False,
                )

        def on_refresh_doctor() -> tuple[str, list[list[str]]]:
            return get_doctor_table_data()

        refresh_doctor_btn.click(on_refresh_doctor, outputs=[doc_summary_box, doctor_table])
        if hasattr(doctor_tab, "select"):
            doctor_tab.select(on_refresh_doctor, outputs=[doc_summary_box, doctor_table])
