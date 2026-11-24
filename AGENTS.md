# AGENTS.md — Porter Workflow Repository Guidelines

Project-specific instructions for AI coding agents working in `porter-workflow`.

## 1. Architecture & Boundaries

- **Single core engine**: All business logic lives in `src/porter/core/` and is exposed via the lazy root facade `src/porter/__init__.py` (`plan_source`, `run_plan`, `run_doctor`, `resolve`, `save_key`, `writable_config_path`, `JobRegistry`, etc.).
- **Thin entry points**:
  - `src/porter/webui/` — optional Gradio workbench (`porter-workflow[web]`); delegates to `porter` facade, never duplicates core logic.
  - `skills/porter-workflow/` — self-contained Agent Skill whose `scripts/porter_cli.py` calls the published `porter-workflow` PyPI package directly.
- **Hard invariants** (enforced by CI):
  - `porter.core` and `porter/__init__.py` must **never** import `porter.webui`.
  - No `print()` in `src/porter/core/` or `src/porter/__init__.py` (ruff `T20`); log to stderr via `get_logger`.
  - All `Path.write_text()` and text `open()` in `src/porter/` must pass `encoding="utf-8"`.
  - Public exports in `src/porter/__init__.py` must stay in sync across `_LAZY_EXPORTS`, `TYPE_CHECKING`, and `__all__`.
  - No in-process copyleft dependencies; FFmpeg/ffprobe stay behind the subprocess boundary.

## 2. Changelog Isolation Rules

Keep core/package changes and Agent Skill changes strictly separated:

| File | What goes here | Version source |
| --- | --- | --- |
| `CHANGELOG.md` (repo root) | Core engine (`src/porter/core/`), WebUI (`src/porter/webui/`), packaging, CI, and repo-level docs (`docs/`, `README*.md`) | `src/porter/__init__.py` (`__version__`) |
| `skills/porter-workflow/CHANGELOG.md` | Agent Skill only (`SKILL.md`, `scripts/porter_cli.py`, `references/`) | `skills/porter-workflow/SKILL.md` (`metadata.version`) |

- Do **not** duplicate Skill SOP/wrapper details into root `CHANGELOG.md`.
- Whenever `skills/porter-workflow/SKILL.md` `metadata.version` changes, add a matching `## [<version>]` section in `skills/porter-workflow/CHANGELOG.md` (`tests/unit/test_skill.py` enforces this).

## 3. Release & Tag Conventions

- `pypi-vX.Y.Z` — triggers `.github/workflows/release-pypi.yml` (runs gates and publishes `porter-workflow` to PyPI).
- `skill-vX.Y.Z` — lightweight git tag only for Skill version snapshots (paired with `skills/porter-workflow/CHANGELOG.md`); triggers **no** workflow.

## 4. Verification Gates

Run from repository root before committing:

```bash
uv run ruff check src tests skills
uv run mypy src
uv run lint-imports
uv run pytest -m "not network"
```
