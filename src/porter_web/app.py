"""Gradio Web Application for Porter Workflow.

Provides a native, clean Human-in-the-Loop workbench for URL inspection,
pipeline execution, live progress streaming, task oversight, raw/cooked
materials download (with raw.zip and cooked.zip archives), system diagnostics,
and configuration management.
Pure Gradio native components with comprehensive bilingual (zh/en) i18n support.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import sys
import threading
import zipfile
from collections.abc import Generator
from pathlib import Path
from typing import Any

from porter.config import (
    PorterConfig,
    config_file,
    default_config,
    find_project_config,
    load_config_file,
)
from porter.context import RunContext
from porter.doctor.probes import probe_all
from porter.errors import ConfigError
from porter.events import (
    ArtifactReady,
    AwaitingUserAction,
    Event,
    JobState,
    LogRecord,
    PhaseCompleted,
    PhaseStarted,
    ProgressUpdated,
)
from porter.jobs.records import JobRecord, JobRegistry
from porter.jobs.store import JobStore
from porter.logging import get_logger
from porter.models.inspection import InspectionResult
from porter.models.request import BurnMode, JobOptions, JobRequest, JobResult
from porter.pipeline import Pipeline
from porter.platforms.inspector import inspect_url
from porter_web.i18n import create_i18n, translate

_logger = get_logger("web")


def check_gradio_installed() -> Any:
    """Verify that gradio is importable; raises SystemExit with instructions otherwise."""
    try:
        import gradio as gr

        return gr
    except ImportError:
        _logger.error(
            "Gradio is required for the web frontend but not installed. "
            "Install it with: pip install 'porter-workflow[web]'"
        )
        sys.stderr.write(
            "Error: Gradio is required for the web interface.\n"
            "Run: pip install 'porter-workflow[web]' or uv pip install 'porter-workflow[web]'\n"
        )
        sys.exit(1)


def get_config_target() -> Path:
    """Resolve destination configuration file: project config if exists, else user config."""
    found = find_project_config()
    if found:
        return found
    return config_file()


def read_config_dict() -> tuple[dict[str, Any], Path]:
    """Read existing config file as dict, or return default mapping."""
    dest = get_config_target()
    if dest.is_file():
        try:
            return load_config_file(dest), dest
        except (ConfigError, OSError) as exc:
            _logger.warning("Failed to parse config %s: %s", dest, exc)
    return default_config().model_dump(mode="json"), dest


def save_config_dict(data: dict[str, Any], dest: Path) -> None:
    """Save config dict to target JSON file with secure permissions."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(data, indent=2, ensure_ascii=False) + "\n"
    dest.write_text(text, encoding="utf-8")
    with contextlib.suppress(OSError):
        dest.chmod(0o600)


def ensure_task_zips(task_dir: Path) -> tuple[str | None, str | None]:
    """Generate or retrieve raw.zip and cooked.zip archives for the task."""
    raw_dir = task_dir / "raw"
    cooked_dir = task_dir / "cooked"

    raw_zip_path = task_dir / "raw.zip"
    cooked_zip_path = task_dir / "cooked.zip"

    if raw_dir.is_dir() and any(f.is_file() and not f.name.startswith(".") for f in raw_dir.iterdir()):
        try:
            with zipfile.ZipFile(raw_zip_path, "w", compression=zipfile.ZIP_STORED) as zf:
                for f in sorted(raw_dir.iterdir()):
                    if f.is_file() and not f.name.startswith("."):
                        zf.write(f, arcname=f.name)
        except OSError as exc:
            _logger.warning("Failed to create raw.zip: %s", exc)

    if cooked_dir.is_dir() and any(f.is_file() and not f.name.startswith(".") for f in cooked_dir.iterdir()):
        try:
            with zipfile.ZipFile(cooked_zip_path, "w", compression=zipfile.ZIP_STORED) as zf:
                for f in sorted(cooked_dir.iterdir()):
                    if f.is_file() and not f.name.startswith("."):
                        zf.write(f, arcname=f.name)
        except OSError as exc:
            _logger.warning("Failed to create cooked.zip: %s", exc)

    r_str = str(raw_zip_path.resolve()) if raw_zip_path.is_file() else None
    c_str = str(cooked_zip_path.resolve()) if cooked_zip_path.is_file() else None
    return r_str, c_str


