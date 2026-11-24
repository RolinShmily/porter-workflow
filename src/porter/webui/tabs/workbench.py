"""The workbench tab of the Porter WebUI.

The workbench follows the same contract as the CLI and MCP: probe the source,
resolve one :class:`~porter.core.workflow.model.WorkflowPlan`, show it, and
execute exactly that plan. Three consequences are visible in the UI:

* there is no "auto fallback" selector. A capability dropdown names one peer, or
  is left on automatic selection, which is a *planning* decision, not a runtime
  chain;
* the plan JSON is displayed and copyable. A human can hand it to
  ``porter run --plan`` or to ``porter_job_start(plan=...)`` and get the same
  stages;
* changing a planning control invalidates the displayed plan. Starting then
  resolves a new one instead of silently running the old choices.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Generator
from pathlib import Path
from typing import Any

from porter.core.config import resolve
from porter.core.context import RunContext
from porter.core.errors import PorterError
from porter.core.events import (
    ArtifactReady,
    AwaitingUserAction,
    ErrorInfo,
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
from porter.core.models.request import BurnMode, JobOptions, JobRequest, JobResult
from porter.core.workflow.executor import execute_plan
from porter.core.workflow.model import Task, WorkflowPlan, WorkflowResult
from porter.core.workflow.resolver import PlanOverrides, config_fingerprint, resolve_plan
from porter.webui.i18n import resolve_translator
from porter.webui.materials import collect_task_materials

_logger = get_logger("web.workbench")

__all__ = ["build_workbench_tab"]


def _clean_path_input(text: str) -> str:
    cleaned = text.strip()
    if len(cleaned) >= 2 and cleaned[0] == cleaned[-1] and cleaned[0] in ("'", '"'):
        return cleaned[1:-1].strip()
    return cleaned


def _resolve_plan(
    source: str,
    *,
    task: str,
    burn: str,
    transcript: str,
    translate: str,
    target_lang: str,
    subtitle_file: str,
    allow_paid: bool,
    force_translate: bool = False,
    force: bool = False,
) -> WorkflowPlan:
    """Resolve the plan this tab would execute.

    Capability arguments are ids (``whisper-local``, ``bing``, ...); an empty
    string means automatic selection. Cookies come from configuration, because
    the browser UI is not where credentials belong.
    """
    config = resolve(None)
    cleaned_srt = _clean_path_input(subtitle_file) if subtitle_file else ""
    overrides = PlanOverrides(
        task=Task(task),
        output_dir=Path(config.output_dir),
        target_lang=target_lang or "zh-Hans",
        burn=BurnMode(burn),
        transcript=transcript or None,
        translate=translate or None,
        subtitle_file=Path(cleaned_srt) if cleaned_srt else None,
        cookies_file=config.cookies_file,
        cookies_browser=config.cookies_browser,
        force=force,
        audio_denoise=config.asr.audio_denoise,
        allow_paid=allow_paid,
        force_translate=force_translate,
    )
    ctx = RunContext(
        job_id="web-plan",
        options=JobOptions(
            output_dir=overrides.output_dir,
            cookies_file=config.cookies_file,
            cookies_browser=config.cookies_browser,
        ),
        config=config,
    )
    return resolve_plan(source, overrides, ctx)


def _plan_key(
    url: str,
    task: str,
    burn: str,
    transcript: str,
    translate: str,
    target_lang: str,
    subtitle_file: str,
    allow_paid: bool,
    force_translate: bool = False,
    force: bool = False,
) -> tuple[Any, ...]:
    return (
        url,
        task,
        burn,
        transcript,
        translate,
        target_lang,
        subtitle_file,
        allow_paid,
        force_translate,
        force,
    )


def _plan_status(plan: WorkflowPlan) -> str:
    lines = [
        f"**Workflow**: `{plan.workflow.value}` | **Task**: `{plan.task.value}` | "
        f"**Feasible**: {'✅ yes' if plan.feasible else '❌ NO'}"
    ]
    for stage in plan.stages:
        lines.append(f"- `{stage.stage.value}` → `{stage.capability}`")
    for issue in plan.issues:
        label = "blocker" if issue.severity == "blocker" else "note"
        lines.append(f"- **{label}** `{issue.code}`: {issue.message}")
        if issue.alternatives:
            lines.append(f"  - alternatives: {', '.join(f'`{alt}`' for alt in issue.alternatives)}")
        if issue.suggested_workflow is not None:
            lines.append(f"  - suggested workflow: `{issue.suggested_workflow.value}`")
        if issue.fix:
            lines.append(f"  - fix: {issue.fix}")
        if issue.code == "SOURCE_UNUSABLE" and (
            "cookie" in issue.message.lower() or "auth" in issue.message.lower()
        ):
            lines.append(
                "  - WebUI 提示：可在顶部「⚙️ 配置文件与首选项 → 平台 Cookies」填入浏览器名（如 `chrome`/`firefox`）或 `cookies.txt` 路径，保存后重新预检。"
            )
    return "\n".join(lines)


def build_workbench_tab(gr: Any, store: JobStore, registry: JobRegistry, i18n: Any = None) -> None:
    """Tab 1: source, plan, live monitor and deliverables."""
    del registry  # the jobs tab owns the shared view; this tab owns one run
    t = resolve_translator(i18n)

    with gr.Tab(t("tab_workbench")):
        with gr.Row(equal_height=False):
            # Left Column: input, planning choices, plan
            with gr.Column(scale=5):
                gr.Markdown(f"### {t('section_input')}")
                with gr.Row():
                    url_input = gr.Textbox(
                        label=t("input_url_label"),
                        placeholder=t("input_url_placeholder"),
                        lines=1,
                        scale=4,
                    )
                    plan_btn = gr.Button(
                        t("btn_plan"), variant="secondary", size="sm", scale=1
                    )

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
                with gr.Row():
                    task_choice = gr.Dropdown(
                        choices=[
                            (t("task_release"), "release"),
                            (t("task_subtitle"), "subtitle"),
                            (t("task_transcribe"), "transcribe"),
                            (t("task_audio"), "audio"),
                            (t("task_probe"), "probe"),
                            (t("task_convert"), "convert"),
                        ],
                        value="release",
                        label=t("task_label"),
                    )
                    target_lang = gr.Textbox(
                        value="zh-Hans",
                        label=t("target_lang_label"),
                        lines=1,
                    )
                with gr.Row():
                    transcript_choice = gr.Dropdown(
                        choices=[
                            (t("transcript_auto"), ""),
                            (t("transcript_platform"), "platform-track"),
                            (t("transcript_local"), "whisper-local"),
                            (t("transcript_api"), "whisper-api"),
                        ],
                        value="",
                        label=t("transcript_label"),
                    )
                    translate_choice = gr.Dropdown(
                        choices=[
                            (t("translate_auto"), ""),
                            (t("translate_bing"), "bing"),
                            (t("translate_google"), "google"),
                            (t("translate_mymemory"), "mymemory"),
                            (t("translate_llm"), "llm"),
                        ],
                        value="",
                        label=t("translate_label"),
                    )
                burn_mode = gr.Radio(
                    choices=[
                        (t("burn_mode_dual"), "dual"),
                        (t("burn_mode_bilingual_only"), "bilingual_only"),
                        (t("burn_mode_zh_only"), "zh_only"),
                        (t("burn_mode_skip"), "skip"),
                    ],
                    value="dual",
                    label=t("burn_mode_label"),
                )
                subtitle_file = gr.Textbox(
                    label=t("subtitle_file_label"),
                    placeholder="/path/to/source.srt",
                    lines=1,
                )
                with gr.Row():
                    allow_paid = gr.Checkbox(
                        value=False,
                        label=t("allow_paid_label"),
                    )
                    force_translate = gr.Checkbox(
                        value=False,
                        label=t("force_translate_label"),
                    )
                    force_rerun = gr.Checkbox(
                        value=False,
                        label=t("force_rerun_label"),
                    )

                gr.Markdown(f"### {t('section_plan')}")
                plan_status = gr.Markdown(f"ℹ️ {t('plan_idle')}")
                plan_json = gr.Textbox(
                    label=t("plan_json_label"),
                    value="",
                    lines=12,
                    max_lines=24,
                    interactive=False,
                )

                with gr.Row():
                    start_btn = gr.Button(t("btn_start"), variant="primary", size="md", scale=3)
                    cancel_btn = gr.Button(t("btn_cancel"), variant="secondary", size="md", scale=1)

            # Right Column: live monitor, preview, deliverables
            with gr.Column(scale=7):
                gr.Markdown(f"### {t('section_monitor')}")
                live_status = gr.Markdown(f"**{t('live_status_ready')}**")
                log_display = gr.Textbox(
                    label=t("log_display_label"),
                    lines=10,
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

        # -- Plan --------------------------------------------------------------
        current: dict[str, Any] = {"plan": None, "key": None}
        plan_controls = [
            url_input,
            task_choice,
            burn_mode,
            transcript_choice,
            translate_choice,
            target_lang,
            subtitle_file,
            allow_paid,
            force_translate,
            force_rerun,
        ]

        def on_controls_changed(
            url: str,
            task: str,
            burn: str,
            transcript: str,
            translate: str,
            lang: str,
            srt: str,
            paid: bool,
            f_translate: bool,
            f_rerun: bool,
        ) -> str:
            if current["plan"] is None:
                return f"ℹ️ {t('plan_idle')}"
            key = _plan_key(
                url.strip(), task, burn, transcript, translate, lang, srt, paid, f_translate, f_rerun
            )
            if current["key"] == key:
                label = t("plan_ready") if current["plan"].feasible else t("plan_blocked")
                return f"**{label}** — `{current['plan'].plan_id[:19]}…`\n\n" + _plan_status(
                    current["plan"]
                )
            return f"⚠️ **{t('plan_stale')}**"

        def _render_plan_view(plan: WorkflowPlan) -> tuple[str, str, str, str, str, str]:
            probe = plan.source_probe
            title = probe.title or "未知标题"
            platform = probe.platform or probe.kind.value
            duration = (
                f"{probe.duration_seconds:.1f}s"
                if probe.duration_seconds is not None
                else "-"
            )
            tracks = (
                ", ".join(
                    f"{track.role}={track.lang}"
                    + (" (auto)" if track.auto_generated else "")
                    + ("" if track.verified else " (listed)")
                    for track in probe.tracks
                )
                or "无"
            )
            label = t("plan_ready") if plan.feasible else t("plan_blocked")
            status = f"**{label}** — `{plan.plan_id[:19]}…`\n\n" + _plan_status(plan)
            return title, platform, duration, tracks, status, json.dumps(
                plan.to_dict(), ensure_ascii=False, indent=2
            )

        def on_plan(
            url: str,
            task: str,
            burn: str,
            transcript: str,
            translate: str,
            lang: str,
            srt: str,
            paid: bool,
            f_translate: bool,
            f_rerun: bool,
        ) -> tuple[str, str, str, str, str, str]:
            if not url or not url.strip():
                return "⚠️ 未输入链接或路径", "-", "-", "-", "**计划未生成**", ""
            source = url.strip()
            key = _plan_key(
                source, task, burn, transcript, translate, lang, srt, paid, f_translate, f_rerun
            )
            try:
                plan = _resolve_plan(
                    source,
                    task=task,
                    burn=burn,
                    transcript=transcript,
                    translate=translate,
                    target_lang=lang,
                    subtitle_file=srt,
                    allow_paid=paid,
                    force_translate=f_translate,
                    force=f_rerun,
                )
            except (PorterError, ValueError, OSError) as exc:
                current["plan"] = None
                current["key"] = None
                return "❌ 预检失败", "-", "-", "-", f"**计划失败**: {exc}", ""

            current["plan"] = plan
            current["key"] = key
            return _render_plan_view(plan)

        plan_outputs = [
            video_title,
            video_platform,
            video_duration,
            video_subs,
            plan_status,
            plan_json,
        ]
        plan_btn.click(
            on_plan,
            inputs=plan_controls,
            outputs=plan_outputs,
        )
        if hasattr(url_input, "submit"):
            url_input.submit(
                on_plan,
                inputs=plan_controls,
                outputs=plan_outputs,
            )

        for control in plan_controls:
            if hasattr(control, "change"):
                control.change(
                    on_controls_changed,
                    inputs=plan_controls,
                    outputs=[plan_status],
                )

        # -- Run ---------------------------------------------------------------
        active_job_ref: dict[str, str | None] = {"job_id": None}
        result_holder: dict[str, JobResult | None] = {"result": None}
        workflow_holder: dict[str, WorkflowResult | None] = {"workflow": None}

        def on_run_streaming(
            url: str,
            task: str,
            burn: str,
            transcript: str,
            translate: str,
            lang: str,
            srt: str,
            paid: bool,
            f_translate: bool,
            f_rerun: bool,
            progress: Any = None,
        ) -> Generator[tuple[str, str], None, None]:
            prog = progress or (gr.Progress(track_tqdm=False) if hasattr(gr, "Progress") else None)
            result_holder["result"] = None
            workflow_holder["workflow"] = None

            if not url or not url.strip():
                yield ("⚠️ 请先输入视频链接或本地路径", "错误：未指定源")
                return

            source = url.strip()
            key = _plan_key(
                source, task, burn, transcript, translate, lang, srt, paid, f_translate, f_rerun
            )
            try:
                config = resolve(None)
            except (PorterError, ValueError, OSError) as exc:
                yield (f"❌ **配置解析失败**: {exc}", "请检查配置文件")
                return

            current_fp = config_fingerprint(
                RunContext(job_id="web-check", options=JobOptions(), config=config)
            )
            cached = current["plan"] if current["key"] == key else None
            if cached is not None and cached.config_fingerprint != current_fp:
                cached = None

            plan = cached
            if plan is None:
                yield ("🧭 **正在预检并解析计划…**", f"source={source}")
                try:
                    plan = _resolve_plan(
                        source,
                        task=task,
                        burn=burn,
                        transcript=transcript,
                        translate=translate,
                        target_lang=lang,
                        subtitle_file=srt,
                        allow_paid=paid,
                        force_translate=f_translate,
                        force=f_rerun,
                    )
                except (PorterError, ValueError, OSError) as exc:
                    yield (f"❌ **计划失败**: {exc}", "无法生成计划")
                    return
                current["plan"] = plan
                current["key"] = key

            if not plan.feasible:
                blocker_lines: list[str] = []
                for issue in plan.blockers:
                    line = f"- `{issue.code}`: {issue.message}" + (
                        f" → {issue.fix}" if issue.fix else ""
                    )
                    blocker_lines.append(line)
                    if issue.code == "SOURCE_UNUSABLE" and (
                        "cookie" in issue.message.lower() or "auth" in issue.message.lower()
                    ):
                        blocker_lines.append(
                            "  💡 WebUI 操作指引：YouTube / 部分平台在当前网络下触发了登录或反机器人校验。"
                            "请切换到顶部「⚙️ 配置文件与首选项 → 平台 Cookies 与网络」，"
                            "选择你已登录该平台的浏览器（如 `chrome`/`firefox`/`chromium`）或填入导出的 `cookies.txt` 路径，"
                            "点击「💾 保存表单配置」后回到工作台重新「预检并生成计划」。"
                        )
                yield (
                    f"❌ **计划不可行** (`{plan.plan_id[:19]}…`)",
                    "请先解决以下阻塞项，再重新生成计划：\n" + "\n".join(blocker_lines),
                )
                return

            try:
                request = JobRequest.from_source(
                    plan.source,
                    options=JobOptions(
                        output_dir=Path(plan.runtime.output_dir),
                        force=plan.runtime.force,
                    ),
                )
            except (PorterError, ValueError, OSError) as exc:
                yield (f"❌ **任务创建失败**: {exc}", "检查源与输出目录")
                return

            job = store.create(request)
            active_job_ref["job_id"] = job.job_id
            ctx = RunContext(
                job_id=job.job_id,
                options=request.options,
                config=config,
            )
            store.attach(job, ctx)

            logs: list[str] = [
                f"[{job.job_id}] workflow={plan.workflow.value} task={plan.task.value}",
                f"[{job.job_id}] plan={plan.plan_id}",
            ]
            yield (f"**任务开始**: `{job.job_id}`", "\n".join(logs))

            event_queue: list[Event] = []
            queue_lock = threading.Lock()

            def sink(event: Event) -> None:
                with queue_lock:
                    event_queue.append(event)

            ctx.events = sink

            def worker() -> None:
                try:
                    outcome = execute_plan(plan, ctx)
                    workflow_holder["workflow"] = outcome.workflow
                    result_holder["result"] = outcome.job
                except Exception as exc:  # pragma: no cover - executor records its own failures
                    _logger.exception("Workflow failed in web worker: %s", exc)
                    result_holder["result"] = JobResult(
                        job_id=job.job_id,
                        state=JobState.FAILED,
                        error=ErrorInfo.from_exception(exc),
                    )
                finally:
                    final = result_holder["result"] or JobResult(
                        job_id=job.job_id, state=JobState.FAILED
                    )
                    store.finish(job, final)

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
                            f"⏸ 【人机协同】等待用户操作: {evt.action} - {evt.message}"
                        )

                current_status = (
                    f"**运行中**: `{job.job_id}` | 阶段: `{job.to_record().phase or '初始化'}`"
                )
                yield (current_status, "\n".join(logs[-100:]))
                thread.join(timeout=0.3)

            final_res = result_holder["result"]
            final_wf = workflow_holder["workflow"]
            if final_res and final_res.state == JobState.DONE:
                status_text = f"🎉 **任务圆满完成** (`{job.job_id}`)"
            elif final_res and final_res.state == JobState.CANCELLED:
                status_text = f"⏹ **任务已取消** (`{job.job_id}`)"
            else:
                err_msg = final_res.error.message if final_res and final_res.error else "未知错误"
                hints: list[str] = []
                if final_wf and final_wf.error:
                    if final_wf.error.alternatives:
                        alts = ", ".join(f"`{a}`" for a in final_wf.error.alternatives)
                        hints.append(f"可选替代能力: {alts}")
                    if final_wf.error.suggested_workflow is not None:
                        hints.append(f"建议改用工作流: `{final_wf.error.suggested_workflow.value}`")
                hint_suffix = (" — " + "；".join(hints)) if hints else ""
                status_text = f"❌ **任务失败**: {err_msg}{hint_suffix}"
                if hints:
                    logs.append("💡 " + "；".join(hints))

            yield (status_text, "\n".join(logs[-100:]))

        def on_finish() -> tuple[
            str | None,
            str | None,
            str | None,
            str | None,
            list[str] | None,
            list[str] | None,
        ]:
            workflow = workflow_holder["workflow"]
            final_res = result_holder["result"]

            artifacts: dict[str, str] = workflow.artifacts if workflow else {}
            task_dir = (
                Path(workflow.task_dir)
                if workflow and workflow.task_dir
                else (final_res.task_dir if final_res else None)
            )
            if task_dir is None:
                return None, None, None, None, None, None

            bilingual_path = artifacts.get("video_bilingual")
            zh_path = artifacts.get("video_zh")
            if bilingual_path and not Path(bilingual_path).is_file():
                bilingual_path = None
            if zh_path and not Path(zh_path).is_file():
                zh_path = None

            raw_files, cooked_files, raw_zip, cooked_zip, _ = collect_task_materials(task_dir)
            return (
                bilingual_path,
                zh_path,
                cooked_zip,
                raw_zip,
                cooked_files if cooked_files else None,
                raw_files if raw_files else None,
            )

        def on_sync_plan() -> tuple[Any, Any, Any, Any, Any, Any]:
            plan = current.get("plan")
            if plan is None:
                return (
                    gr.update(),
                    gr.update(),
                    gr.update(),
                    gr.update(),
                    gr.update(),
                    gr.update(),
                )
            return _render_plan_view(plan)

        start_btn.click(
            on_run_streaming,
            inputs=plan_controls,
            outputs=[live_status, log_display],
        ).then(
            on_sync_plan,
            outputs=plan_outputs,
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
