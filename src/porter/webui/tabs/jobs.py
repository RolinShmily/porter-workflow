"""The jobs tab of the Porter WebUI."""

from __future__ import annotations

from typing import Any

from porter.core.events import JobState
from porter.core.jobs.records import JobRegistry
from porter.webui.i18n import resolve_translator
from porter.webui.jobs_view import (
    get_active_job_details,
    get_job_id_choices,
    get_jobs_dataframe_rows,
)
from porter.webui.materials import collect_task_materials

__all__ = ["build_jobs_tab"]


def build_jobs_tab(gr: Any, registry: JobRegistry, i18n: Any = None) -> None:
    """Tab 2: Responsive two-column Job overview with native components."""
    t = resolve_translator(i18n)

    with gr.Tab(t("tab_jobs")) as jobs_tab:
        # Top: Active Agent Task Monitor (Visible ONLY when a task is running)
        is_act, j_id, ph, st, msg, pct = get_active_job_details(registry)
        with gr.Group(visible=is_act) as active_agent_group:
            gr.Markdown(f"### {t('active_task_header')}")
            with gr.Row():
                active_job_box = gr.Textbox(label="Job ID", value=j_id, interactive=False, scale=2)
                active_phase_box = gr.Textbox(label="Phase", value=ph, interactive=False, scale=1)
                active_status_box = gr.Textbox(label="Status", value=st, interactive=False, scale=1)
                active_msg_box = gr.Textbox(
                    label="Message / Action", value=msg, interactive=False, scale=2
                )
            active_progress = gr.Slider(
                label=t("active_progress_label"),
                value=pct,
                minimum=0.0,
                maximum=100.0,
                interactive=False,
            )

        with gr.Row():
            gr.Markdown(f"### {t('section_job_list')}")
            refresh_active_btn = gr.Button(t("btn_refresh_board"), variant="secondary", size="sm")

        # Bottom Two Columns
        with gr.Row(equal_height=False):
            # Left: Native Dataframe for historical records (Explicit percentage widths, 100% full stretch)
            with gr.Column(scale=7):
                initial_rows = get_jobs_dataframe_rows(registry)
                jobs_table = gr.Dataframe(
                    value=initial_rows,
                    headers=["Job ID", "Status", "Phase", "Progress", "Action", "Source"],
                    datatype=["str", "str", "str", "str", "str", "markdown"],
                    column_widths=["22%", "13%", "15%", "10%", "15%", "25%"],
                    wrap=False,
                    interactive=False,
                )

            # Right: Human-in-the-Loop & Historical Materials
            with gr.Column(scale=5):
                job_choices = get_job_id_choices(registry)
                init_choice = job_choices[0][1] if job_choices else ""

                with gr.Group():
                    gr.Markdown(f"### {t('section_hitl')}")
                    target_job_dropdown = gr.Dropdown(
                        label=t("target_job_label"),
                        choices=job_choices,
                        value=init_choice,
                        allow_custom_value=True,
                    )
                    with gr.Row():
                        action_name = gr.Textbox(
                            label=t("action_name_label"), value="confirm", lines=1
                        )
                    action_payload = gr.Textbox(
                        label=t("action_payload_label"),
                        value="{}",
                        lines=2,
                    )
                    submit_action_btn = gr.Button(
                        t("btn_submit_action"), variant="primary", size="sm"
                    )
                    action_result = gr.Markdown("")

                with gr.Group():
                    gr.Markdown(f"### {t('section_hist_materials')}")
                    inspect_job_dropdown = gr.Dropdown(
                        label=t("inspect_job_label"),
                        choices=job_choices,
                        value=init_choice,
                        allow_custom_value=True,
                    )
                    fetch_materials_btn = gr.Button(
                        t("btn_fetch_materials"), variant="secondary", size="sm"
                    )

                    with gr.Row():
                        hist_cooked_zip = gr.File(
                            label=t("hist_cooked_zip_label"), interactive=False
                        )
                        hist_raw_zip = gr.File(label=t("hist_raw_zip_label"), interactive=False)
                    with gr.Row():
                        hist_cooked_files = gr.File(
                            label=t("hist_cooked_label"), file_count="multiple", interactive=False
                        )
                        hist_raw_files = gr.File(
                            label=t("hist_raw_label"), file_count="multiple", interactive=False
                        )

        def load_all_jobs() -> tuple[Any, str, str, str, str, float, list[list[str]], Any, Any]:
            is_active, job_id, phase, status, message, percent = get_active_job_details(registry)
            group_update = gr.Group(visible=is_active)
            rows = get_jobs_dataframe_rows(registry)
            choices = get_job_id_choices(registry)
            def_val = choices[0][1] if choices else ""
            dd_update = gr.Dropdown(choices=choices, value=def_val)
            return (
                group_update,
                job_id,
                phase,
                status,
                message,
                percent,
                rows,
                dd_update,
                dd_update,
            )

        jobs_outputs = [
            active_agent_group,
            active_job_box,
            active_phase_box,
            active_status_box,
            active_msg_box,
            active_progress,
            jobs_table,
            target_job_dropdown,
            inspect_job_dropdown,
        ]
        refresh_active_btn.click(
            load_all_jobs,
            outputs=jobs_outputs,
        )
        if hasattr(jobs_tab, "select"):
            jobs_tab.select(
                load_all_jobs,
                outputs=jobs_outputs,
            )

        def on_select_table_row(evt: Any = None) -> tuple[str, str]:
            if evt and hasattr(evt, "index") and evt.index:
                row_idx = evt.index[0]
                records = registry.read(reap=True)
                if 0 <= row_idx < len(records):
                    selected_id = records[row_idx].job_id
                    return selected_id, selected_id
            return "", ""

        jobs_table.select(on_select_table_row, outputs=[target_job_dropdown, inspect_job_dropdown])

        def on_fetch_materials(job_id: str) -> tuple[Any, Any, Any, Any]:
            if not job_id.strip():
                return None, None, None, None
            rec = registry.get(job_id.strip())
            if rec is None or not rec.task_dir:
                return None, None, None, None

            raws, cookeds, r_zip, c_zip, _ = collect_task_materials(rec.task_dir)
            return c_zip, r_zip, cookeds if cookeds else None, raws if raws else None

        fetch_materials_btn.click(
            on_fetch_materials,
            inputs=[inspect_job_dropdown],
            outputs=[hist_cooked_zip, hist_raw_zip, hist_cooked_files, hist_raw_files],
        )

        def on_submit_action(job_id: str, action: str, payload_str: str) -> str:
            if not job_id.strip():
                return "⚠️ 请提供有效的 Job ID。"
            rec = registry.get(job_id.strip())
            if rec is None:
                return f"❌ 找不到任务 `{job_id}`"
            if rec.state != JobState.AWAITING_USER.value:
                return f"ℹ️ 任务 `{job_id}` 当前状态为 `{rec.state}`，无需等待用户操作。"

            rec.interaction_response = {"action": action, "data": payload_str}
            registry.publish(rec)
            return f"✅ 已成功向任务 `{job_id}` 提交交互响应，流水线即将恢复。"

        submit_action_btn.click(
            on_submit_action,
            inputs=[target_job_dropdown, action_name, action_payload],
            outputs=[action_result],
        )
