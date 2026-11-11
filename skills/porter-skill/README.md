# porter-skill

Agent Skill assets for **Porter Workflow**. This directory is what
`npx skills add` installs; it contains no Python source.

```bash
npx skills add RolinShmily/porter-workflow --skill porter-skill
```

## Why the skill lives in a subdirectory

`porter-workflow` is a monorepo: the engine, the CLI and the MCP server are
Python packages under `src/`, and they must not be shipped to an agent's skill
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

## Contents

| Path | Purpose |
| --- | --- |
| `SKILL.md` | Frontmatter plus the SOP an agent follows: choose a channel, confirm environment/configuration, then inspect → plan → confirm → run → quality check. |
| `scripts/` | Bash `porter.sh` / `inspect.sh` and native PowerShell `porter.ps1` delegate to `uvx`; `bootstrap.sh` works from a source checkout with local dependencies. |
| `references/` | Deep material loaded only on demand: setup, WebUI operations, configuration, MCP mapping, hosts and architecture. |
| `assets/` | `config.example.json`. |

## Restricted desktop hosts

Use `scripts/porter.ps1` on PowerShell without requiring Git Bash / WSL. With a workspace-only sandbox, set `PORTER_SKILL_STATE_DIR` explicitly to an absolute writable workspace directory and run `--host-check` before `doctor`; this redirects uv, yt-dlp and the job registry there. The CLI wrappers default to `porter-workflow[llm]` rather than pulling `[all]` for a simple `inspect`. See [`references/HOSTS.md`](references/HOSTS.md) for Windows examples, custom extras and sandbox limitations. Do not grant full filesystem access as the default workaround.

## Calling the engine: CLI by default, MCP when configured

The skill drives the **CLI**, through `scripts/porter.sh`. That is not a
preference — it follows from what a skill can and cannot do.

A skill is instructions, not an executable: "the skill calls MCP" can only mean
"`SKILL.md` tells the agent to call `porter_*` tools", and that works solely if
the user has separately configured the MCP server. `npx skills add` writes to the
agent's skill directory and installs no MCP server. A skill whose primary path
were MCP would therefore be broken on every fresh install — the agent would go
looking for `porter_inspect` and find nothing.

The test to apply to any ambiguity here: **on a fresh `npx skills add` with no
MCP configured, does the skill still work?** If not, the path is wrong.

When MCP *is* configured, it is the better path: structured results, job polling
without shell timeouts, and §8.3 sampling, which translates with the host's model
and so needs no API key. It is documented in
[`references/MCP.md`](references/MCP.md) with setup instructions. The MCP tools
are self-describing, so `SKILL.md` does not restate their signatures.

The WebUI is the third channel, and it is **human-operated**: the agent may start
it and explain it, but cannot click it. Its operation guide is
[`references/WEBUI.md`](references/WEBUI.md). All three channels follow the same
SOP and the same quality checks; only the entry point changes.

## Status

**When editing `SKILL.md`, do not reintroduce v0.1's "pure-Python, zero-key"
claim.** It is false: every key-free speech-to-text *endpoint* has stopped
working (measured 2026-09-22). The honest key-free path is local Whisper via the
`[asr-local]` extra; otherwise transcription needs `OPENAI_API_KEY`, a Whisper
API key, or the VideoCaptioner CLI. Translation still needs no key. This belongs
in the skill's `description` as well as its body, because an agent decides
whether to invoke the skill from the description alone.
`tests/unit/test_skill_assets.py` enforces both.