def collect_task_materials(
    task_dir: str | Path | None,
) -> tuple[list[str], list[str], str | None, str | None, str]:
    """Inspect task directory, package zip archives, and categorize files for download."""
    if not task_dir:
        return [], [], None, None, "暂无任务物料"

    p = Path(task_dir)
    if not p.is_dir():
        return [], [], None, None, f"物料目录不存在: `{p}`"

    raw_dir = p / "raw"
    cooked_dir = p / "cooked"

    # Strictly filter out dotfiles (e.g. .transcribe.json, .translate.json) for web security
    raw_files: list[str] = sorted(
        [str(f.resolve()) for f in raw_dir.iterdir() if f.is_file() and not f.name.startswith(".")]
    ) if raw_dir.is_dir() else []

    cooked_files: list[str] = sorted(
        [str(f.resolve()) for f in cooked_dir.iterdir() if f.is_file() and not f.name.startswith(".")]
    ) if cooked_dir.is_dir() else []

    raw_zip, cooked_zip = ensure_task_zips(p)

    lines: list[str] = [f"**任务目录**: `{p.name}`\n"]

    if raw_files:
        lines.append("### 📦 原始提取物料 (Raw Materials):")
        for rf in raw_files:
            file_path = Path(rf)
            size_kb = file_path.stat().st_size / 1024
            size_str = f"{size_kb / 1024:.2f} MB" if size_kb >= 1024 else f"{size_kb:.1f} KB"
            lines.append(f"- `{file_path.name}` ({size_str})")
    else:
        lines.append("• 暂无 raw 原始物料。")

    lines.append("")
    if cooked_files:
        lines.append("### 🎬 译制产出成果 (Cooked Artifacts):")
        for cf in cooked_files:
            file_path = Path(cf)
            size_kb = file_path.stat().st_size / 1024
            size_str = f"{size_kb / 1024:.2f} MB" if size_kb >= 1024 else f"{size_kb:.1f} KB"
            lines.append(f"- `{file_path.name}` ({size_str})")
    else:
        lines.append("• 暂无 cooked 最终成果。")

    return raw_files, cooked_files, raw_zip, cooked_zip, "\n".join(lines)


def _resolve_t(i18n: Any) -> Any:
    if i18n is not None and callable(i18n):
        return i18n
    return lambda k: translate(k, "zh")


def get_doctor_table_data() -> tuple[str, list[list[str]]]:
    """Probe system health and format as dataframe rows."""
    cfg = PorterConfig()
    report = probe_all(cfg)

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


def get_active_job_details(registry: JobRegistry) -> tuple[bool, str, str, str, str, float]:
    """Return (is_active, job_id, phase, status, message, percent) for native widgets."""
    records: list[JobRecord] = registry.read(reap=True)
    active = [r for r in records if r.state in (JobState.RUNNING.value, JobState.AWAITING_USER.value)]
    if not active:
        return False, "", "", "", "", 0.0

    job = active[0]
    status_label = "AWAITING_USER" if job.state == JobState.AWAITING_USER.value else "RUNNING"
    phase_label = (job.phase or "initializing").capitalize()
    msg_label = (job.interaction_required if job.state == JobState.AWAITING_USER.value else (job.message or "Running...")) or ""

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


