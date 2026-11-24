"""Gradio Web Application for Porter Workflow.

The application is assembled from one module per tab; this module keeps the
console entry point and re-exports the helpers the tabs and tests use.

Pure Gradio native components with comprehensive bilingual (zh/en) i18n support.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

from porter import __version__
from porter.core.config import PorterConfig, resolve
from porter.core.errors import PorterError
from porter.core.jobs.records import JobRegistry
from porter.core.jobs.store import JobStore
from porter.core.logging import configure as configure_logging
from porter.core.logging import get_logger
from porter.webui.config_io import (
    get_config_target,
    read_config_dict,
    save_config_dict,
)
from porter.webui.doctor_view import get_doctor_table_data
from porter.webui.i18n import create_i18n, resolve_translator
from porter.webui.jobs_view import (
    format_source_label,
    format_source_link,
    get_active_job_details,
    get_job_id_choices,
    get_jobs_dataframe_rows,
)
from porter.webui.materials import collect_task_materials, ensure_task_zips
from porter.webui.tabs.doctor import build_doctor_tab
from porter.webui.tabs.jobs import build_jobs_tab
from porter.webui.tabs.settings import build_settings_tab
from porter.webui.tabs.workbench import build_workbench_tab

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
            "Run: uv tool install 'porter-workflow[web]' or pip install 'porter-workflow[web]'\n"
        )
        sys.exit(1)


def create_app(
    store: JobStore | None = None,
    registry: JobRegistry | None = None,
) -> Any:
    """Create the Gradio Blocks application using pure native components."""
    gr = check_gradio_installed()
    i18n = create_i18n(gr)

    reg = registry or JobRegistry()
    st = store or JobStore(registry=reg)

    t = resolve_translator(i18n)

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
    """Entry point for the ``porter`` WebUI command."""
    parser = argparse.ArgumentParser(description="Porter Workflow Web Interface")
    parser.add_argument(
        "--version",
        action="version",
        version=f"porter {__version__}",
    )
    parser.add_argument(
        "--host", default="127.0.0.1", help="Host interface to bind (default: 127.0.0.1)"
    )
    parser.add_argument("--port", type=int, default=7860, help="Port to listen on (default: 7860)")
    parser.add_argument("--share", action="store_true", help="Create a public Gradio share link")
    args = parser.parse_args(argv)
    configure_logging("info")

    gr = check_gradio_installed()
    i18n = create_i18n(gr)
    app = create_app()

    try:
        cfg = resolve(None)
    except PorterError:
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
    "build_doctor_tab",
    "build_jobs_tab",
    "build_settings_tab",
    "build_workbench_tab",
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
