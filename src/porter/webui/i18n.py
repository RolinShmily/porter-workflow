"""Internationalization (i18n) support for Porter Web.

Provides comprehensive bilingual (Chinese and English) translations for native Gradio components.
Restricted strictly to 'zh' (Simplified Chinese) and 'en' (English).
"""

from __future__ import annotations

from typing import Any

# ============================================================================
# Bilingual UI Translations (zh & en)
# ============================================================================

TRANSLATIONS_ZH: dict[str, str] = {
    # Header
    "header_title": "Porter Workflow 视频本地化工作台",
    "header_subtitle": "自动化媒体提取、可审计的单能力工作流、双语翻译与 ASS/FFmpeg 专业硬字幕压制。",
    # Tabs
    "tab_workbench": "🎬 视频本地化工作台",
    "tab_jobs": "📋 任务总览与人机协作",
    "tab_settings": "⚙️ 配置文件与首选项",
    "tab_doctor": "🩺 系统体检与环境自检",
    # Workbench Tab - Input & Plan
    "section_input": "1. 媒体输入与预检",
    "input_url_label": "视频链接或本地路径 (URL / Local Path)",
    "input_url_placeholder": "https://www.youtube.com/watch?v=... 或 file:///...",
    "btn_plan": "🧭 预检并生成计划",
    "video_title_label": "视频标题",
    "video_platform_label": "来源平台 / 类型",
    "video_duration_label": "时长规格",
    "video_subs_label": "字幕轨（平台列出）",
    # Workbench Tab - Planning choices
    "section_params": "2. 规划选项",
    "task_label": "任务范围 (Task)",
    "task_probe": "仅探测",
    "task_transcribe": "仅转录",
    "task_subtitle": "生成字幕",
    "task_release": "完整发布（字幕 + 压制）",
    "task_audio": "仅提取音频",
    "task_convert": "格式转换（尚未接入）",
    "burn_mode_label": "压制模式",
    "burn_mode_dual": "双版本压制（双语 + 仅中文）",
    "burn_mode_bilingual_only": "仅双语字幕压制 (Bilingual Only)",
    "burn_mode_zh_only": "仅中文字幕压制 (Chinese Only)",
    "burn_mode_skip": "仅生成字幕，跳过压制 (Subtitles Only)",
    "transcript_label": "转录能力 (单能力，无回退)",
    "transcript_auto": "自动选择可用能力",
    "transcript_platform": "平台原生字幕轨",
    "transcript_local": "本地 Whisper（免费离线）",
    "transcript_api": "Whisper API（付费）",
    "translate_label": "翻译能力 (单能力，无回退)",
    "translate_auto": "自动选择免 Key 端点",
    "translate_bing": "Bing（免 Key）",
    "translate_google": "Google（免 Key）",
    "translate_mymemory": "MyMemory（免 Key）",
    "translate_llm": "LLM（付费）",
    "subtitle_file_label": "供字幕文件（可选，.srt/.vtt）",
    "allow_paid_label": "允许付费能力 (allow_paid)",
    "force_translate_label": "强制重新翻译（忽略平台目标语字幕）",
    "force_rerun_label": "忽略缓存强制重跑 (force)",
    "target_lang_label": "目标翻译语言",
    "quality_label": "目标画质（达不到自动降级）",
    "quality_best": "最高可用原画 (best)",
    "section_plan": "3. 可执行计划 (WorkflowPlan)",
    "plan_idle": "尚未生成计划 — 输入链接或本地路径后点击「预检并生成计划」",
    "plan_stale": "规划选项已变更 — 请重新点击「预检并生成计划」（或直接执行按新选项重新解析）",
    "plan_ready": "计划已生成",
    "plan_blocked": "计划不可行",
    "plan_json_label": "计划 JSON（可交给 CLI --plan 或 MCP job_start(plan=...)）",
    "btn_start": "🚀 执行该计划",
    "btn_cancel": "⏹ 取消任务",
    # Workbench Tab - Monitor
    "section_monitor": "4. 实时执行状态与日志",
    "live_status_ready": "准备就绪",
    "log_display_label": "运行事件与日志流",
    # Workbench Tab - Outputs & Materials
    "section_preview": "5. 视频成品播放预览",
    "tab_preview_bilingual": "双语字幕压制成品 (Bilingual)",
    "tab_preview_zh": "中文单语压制成品 (Chinese Only)",
    "video_bilingual_label": "双语视频播放",
    "video_zh_label": "中文视频播放",
    "accordion_materials": "6. 全套任务物料下载 (Raw & Cooked)",
    "cooked_zip_label": "📦 打包下载 (cooked.zip: 视频+字幕+ASS)",
    "raw_zip_label": "📦 打包下载 (raw.zip: 母版音视频+原字幕+元数据)",
    "cooked_downloads_label": "最终成果细览 (Cooked Files)",
    "raw_downloads_label": "原始物料细览 (Raw Files)",
    # Jobs Tab
    "active_task_header": "⚡ 当前正在执行的 Agent 任务",
    "active_progress_label": "当前任务实时推进进度",
    "btn_refresh_board": "🔄 刷新任务看板",
    "section_job_list": "任务历史记录 (Job Registry)",
    "section_hitl": "人机协同指令响应 (HITL)",
    "target_job_label": "选择目标任务 (Target Job)",
    "action_name_label": "执行动作 (Action)",
    "action_payload_label": "提交参数覆盖 (JSON)",
    "btn_submit_action": "✅ 提交指令并恢复流水线",
    "section_hist_materials": "历史任务物料提取与打包",
    "inspect_job_label": "选择要提取物料的任务",
    "btn_fetch_materials": "📂 提取任务物料",
    "hist_cooked_zip_label": "📦 打包下载 (cooked.zip)",
    "hist_raw_zip_label": "📦 打包下载 (raw.zip)",
    "hist_cooked_label": "译制成果包 (Cooked)",
    "hist_raw_label": "原始物料包 (Raw)",
    # Settings Tab
    "config_target_label": "当前配置文件目标路径",
    "section_form_settings": "结构化配置项 (Form Settings)",
    "accordion_llm": "大模型与翻译设置 (LLM)",
    "llm_key_label": "LLM API Key (DeepSeek / OpenAI)",
    "llm_base_label": "LLM API Base URL",
    "llm_model_label": "LLM 翻译模型 (Model)",
    "accordion_asr": "语音转录设置 (ASR)",
    "asr_default_engine": "首选转录能力（偏好，非付费授权）",
    "asr_auto": "自动偏好（可用时优先）",
    "asr_local": "本地 faster-whisper",
    "asr_api": "Whisper API (OpenAI)",
    "asr_whisper_size": "本地 Whisper 模型大小",
    "asr_denoise_label": "启用提取音频降噪增强 (audio_denoise)",
    "asr_w_key_label": "Whisper API Key (可选)",
    "asr_w_base_label": "Whisper API Base (可选)",
    "accordion_style": "字幕排版与样式 (Style)",
    "style_zh_font": "中文字体 (zh_font)",
    "style_en_font": "英文字体 (en_font)",
    "style_zh_size": "中文字号 (zh_font_size)",
    "style_en_size": "英文字号 (en_font_size)",
    "style_outline": "描边粗细 (outline_width)",
    "style_shadow": "阴影深度 (shadow)",
    "style_margin_zh": "双语中文垂直边距 (bilingual_zh_margin_v)",
    "style_margin_en": "双语英文垂直边距 (bilingual_en_margin_v)",
    "accordion_cookies": "平台 Cookies 与网络 (Optional)",
    "cookies_browser": "浏览器 Cookies 来源 (例: chrome, edge, firefox)",
    "cookies_file": "Cookies 文件路径 (cookies.txt)",
    "btn_save_form": "💾 保存表单配置",
    "btn_reload_config": "🔄 重新读取文件",
    "section_raw_json": "全量 JSON 源码编辑器 (Raw JSON)",
    "raw_json_label": "配置文件源码 (config.json)",
    "btn_save_raw": "💾 保存 JSON 源码",
    "btn_format_json": "🧹 格式化 JSON",
    # Doctor Tab
    "doctor_desc": "实时探测 FFmpeg、libass 滤镜、JavaScript 挑战求解器、GPU 硬件编码器以及 ASR/翻译可用性。",
    "btn_run_doctor": "🔄 重新体检",
    # Table column headers
    "col_job_id": "Job ID",
    "col_status": "状态 (Status)",
    "col_phase": "阶段 (Phase)",
    "col_progress": "进度 (Progress)",
    "col_action": "待办操作 (Action)",
    "col_source": "来源 (Source)",
    "col_component": "检测组件",
    "col_details": "检测详情",
    "col_guidance": "建议指南",
}