def build_workbench_tab(gr: Any, store: JobStore, registry: JobRegistry, i18n: Any = None) -> None:
    """Tab 1: Two-column native responsive localization workbench."""
    t = _resolve_t(i18n)

    with gr.Tab(t("tab_workbench")):
        with gr.Row(equal_height=False):
            # Left Column: Inputs & Controls
            with gr.Column(scale=5):
                gr.Markdown(f"### {t('section_input')}")
                with gr.Row():
                    url_input = gr.Textbox(
                        label=t("input_url_label"),
                        placeholder=t("input_url_placeholder"),
                        lines=1,
                        scale=4,
                    )
                    inspect_btn = gr.Button(t("btn_inspect"), variant="secondary", size="sm", scale=1)

                # Native First-class Component Inspection Box
                with gr.Group():
                    video_title = gr.Textbox(label=t("video_title_label"), interactive=False, lines=1)
                    with gr.Row():
                        video_platform = gr.Textbox(label=t("video_platform_label"), interactive=False, scale=1)
                        video_duration = gr.Textbox(label=t("video_duration_label"), interactive=False, scale=1)
                        video_subs = gr.Textbox(label=t("video_subs_label"), interactive=False, scale=1)

                gr.Markdown(f"### {t('section_params')}")
                burn_mode = gr.Radio(
                    choices=[
                        (t("burn_mode_dual"), "dual"),
                        (t("burn_mode_zh_only"), "zh_only"),
                        (t("burn_mode_skip"), "skip"),
                    ],
                    value="dual",
                    label=t("burn_mode_label"),
                )
                with gr.Row():
                    asr_engine = gr.Dropdown(
                        choices=[
                            (t("asr_auto"), ""),
                            (t("asr_local"), "whisper-local"),
                            (t("asr_api"), "whisper-api"),
                            (t("asr_videocaptioner"), "videocaptioner"),
                        ],
                        value="",
                        label=t("asr_engine_label"),
                    )
                    target_lang = gr.Textbox(
                        value="zh-Hans",
                        label=t("target_lang_label"),
                        lines=1,
                    )

                with gr.Row():
                    start_btn = gr.Button(t("btn_start"), variant="primary", size="md", scale=3)
                    cancel_btn = gr.Button(t("btn_cancel"), variant="secondary", size="md", scale=1)

            # Right Column: Live Monitor, Video Preview, and Deliverables
            with gr.Column(scale=7):
                gr.Markdown(f"### {t('section_monitor')}")
                live_status = gr.Markdown(f"**{t('live_status_ready')}**")
                log_display = gr.Textbox(
                    label=t("log_display_label"),
                    lines=6,
                    interactive=False,
                )

                gr.Markdown(f"### {t('section_preview')}")
                with gr.Tabs():
                    with gr.TabItem(t("tab_preview_bilingual")):
                        video_bilingual = gr.Video(label=t("video_bilingual_label"), interactive=False)
                    with gr.TabItem(t("tab_preview_zh")):
                        video_zh = gr.Video(label=t("video_zh_label"), interactive=False)

                with gr.Accordion(t("accordion_materials"), open=True):
                    with gr.Row():
                        cooked_zip_file = gr.File(
                            label=t("cooked_zip_label"),
                            interactive=False,
                        )
                        raw_zip_file = gr.File(
                            label=t("raw_zip_label"),
                            interactive=False,
                        )
                    with gr.Row():
                        cooked_downloads = gr.File(
                            label=t("cooked_downloads_label"),
                            file_count="multiple",
                            interactive=False,
                        )
                        raw_downloads = gr.File(
                            label=t("raw_downloads_label"),
                            file_count="multiple",
                            interactive=False,
                        )

        # -- Handlers --------------------------------------------------------
        def on_inspect(url: str) -> tuple[str, str, str, str]:
            if not url or not url.strip():
                return "⚠️ 未输入链接", "-", "-", "-"
            result: InspectionResult = inspect_url(url.strip())
            if not result.is_valid:
                return f"❌ 预检失败: {result.error_message or '无法解析'}", result.platform or "-", "-", "-"

            sub_status = "✅ 原生字幕就绪" if result.has_subtitles else "🎙️ 无原生字幕 (需 ASR)"
            dur_fmt = f"{result.duration_label} ({result.aspect_label or '未知'})"
            return (
                result.title or "未知标题",
                result.platform,
                dur_fmt,
                sub_status,
            )

        inspect_btn.click(
            on_inspect,
            inputs=[url_input],
            outputs=[video_title, video_platform, video_duration, video_subs],
        )

        active_job_ref: dict[str, str | None] = {"job_id": None}
        result_holder: dict[str, JobResult | None] = {"result": None}

        def on_run_streaming(
            url: str,
            mode: str,
            engine: str,
            lang: str,
            progress: Any = None,
        ) -> Generator[tuple[str, str], None, None]:
            prog = progress or (gr.Progress(track_tqdm=False) if hasattr(gr, "Progress") else None)
            result_holder["result"] = None

            if not url or not url.strip():
                yield ("⚠️ 请先输入视频链接", "错误：未指定源")
                return

            req = JobRequest.from_source(
                url.strip(),
                options=JobOptions(
                    burn=BurnMode(mode),
                    asr_engine=engine or None,
                    target_lang=lang or "zh-Hans",
                ),
            )
            job = store.create(req)
            active_job_ref["job_id"] = job.job_id
            ctx = RunContext(job_id=job.job_id, options=req.options, config=PorterConfig())
            store.attach(job, ctx)

            logs: list[str] = [f"[{job.job_id}] 任务创建完成，正在初始化管线..."]
            yield (f"**任务开始**: `{job.job_id}`", "\n".join(logs))

            event_queue: list[Event] = []
            queue_lock = threading.Lock()

            def sink(event: Event) -> None:
                with queue_lock:
                    event_queue.append(event)

            ctx.events = sink

            pipeline = Pipeline.default(ctx)

            def worker() -> None:
                try:
                    result_holder["result"] = pipeline.run(req, ctx)
                except Exception as exc:  # pragma: no cover
                    _logger.exception("Pipeline failed in web worker: %s", exc)
                finally:
                    res = result_holder["result"] or JobResult(
                        job_id=job.job_id, state=JobState.FAILED
                    )
                    store.finish(job, res)

            thread = threading.Thread(target=worker, daemon=True)
            thread.start()

            while thread.is_alive() or event_queue:
                events_to_process: list[Event] = []
                with queue_lock:
                    if event_queue:
                        events_to_process = list(event_queue)
                        event_queue.clear()

                for evt in events_to_process:
                    if isinstance(evt, PhaseStarted):
                        logs.append(f"▶ 进入阶段: {evt.phase.value}")
                        if prog:
                            prog(0.1, desc=f"阶段: {evt.phase.value}")
                    elif isinstance(evt, ProgressUpdated):
                        pct = evt.percent / 100.0
                        if prog:
                            prog(pct, desc=f"{evt.phase.value}: {evt.message or ''}")
                    elif isinstance(evt, PhaseCompleted):
                        logs.append(f"✓ 阶段完成: {evt.phase.value}")
                    elif isinstance(evt, ArtifactReady):
                        logs.append(f"📦 产物就绪 [{evt.kind.value}]: {evt.path.name}")
                    elif isinstance(evt, LogRecord):
                        logs.append(f"[{evt.level.upper()}] {evt.message}")
                    elif isinstance(evt, AwaitingUserAction):
                        logs.append(f"⏸ 【人机协同】等待用户在工作台操作: {evt.action} - {evt.message}")

                current_status = f"**运行中**: `{job.job_id}` | 阶段: `{job.to_record().phase or '初始化'}`"
                yield (current_status, "\n".join(logs[-100:]))
                thread.join(timeout=0.3)

            final_res = result_holder["result"]
            if final_res and final_res.state == JobState.DONE:
                status_text = f"🎉 **任务圆满完成** (`{job.job_id}`)"
            elif final_res and final_res.state == JobState.CANCELLED:
                status_text = f"⏹ **任务已取消** (`{job.job_id}`)"
            else:
                err_msg = final_res.error.message if final_res and final_res.error else "未知错误"
                status_text = f"❌ **任务失败**: {err_msg}"

            yield (status_text, "\n".join(logs[-100:]))

        def on_finish() -> tuple[
            str | None,
            str | None,
            str | None,
            str | None,
            list[str] | None,
            list[str] | None,
        ]:
            final_res = result_holder["result"]
            if not final_res or not final_res.task_dir:
                return None, None, None, None, None, None

            task_dir = final_res.task_dir
            cooked_dir = task_dir / "cooked"

            bilingual_path: str | None = None
            zh_path: str | None = None

            bilingual_candidate = cooked_dir / "video_bilingual.mp4"
            if bilingual_candidate.is_file():
                bilingual_path = str(bilingual_candidate.resolve())

            zh_candidate = cooked_dir / "video_zh.mp4"
            if zh_candidate.is_file():
                zh_path = str(zh_candidate.resolve())

            raw_files, cooked_files, raw_zip, cooked_zip, _ = collect_task_materials(task_dir)
            return (
                bilingual_path,
                zh_path,
                cooked_zip,
                raw_zip,
                cooked_files if cooked_files else None,
                raw_files if raw_files else None,
            )

        start_btn.click(
            on_run_streaming,
            inputs=[url_input, burn_mode, asr_engine, target_lang],
            outputs=[live_status, log_display],
        ).then(
            on_finish,
            outputs=[
                video_bilingual,
                video_zh,
                cooked_zip_file,
                raw_zip_file,
                cooked_downloads,
                raw_downloads,
            ],
        )

        def on_cancel() -> str:
            job_id = active_job_ref.get("job_id")
            if not job_id:
                return "当前无正在运行的任务"
            cancelled = store.cancel(job_id)
            return "已发送取消请求" if cancelled else "取消失败或任务已结束"

        cancel_btn.click(on_cancel, outputs=[live_status])


