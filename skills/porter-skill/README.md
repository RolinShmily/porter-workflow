# porter-skill

Agent Skill assets for **Porter Workflow**. This directory is what
`npx skills add` installs; it contains no Python source.

```bash
npx skills add RolinShmily/porter-workflow --skill porter-skill
```

## Why the skill lives in a subdirectory

`porter-workflow` is a monorepo: the engine, the CLI, the MCP server and the WebUI
are Python packages under `src/`, and they must not be shipped to an agent's skill
directory. `skills/` is one of the discovery roots the `skills` CLI walks, and
sub-paths are supported, so a single repository can host both an installable
package and an installable skill.

Two constraints follow from that layout:

1. **There must be exactly one `SKILL.md` in the whole repository.** A second
   one turns the bare `npx skills add RolinShmily/porter-workflow` command into
   an interactive multi-select, which breaks the one-line install.
2. **The skill name must stay `porter-skill`.** The Agent Skills specification
   requires `name` to match the parent directory name, and changing it would
   break the trigger phrase for everyone who already installed it.

The repository being called `porter-workflow` while the skill is called
`porter-skill` is intentional.

## The skill is an SOP, not a parameter reference

`SKILL.md` has one shape, in this order:

1. **Complete environment.** What the machine needs, then three gates:
   `doctor` (machine health, with remediation in its JSON `guidance`) →
   `capabilities` (peer capabilities per stage) → `probe` (facts about this
   source).
2. **Workflow model.** Seven stages, six workflows chosen from source facts, and
   the rule that a plan selects exactly one capability per stage. There is no
   runtime fallback chain; substitution is a new plan.
3. **Standard loop.** `plan --json` → user confirmation → `run --plan` →
   `jobs status` polling → quality check.
4. **Scenario examples** that complete a whole workflow, a **failure/replan
   matrix**, **artifacts and QC**, and the **safety constraints**.

The CLI is the canonical entry point. MCP and WebUI resolve and execute the same
`WorkflowPlan` / `WorkflowResult` model, with the same one-capability-per-stage
and no-runtime-fallback rules; the CLI remains the copyable, auditable contract
because a plan file is plain text.

## Contents

| Path | Purpose |
| --- | --- |
| `SKILL.md` | Environment-first SOP: doctor/capabilities/probe, workflow model, plan → confirm → run --plan → poll → QC. |
| `scripts/` | One Python entry point, `porter.py`, handles the workspace state directory, `--host-check` and `uvx` forwarding on every host; `bootstrap.py` creates a local venv for offline or source-checkout installs. |
| `references/` | Deep material loaded on demand: setup, plan contract, configuration, architecture, MCP, WebUI, hosts. |
| `assets/` | `config.example.json`. |

## Free-first defaults

The wrappers default to the base distribution `porter-workflow`: yt-dlp, platform
subtitles, key-free translation and rendering. For free offline speech
recognition set:

```bash
export PORTER_SKILL_SPEC='porter-workflow[asr-local]'
```

Paid Whisper API and LLM translation need `[llm]` and an OpenAI-compatible key;
the planner will not select a paid capability unless the plan names it or
`--allow-paid` is passed. Translation itself needs no key.

## Restricted desktop hosts

The wrapper is one Python file, so Windows PowerShell uses the same entry point:
`py -3 <SKILL_DIR>\scripts\porter.py ...`. With a workspace-only sandbox, set
`PORTER_SKILL_STATE_DIR` explicitly to an absolute writable workspace directory
and run `--host-check` before `doctor`; this redirects uv, yt-dlp and the job
registry there. See [`references/HOSTS.md`](references/HOSTS.md) for Windows
examples, custom extras and sandbox limitations. Do not grant full filesystem
access as the default workaround.

## Status

**When editing `SKILL.md`, keep it environment-first and plan-driven.** Do not
reintroduce v0.1's "pure-Python, zero-key" claim (false: the only key-free ASR is
local Whisper via `[asr-local]`), the old fallback-chain language, or
VideoCaptioner as a porter backend. Do not describe MCP/WebUI as having their own
pipeline: they execute the same plan. `tests/unit/test_skill_assets.py` enforces
these properties.
