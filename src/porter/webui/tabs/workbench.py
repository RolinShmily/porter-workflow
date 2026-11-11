"""The workbench tab of the Porter WebUI."""

from __future__ import annotations

import threading
from collections.abc import Generator
from typing import Any

from porter.core.config import PorterConfig
from porter.core.context import RunContext
from porter.core.events import (
    ArtifactReady,
    AwaitingUserAction,
    Event,
    JobState,
    LogRecord,
    PhaseCompleted,
    PhaseStarted,
    ProgressUpdated,
)
from porter.core.jobs.records import JobRegistry
from porter.core.jobs.store import JobStore
from porter.core.logging import get_logger
from porter.core.models.inspection import InspectionResult
from porter.core.models.request import BurnMode, JobOptions, JobRequest, JobResult
from porter.core.pipeline import Pipeline
from porter.core.platforms.inspector import inspect_url
from porter.webui.i18n import resolve_translator
from porter.webui.materials import collect_task_materials

_logger = get_logger("web.workbench")

__all__ = ["build_workbench_tab"]


def build_workbench_tab(gr: Any, store: JobStore, registry: JobRegistry, i18n: Any = None) -> None:
    """Tab 1: Two-column native responsive localization workbench."""
    t = resolve_translator(i18n)

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
                    inspect_btn = gr.Button(
                        t("btn_inspect"), variant="secondary", size="sm", scale=1
                    )

                # Native First-class Component Inspection Box
                with gr.Group():
                    video_title = gr.Textbox(
                        label=t("video_title_label"), interactive=False, lines=1
                    )
                    with gr.Row():
                        video_platform = gr.Textbox(
                            label=t("video_platform_label"), interactive=False, scale=1
                        )
                        video_duration = gr.Textbox(
                            label=t("video_duration_label"), interactive=False, scale=1
                        )
                        video_subs = gr.Textbox(
                            label=t("video_subs_label"), interactive=False, scale=1
                        )

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
                        video_bilingual = gr.Video(
                            label=t("video_bilingual_label"), interactive=False
                        )
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
                return (
                    f"❌ 预检失败: {result.error_message or '无法解析'}",
                    result.platform or "-",
                    "-",
                    "-",
                )

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
                        logs.append(
                            f"⏸ 【人机协同】等待用户在工作台操作: {evt.action} - {evt.message}"
                        )

                current_status = (
                    f"**运行中**: `{job.job_id}` | 阶段: `{job.to_record().phase or '初始化'}`"
                )
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
