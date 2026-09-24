# Documentation

Two audiences, kept deliberately separate. If you are not changing the engine,
start with the [README](../README.md) and the Agent Skill references.

## For contributors

| Document | Language | What it answers |
| --- | --- | --- |
| [`ARCHITECTURE.md`](ARCHITECTURE.md) | 中文 | Why the engine is layered this way, where a change belongs, and what breaks if you move it. |
| [`CONFIG.md`](CONFIG.md) | 中文 | Every configuration key: who defines it, who reads it, and which ones are silently ignored. |
| [`MCP.md`](MCP.md) | 中文 | The `porter-mcp` contract, its boundaries, and the parts of the original design it deliberately diverges from. |
| [`MIGRATION.md`](MIGRATION.md) | 中文 | Upgrading from v0.1 (`porter-skill`): what changed, what breaks, and how to fix it. |

## For agents and end users

These are the Agent Skill assets, written as operating instructions rather than
design rationale:

| Document | What it covers |
| --- | --- |
| [`../skills/porter-skill/SKILL.md`](../skills/porter-skill/SKILL.md) | The four-stage workflow and its checkpoints. |
| [`../skills/porter-skill/references/ARCHITECTURE.md`](../skills/porter-skill/references/ARCHITECTURE.md) | What the four stages are and which backends are available. |
| [`../skills/porter-skill/references/CONFIG.md`](../skills/porter-skill/references/CONFIG.md) | How to configure a run. |
| [`../skills/porter-skill/references/MCP.md`](../skills/porter-skill/references/MCP.md) | Tool-by-tool reference and client setup. |

## Project-level documents

* [`../README.md`](../README.md) — English overview and quick start.
* [`../README_zh.md`](../README_zh.md) — Chinese overview and quick start.
* [`../CONTRIBUTING.md`](../CONTRIBUTING.md) — how to build, test and land a change.
* [`../SECURITY.md`](../SECURITY.md) — how to report a vulnerability.
* [`../THIRD_PARTY_NOTICES.md`](../THIRD_PARTY_NOTICES.md) — third-party components and their licences.
* [`../CHANGELOG.md`](../CHANGELOG.md) — release history.
