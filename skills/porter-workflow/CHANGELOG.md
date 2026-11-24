# Changelog — Porter Workflow Skill

All notable changes to the self-contained `skills/porter-workflow/` Agent Skill are
documented here and tracked via `SKILL.md` `metadata.version` (distributed directly from
`main` via `npx skills add`). Core engine & WebUI releases (`pypi-v*` tags) are tracked
separately in the root [`CHANGELOG.md`](../../CHANGELOG.md).

## [0.4.0] - 2026-11-24

### Added
- Redesigned self-contained `porter-workflow` skill directly wrapping the published
  `porter-workflow>=0.4.0` PyPI Python API (`import porter`), replacing legacy in-repo
  CLI and MCP frontends.
- Standalone pure-JSON script `scripts/porter_cli.py` with automatic `uv run --no-project --with porter-workflow>=0.4.0`
  bootstrapping, Windows `uv.cmd` shim handling, UTF-16/UTF-8 BOM-safe `--plan` loading,
  and `PORTER_SKILL_STATE_DIR` sandbox isolation.
- Unified commands: `--host-check`, `version`, `doctor`, `capabilities`, `probe`,
  `plan --out`, `run --plan`, `jobs list/status/cancel/clear`, `config path/show/set`,
  and optional `webui` launcher (`porter-workflow[web]>=0.4.0`).
- Concise reference pages (`references/PLAN.md`, `references/CONFIG.md`, `references/HOSTS.md`)
  covering target video quality (`1080p` default + `QUALITY_DEGRADED`), manual-only target
  track skip (`TARGET_TRACK_MISSING`), and restricted-host guidance.