def build_jobs_tab(gr: Any, registry: JobRegistry, i18n: Any = None) -> None:
    """Tab 2: Responsive two-column Job overview with native components."""
    t = _resolve_t(i18n)

    with gr.Tab(t("tab_jobs")):
        # Top: Active Agent Task Monitor (Visible ONLY when a task is running)
        is_act, j_id, ph, st, msg, pct = get_active_job_details(registry)
        with gr.Group(visible=is_act) as active_agent_group:
            gr.Markdown(f"### {t('active_task_header')}")
            with gr.Row():
                active_job_box = gr.Textbox(label="Job ID", value=j_id, interactive=False, scale=2)
                active_phase_box = gr.Textbox(label="Phase", value=ph, interactive=False, scale=1)
                active_status_box = gr.Textbox(label="Status", value=st, interactive=False, scale=1)
                active_msg_box = gr.Textbox(label="Message / Action", value=msg, interactive=False, scale=2)
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
                        action_name = gr.Textbox(label=t("action_name_label"), value="confirm", lines=1)
                    action_payload = gr.Textbox(
                        label=t("action_payload_label"),
                        value="{}",
                        lines=2,
                    )
                    submit_action_btn = gr.Button(t("btn_submit_action"), variant="primary", size="sm")
                    action_result = gr.Markdown("")

                with gr.Group():
                    gr.Markdown(f"### {t('section_hist_materials')}")
                    inspect_job_dropdown = gr.Dropdown(
                        label=t("inspect_job_label"),
                        choices=job_choices,
                        value=init_choice,
                        allow_custom_value=True,
                    )
                    fetch_materials_btn = gr.Button(t("btn_fetch_materials"), variant="secondary", size="sm")

                    with gr.Row():
                        hist_cooked_zip = gr.File(label=t("hist_cooked_zip_label"), interactive=False)
                        hist_raw_zip = gr.File(label=t("hist_raw_zip_label"), interactive=False)
                    with gr.Row():
                        hist_cooked_files = gr.File(label=t("hist_cooked_label"), file_count="multiple", interactive=False)
                        hist_raw_files = gr.File(label=t("hist_raw_label"), file_count="multiple", interactive=False)

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

        refresh_active_btn.click(
            load_all_jobs,
            outputs=[
                active_agent_group,
                active_job_box,
                active_phase_box,
                active_status_box,
                active_msg_box,
                active_progress,
                jobs_table,
                target_job_dropdown,
                inspect_job_dropdown,
            ],
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


def build_settings_tab(gr: Any, i18n: Any = None) -> None:
    """Tab 3: Two-column settings layout: Visual form on left, scrollable raw JSON on right."""
    t = _resolve_t(i18n)

    with gr.Tab(t("tab_settings")):
        init_data, target_path = read_config_dict()

        target_file_input = gr.Textbox(
            label=t("config_target_label"),
            value=str(target_path),
            interactive=False,
            lines=1,
        )
        status_msg = gr.Markdown("")

        with gr.Row(equal_height=False):
            # Left Column: Visual Form
            with gr.Column(scale=6):
                gr.Markdown(f"### {t('section_form_settings')}")
                with gr.Accordion(t("accordion_llm"), open=True):
                    with gr.Row():
                        llm_api_key = gr.Textbox(
                            label=t("llm_key_label"),
                            value=init_data.get("llm", {}).get("api_key") or "",
                            type="password",
                            placeholder="sk-...",
                        )
                        llm_api_base = gr.Textbox(
                            label=t("llm_base_label"),
                            value=init_data.get("llm", {}).get("api_base") or "https://api.deepseek.com/v1",
                            placeholder="https://api.deepseek.com/v1",
                        )
                    llm_model = gr.Textbox(
                        label=t("llm_model_label"),
                        value=init_data.get("llm", {}).get("model") or "deepseek-chat",
                        placeholder="deepseek-chat 或 deepseek-reasoner",
                    )

                with gr.Accordion(t("accordion_asr"), open=True):
                    with gr.Row():
                        asr_engine_cfg = gr.Dropdown(
                            choices=[
                                (t("asr_auto"), ""),
                                (t("asr_local"), "whisper-local"),
                                (t("asr_api"), "whisper-api"),
                                (t("asr_videocaptioner"), "videocaptioner"),
                            ],
                            value=init_data.get("asr", {}).get("engine") or "",
                            allow_custom_value=True,
                            label=t("asr_default_engine"),
                        )
                        asr_local_model_cfg = gr.Dropdown(
                            choices=["tiny", "base", "small", "medium", "large-v3"],
                            value=init_data.get("asr", {}).get("whisper_local_model") or "small",
                            label=t("asr_whisper_size"),
                        )
                    asr_denoise_cfg = gr.Checkbox(
                        value=init_data.get("asr", {}).get("audio_denoise", True),
                        label=t("asr_denoise_label"),
                    )
                    with gr.Row():
                        asr_whisper_key = gr.Textbox(
                            label=t("asr_w_key_label"),
                            value=init_data.get("asr", {}).get("whisper_api_key") or "",
                            type="password",
                        )
                        asr_whisper_base = gr.Textbox(
                            label=t("asr_w_base_label"),
                            value=init_data.get("asr", {}).get("whisper_api_base") or "https://api.openai.com/v1",
                        )

                with gr.Accordion(t("accordion_style"), open=False):
                    with gr.Row():
                        style_zh_font = gr.Textbox(
                            label=t("style_zh_font"),
                            value=init_data.get("style", {}).get("zh_font") or "Microsoft YaHei",
                        )
                        style_en_font = gr.Textbox(
                            label=t("style_en_font"),
                            value=init_data.get("style", {}).get("en_font") or "Arial",
                        )
                    with gr.Row():
                        style_zh_size = gr.Slider(
                            minimum=20,
                            maximum=100,
                            step=1,
                            value=init_data.get("style", {}).get("zh_font_size", 52),
                            label=t("style_zh_size"),
                        )
                        style_en_size = gr.Slider(
                            minimum=16,
                            maximum=80,
                            step=1,
                            value=init_data.get("style", {}).get("en_font_size", 34),
                            label=t("style_en_size"),
                        )
                    with gr.Row():
                        style_outline = gr.Number(
                            value=init_data.get("style", {}).get("outline_width", 3.5),
                            label=t("style_outline"),
                        )
                        style_shadow = gr.Number(
                            value=init_data.get("style", {}).get("shadow", 1.5),
                            label=t("style_shadow"),
                        )
                    with gr.Row():
                        style_margin_zh = gr.Number(
                            value=init_data.get("style", {}).get("bilingual_zh_margin_v", 90),
                            label=t("style_margin_zh"),
                        )
                        style_margin_en = gr.Number(
                            value=init_data.get("style", {}).get("bilingual_en_margin_v", 35),
                            label=t("style_margin_en"),
                        )

                with gr.Accordion(t("accordion_cookies"), open=False), gr.Row():
                    cookies_browser = gr.Textbox(
                        label=t("cookies_browser"),
                        value=init_data.get("cookies_browser") or "",
                    )
                    cookies_file = gr.Textbox(
                        label=t("cookies_file"),
                        value=str(init_data.get("cookies_file") or ""),
                    )

                with gr.Row():
                    save_form_btn = gr.Button(t("btn_save_form"), variant="primary", size="md", scale=2)
                    reload_all_btn = gr.Button(t("btn_reload_config"), variant="secondary", size="md", scale=1)

            # Right Column: Raw JSON Editor with vertical scrolling
            with gr.Column(scale=6):
                gr.Markdown(f"### {t('section_raw_json')}")
                raw_json_box = gr.Textbox(
                    label=t("raw_json_label"),
                    value=json.dumps(init_data, indent=2, ensure_ascii=False),
                    lines=22,
                    max_lines=32,
                    autoscroll=False,
                    interactive=True,
                )
                with gr.Row():
                    save_raw_btn = gr.Button(t("btn_save_raw"), variant="primary", size="md", scale=2)
                    format_json_btn = gr.Button(t("btn_format_json"), variant="secondary", size="md", scale=1)

        # -- Handlers --------------------------------------------------------
        def on_save_form(
            key: str,
            base: str,
            model: str,
            engine: str,
            local_model: str,
            denoise: bool,
            w_key: str,
            w_base: str,
            zh_font: str,
            en_font: str,
            zh_size: float,
            en_size: float,
            outline: float,
            shadow: float,
            margin_zh: float,
            margin_en: float,
            c_browser: str,
            c_file: str,
        ) -> tuple[str, str]:
            current, dest = read_config_dict()

            llm_dict = current.setdefault("llm", {})
            llm_dict["api_key"] = key.strip() or None
            llm_dict["api_base"] = base.strip() or None
            llm_dict["model"] = model.strip() or "deepseek-chat"

            asr_dict = current.setdefault("asr", {})
            asr_dict["engine"] = engine.strip() or None
            asr_dict["whisper_local_model"] = local_model
            asr_dict["audio_denoise"] = denoise
            asr_dict["whisper_api_key"] = w_key.strip() or None
            asr_dict["whisper_api_base"] = w_base.strip() or None

            style_dict = current.setdefault("style", {})
            style_dict["zh_font"] = zh_font.strip() or "Microsoft YaHei"
            style_dict["en_font"] = en_font.strip() or "Arial"
            style_dict["zh_font_size"] = int(zh_size)
            style_dict["en_font_size"] = int(en_size)
            style_dict["outline_width"] = float(outline)
            style_dict["shadow"] = float(shadow)
            style_dict["bilingual_zh_margin_v"] = int(margin_zh)
            style_dict["bilingual_en_margin_v"] = int(margin_en)

            current["cookies_browser"] = c_browser.strip() or ""
            current["cookies_file"] = c_file.strip() or ""

            try:
                save_config_dict(current, dest)
                json_str = json.dumps(current, indent=2, ensure_ascii=False)
                return f"✅ 配置已成功保存至 `{dest}`！", json_str
            except (OSError, TypeError, ValueError) as exc:
                return f"❌ 保存配置失败: {exc}", json.dumps(current, indent=2, ensure_ascii=False)

        save_form_btn.click(
            on_save_form,
            inputs=[
                llm_api_key,
                llm_api_base,
                llm_model,
                asr_engine_cfg,
                asr_local_model_cfg,
                asr_denoise_cfg,
                asr_whisper_key,
                asr_whisper_base,
                style_zh_font,
                style_en_font,
                style_zh_size,
                style_en_size,
                style_outline,
                style_shadow,
                style_margin_zh,
                style_margin_en,
                cookies_browser,
                cookies_file,
            ],
            outputs=[status_msg, raw_json_box],
        )

        def on_format_json(raw_text: str) -> tuple[str, str]:
            try:
                parsed = json.loads(raw_text)
                return json.dumps(parsed, indent=2, ensure_ascii=False), "✅ JSON 格式化成功。"
            except (json.JSONDecodeError, TypeError) as exc:
                return raw_text, f"❌ JSON 格式错误: {exc}"

        format_json_btn.click(on_format_json, inputs=[raw_json_box], outputs=[raw_json_box, status_msg])

        def on_save_raw(raw_text: str) -> str:
            dest = get_config_target()
            try:
                parsed = json.loads(raw_text)
            except (json.JSONDecodeError, TypeError) as exc:
                return f"❌ 无法保存: JSON 解析失败 ({exc})"

            try:
                save_config_dict(parsed, dest)
                return f"✅ 源码配置已成功保存至 `{dest}`！"
            except OSError as exc:
                return f"❌ 写入文件失败: {exc}"

        save_raw_btn.click(on_save_raw, inputs=[raw_json_box], outputs=[status_msg])

        def on_reload_all() -> tuple[
            str,
            str,
            str,
            Any,
            Any,
            Any,
            Any,
            Any,
            Any,
            Any,
            Any,
            Any,
            Any,
            Any,
            Any,
            Any,
            Any,
            Any,
            Any,
            Any,
        ]:
            data, dest = read_config_dict()
            formatted = json.dumps(data, indent=2, ensure_ascii=False)
            llm_d = data.get("llm", {})
            asr_d = data.get("asr", {})
            style_d = data.get("style", {})

            return (
                f"✅ 已从 `{dest}` 重新加载配置。",
                str(dest),
                formatted,
                llm_d.get("api_key") or "",
                llm_d.get("api_base") or "https://api.deepseek.com/v1",
                llm_d.get("model") or "deepseek-chat",
                asr_d.get("engine") or "",
                asr_d.get("whisper_local_model") or "small",
                asr_d.get("audio_denoise", True),
                asr_d.get("whisper_api_key") or "",
                asr_d.get("whisper_api_base") or "https://api.openai.com/v1",
                style_d.get("zh_font") or "Microsoft YaHei",
                style_d.get("en_font") or "Arial",
                style_d.get("zh_font_size", 52),
                style_d.get("en_font_size", 34),
                style_d.get("outline_width", 3.5),
                style_d.get("shadow", 1.5),
                style_d.get("bilingual_zh_margin_v", 90),
                style_d.get("bilingual_en_margin_v", 35),
                data.get("cookies_browser") or "",
            )

        reload_all_btn.click(
            on_reload_all,
            outputs=[
                status_msg,
                target_file_input,
                raw_json_box,
                llm_api_key,
                llm_api_base,
                llm_model,
                asr_engine_cfg,
                asr_local_model_cfg,
                asr_denoise_cfg,
                asr_whisper_key,
                asr_whisper_base,
                style_zh_font,
                style_en_font,
                style_zh_size,
                style_en_size,
                style_outline,
                style_shadow,
                style_margin_zh,
                style_margin_en,
                cookies_browser,
            ],
        )


def build_doctor_tab(gr: Any, i18n: Any = None) -> None:
    """Tab 4: Hardware & Environment Diagnostics with balanced, full-width layout."""
    t = _resolve_t(i18n)

    with gr.Tab(t("tab_doctor")):
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


def create_app(
    store: JobStore | None = None,
    registry: JobRegistry | None = None,
) -> Any:
    """Create the Gradio Blocks application using pure native components."""
    gr = check_gradio_installed()
    i18n = create_i18n(gr)

    reg = registry or JobRegistry()
    st = store or JobStore(registry=reg)

    t = _resolve_t(i18n)

    with gr.Blocks(title="Porter Workflow - AI 视频本地化工作台") as app:
        # Native Markdown branding header fully hooked to i18n
        gr.Markdown(f"# {t('header_title')}")
        gr.Markdown(f"> {t('header_subtitle')}")

        build_workbench_tab(gr, st, reg, i18n)
        build_jobs_tab(gr, reg, i18n)
        build_settings_tab(gr, i18n)
        build_doctor_tab(gr, i18n)

    return app


def main(argv: list[str] | None = None) -> int:
    """Entry point for the ``porter-web`` CLI command."""
    parser = argparse.ArgumentParser(description="Porter Workflow Web Interface")
    parser.add_argument("--host", default="127.0.0.1", help="Host interface to bind (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=7860, help="Port to listen on (default: 7860)")
    parser.add_argument("--share", action="store_true", help="Create a public Gradio share link")
    args = parser.parse_args(argv)

    gr = check_gradio_installed()
    i18n = create_i18n(gr)
    app = create_app()

    cfg = PorterConfig()
    allowed_dirs = [
        str(Path.cwd().resolve()),
        str(cfg.output_dir.resolve()),
    ]

    app.queue().launch(
        server_name=args.host,
        server_port=args.port,
        share=args.share,
        i18n=i18n,
        allowed_paths=allowed_dirs,
    )
    return 0


__all__ = [
    "check_gradio_installed",
    "collect_task_materials",
    "create_app",
    "ensure_task_zips",
    "format_source_label",
    "format_source_link",
    "get_active_job_details",
    "get_config_target",
    "get_doctor_table_data",
    "get_job_id_choices",
    "get_jobs_dataframe_rows",
    "main",
    "read_config_dict",
    "save_config_dict",
]