TRANSLATIONS_EN: dict[str, str] = {
    # Header
    "header_title": "Porter Workflow Localization Workbench",
    "header_subtitle": "Automated media extraction, auditable single-capability workflows, bilingual translation, and ASS/FFmpeg hardsubbing.",
    # Tabs
    "tab_workbench": "🎬 Workbench",
    "tab_jobs": "📋 Jobs & HITL",
    "tab_settings": "⚙️ Settings",
    "tab_doctor": "🩺 Doctor Diagnostics",
    # Workbench Tab - Input & Plan
    "section_input": "1. Media Input & Preflight",
    "input_url_label": "Video URL or Local File Path",
    "input_url_placeholder": "https://www.youtube.com/watch?v=... or file:///...",
    "btn_plan": "🧭 Probe & Resolve Plan",
    "video_title_label": "Video Title",
    "video_platform_label": "Platform / Kind",
    "video_duration_label": "Duration / Format",
    "video_subs_label": "Subtitle Tracks (listed)",
    # Workbench Tab - Planning choices
    "section_params": "2. Planning Choices",
    "task_label": "Task Scope",
    "task_probe": "Probe only",
    "task_transcribe": "Transcribe only",
    "task_subtitle": "Produce subtitles",
    "task_release": "Full release (subtitles + burn)",
    "task_audio": "Extract audio only",
    "task_convert": "Convert (not implemented)",
    "burn_mode_label": "Hardsub Burn Mode",
    "burn_mode_dual": "Dual Variants (Bilingual + Chinese Only)",
    "burn_mode_bilingual_only": "Bilingual Only",
    "burn_mode_zh_only": "Target Language Only (Chinese Only)",
    "burn_mode_skip": "Subtitles Only (Skip Burn)",
    "transcript_label": "Transcript Capability (one, no fallback)",
    "transcript_auto": "Automatic available peer",
    "transcript_platform": "Platform's own subtitle track",
    "transcript_local": "Local Whisper (free, offline)",
    "transcript_api": "Whisper API (paid)",
    "translate_label": "Translation Capability (one, no fallback)",
    "translate_auto": "Automatic key-free endpoint",
    "translate_bing": "Bing (key-free)",
    "translate_google": "Google (key-free)",
    "translate_mymemory": "MyMemory (key-free)",
    "translate_llm": "LLM (paid)",
    "subtitle_file_label": "Supplied subtitle file (optional, .srt/.vtt)",
    "allow_paid_label": "Allow paid capabilities (allow_paid)",
    "force_translate_label": "Force translation (ignore platform target track)",
    "force_rerun_label": "Ignore cached outputs (force)",
    "target_lang_label": "Target Language",
    "quality_label": "Target Quality (degrades if unavailable)",
    "quality_best": "Best available (best)",
    "section_plan": "3. Executable Plan (WorkflowPlan)",
    "plan_idle": "No plan yet — enter a URL or local path and click Probe & Resolve Plan",
    "plan_stale": "Planning choices changed — click Probe & Resolve Plan to refresh (or Execute to re-resolve)",
    "plan_ready": "Plan resolved",
    "plan_blocked": "Plan is infeasible",
    "plan_json_label": "Plan JSON (hand to CLI --plan or MCP job_start(plan=...))",
    "btn_start": "🚀 Execute This Plan",
    "btn_cancel": "⏹ Cancel Job",
    # Workbench Tab - Monitor
    "section_monitor": "4. Live Execution Status & Logs",
    "live_status_ready": "Ready",
    "log_display_label": "Live Logs & Events",
    # Workbench Tab - Outputs & Materials
    "section_preview": "5. Video Output Preview",
    "tab_preview_bilingual": "Bilingual Hardsub Video",
    "tab_preview_zh": "Target-only Hardsub Video",
    "video_bilingual_label": "Bilingual Video Player",
    "video_zh_label": "Target-only Video Player",
    "accordion_materials": "6. Task Materials Download (Raw & Cooked)",
    "cooked_zip_label": "📦 Download Archive (cooked.zip: Videos/Subs/ASS)",
    "raw_zip_label": "📦 Download Archive (raw.zip: Masters/Audio/Raw Subs/Metadata)",
    "cooked_downloads_label": "Cooked Artifacts (Individual Files)",
    "raw_downloads_label": "Raw Materials (Individual Files)",
    # Jobs Tab
    "active_task_header": "⚡ Active Agent Task Running",
    "active_progress_label": "Real-time Task Progress",
    "btn_refresh_board": "🔄 Refresh Board",
    "section_job_list": "Job Registry",
    "section_hitl": "Human-in-the-Loop Coordination (HITL)",
    "target_job_label": "Select Target Job",
    "action_name_label": "Action Name",
    "action_payload_label": "Override Parameters (JSON)",
    "btn_submit_action": "✅ Submit & Resume",
    "section_hist_materials": "Historical Task Materials",
    "inspect_job_label": "Select Job for Material Extraction",
    "btn_fetch_materials": "📂 Extract Materials",
    "hist_cooked_zip_label": "📦 Download Archive (cooked.zip)",
    "hist_raw_zip_label": "📦 Download Archive (raw.zip)",
    "hist_cooked_label": "Cooked Artifacts",
    "hist_raw_label": "Raw Materials",
    # Settings Tab
    "config_target_label": "Active Config Target Path",
    "section_form_settings": "Structured Form Settings",
    "accordion_llm": "LLM & Translation Settings",
    "llm_key_label": "LLM API Key (DeepSeek / OpenAI)",
    "llm_base_label": "LLM API Base URL",
    "llm_model_label": "LLM Translation Model",
    "accordion_asr": "Speech Recognition Settings (ASR)",
    "asr_default_engine": "Preferred Transcript Capability (preference, not paid consent)",
    "asr_auto": "Automatic preference",
    "asr_local": "Local faster-whisper",
    "asr_api": "Whisper API (OpenAI)",
    "asr_whisper_size": "Local Whisper Model Size",
    "asr_denoise_label": "Enable Audio Denoising (audio_denoise)",
    "asr_w_key_label": "Whisper API Key (Optional)",
    "asr_w_base_label": "Whisper API Base URL (Optional)",
    "accordion_style": "Subtitle Typography & Style",
    "style_zh_font": "Chinese Font Family (zh_font)",
    "style_en_font": "English Font Family (en_font)",
    "style_zh_size": "Chinese Font Size (zh_font_size)",
    "style_en_size": "English Font Size (en_font_size)",
    "style_outline": "Outline Width (outline_width)",
    "style_shadow": "Shadow Depth (shadow)",
    "style_margin_zh": "Bilingual Target Vertical Margin",
    "style_margin_en": "Bilingual English Vertical Margin",
    "accordion_cookies": "Platform Cookies & Network (Optional)",
    "cookies_browser": "Browser for Cookies (e.g., chrome, edge)",
    "cookies_file": "Cookies File Path (cookies.txt)",
    "btn_save_form": "💾 Save Settings",
    "btn_reload_config": "🔄 Reload from Disk",
    "section_raw_json": "Raw JSON Configuration Editor",
    "raw_json_label": "Configuration Source (config.json)",
    "btn_save_raw": "💾 Save Raw JSON",
    "btn_format_json": "🧹 Format JSON",
    # Doctor Tab
    "doctor_desc": "Real-time detection of FFmpeg, libass filter, JavaScript solvers, GPU hardware encoders, and ASR/translation routes.",
    "btn_run_doctor": "🔄 Run Diagnostics",
    # Table column headers
    "col_job_id": "Job ID",
    "col_status": "Status",
    "col_phase": "Phase",
    "col_progress": "Progress",
    "col_action": "Action Required",
    "col_source": "Source",
    "col_component": "Component",
    "col_details": "Details",
    "col_guidance": "Guidance",
}


def create_i18n(gr: Any) -> Any:
    """Create a Gradio I18n instance strictly configured with Chinese and English."""
    if not hasattr(gr, "I18n"):
        return None
    return gr.I18n(
        zh=TRANSLATIONS_ZH,
        en=TRANSLATIONS_EN,
    )


def translate(key: str, lang: str = "zh") -> str:
    """Static fallback lookup for non-Gradio contexts or unit tests."""
    table = TRANSLATIONS_EN if lang.lower().startswith("en") else TRANSLATIONS_ZH
    return table.get(key, key)


def resolve_translator(i18n: Any) -> Any:
    """Return the i18n callable, or a Chinese fallback outside Gradio."""
    if i18n is not None and callable(i18n):
        return i18n
    return lambda key: translate(key, "zh")


__all__ = [
    "TRANSLATIONS_EN",
    "TRANSLATIONS_ZH",
    "create_i18n",
    "resolve_translator",
    "translate",
]
