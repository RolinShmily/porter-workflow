"""Task material discovery and archive packaging for the WebUI."""

from __future__ import annotations

import zipfile
from pathlib import Path

from porter.core.logging import get_logger

_logger = get_logger("web.materials")

__all__ = ["collect_task_materials", "ensure_task_zips"]


def ensure_task_zips(task_dir: Path) -> tuple[str | None, str | None]:
    """Generate or retrieve raw.zip and cooked.zip archives for the task."""
    raw_dir = task_dir / "raw"
    cooked_dir = task_dir / "cooked"

    raw_zip_path = task_dir / "raw.zip"
    cooked_zip_path = task_dir / "cooked.zip"

    if raw_dir.is_dir() and any(
        f.is_file() and not f.name.startswith(".") for f in raw_dir.iterdir()
    ):
        try:
            with zipfile.ZipFile(raw_zip_path, "w", compression=zipfile.ZIP_STORED) as zf:
                for f in sorted(raw_dir.iterdir()):
                    if f.is_file() and not f.name.startswith("."):
                        zf.write(f, arcname=f.name)
        except OSError as exc:
            _logger.warning("Failed to create raw.zip: %s", exc)

    if cooked_dir.is_dir() and any(
        f.is_file() and not f.name.startswith(".") for f in cooked_dir.iterdir()
    ):
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
    raw_files: list[str] = (
        sorted(
            [
                str(f.resolve())
                for f in raw_dir.iterdir()
                if f.is_file() and not f.name.startswith(".")
            ]
        )
        if raw_dir.is_dir()
        else []
    )

    cooked_files: list[str] = (
        sorted(
            [
                str(f.resolve())
                for f in cooked_dir.iterdir()
                if f.is_file() and not f.name.startswith(".")
            ]
        )
        if cooked_dir.is_dir()
        else []
    )

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
