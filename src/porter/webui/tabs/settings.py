"""The settings tab of the Porter WebUI."""

from __future__ import annotations

import json
from typing import Any

from porter.core.config import default_en_font, default_zh_font
from porter.webui.config_io import read_config_dict, save_config_dict
from porter.webui.i18n import resolve_translator

__all__ = ["build_settings_tab"]

_COOKIE_BROWSER_CHOICES = [
    "",
    "chrome",
    "chromium",
    "firefox",
    "edge",
    "brave",
    "opera",
    "vivaldi",
    "safari",
]


def build_settings_tab(gr: Any, i18n: Any = None) -> None:
    """Tab 3: Two-column settings layout: Visual form on left, scrollable raw JSON on right."""
    t = resolve_translator(i18n)

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
                            value=init_data.get("llm", {}).get("api_base")
                            or "https://api.deepseek.com/v1",
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
                            ],
                            value=init_data.get("asr", {}).get("engine") or "",
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
                            value=init_data.get("asr", {}).get("whisper_api_base")
                            or "https://api.openai.com/v1",
                        )

                with gr.Accordion(t("accordion_style"), open=False):
                    with gr.Row():
                        style_zh_font = gr.Textbox(
                            label=t("style_zh_font"),
                            value=init_data.get("style", {}).get("zh_font") or default_zh_font(),
                        )
                        style_en_font = gr.Textbox(
                            label=t("style_en_font"),
                            value=init_data.get("style", {}).get("en_font") or default_en_font(),
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

                with gr.Accordion(t("accordion_cookies"), open=True), gr.Row():
                    cookies_browser = gr.Dropdown(
                        choices=_COOKIE_BROWSER_CHOICES,
                        value=init_data.get("cookies_browser") or "",
                        allow_custom_value=True,
                        label=t("cookies_browser"),
                    )
                    cookies_file = gr.Textbox(
                        label=t("cookies_file"),
                        value=str(init_data.get("cookies_file") or ""),
                    )

                with gr.Row():
                    save_form_btn = gr.Button(
                        t("btn_save_form"), variant="primary", size="md", scale=2
                    )
                    reload_all_btn = gr.Button(
                        t("btn_reload_config"), variant="secondary", size="md", scale=1
                    )

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
                    save_raw_btn = gr.Button(
                        t("btn_save_raw"), variant="primary", size="md", scale=2
                    )
                    format_json_btn = gr.Button(
                        t("btn_format_json"), variant="secondary", size="md", scale=1
                    )

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
            style_dict["zh_font"] = zh_font.strip() or default_zh_font()
            style_dict["en_font"] = en_font.strip() or default_en_font()
            style_dict["zh_font_size"] = int(zh_size)
            style_dict["en_font_size"] = int(en_size)
            style_dict["outline_width"] = float(outline)
            style_dict["shadow"] = float(shadow)
            style_dict["bilingual_zh_margin_v"] = int(margin_zh)
            style_dict["bilingual_en_margin_v"] = int(margin_en)

            current["cookies_browser"] = c_browser.strip() or None
            current["cookies_file"] = c_file.strip() or None

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

        format_json_btn.click(
            on_format_json, inputs=[raw_json_box], outputs=[raw_json_box, status_msg]
        )

        def on_save_raw(raw_text: str) -> str:
            _, dest = read_config_dict()
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
                style_d.get("zh_font") or default_zh_font(),
                style_d.get("en_font") or default_en_font(),
                style_d.get("zh_font_size", 52),
                style_d.get("en_font_size", 34),
                style_d.get("outline_width", 3.5),
                style_d.get("shadow", 1.5),
                style_d.get("bilingual_zh_margin_v", 90),
                style_d.get("bilingual_en_margin_v", 35),
                data.get("cookies_browser") or "",
                str(data.get("cookies_file") or ""),
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
                cookies_file,
            ],
        )
