# Security Policy

## Supported versions

| Version | Supported |
| --- | --- |
| `0.2.x` (current line) | ✅ |
| `0.1.x` (`porter-skill`) | ❌ — upgrade to `0.2.x`; see [`docs/MIGRATION.md`](docs/MIGRATION.md) |

v0.1 is unmaintained. If you are on it, the migration guide is short and the
security posture of v0.2 is materially better (see *Hardening already in
place*).

## Reporting a vulnerability

**Do not open a public issue.**

Use GitHub's private vulnerability reporting: go to the
[Security tab](https://github.com/RolinShmily/porter-workflow/security), then
**Report a vulnerability**. This creates a private advisory visible only to you
and the maintainer.

If you cannot use that, email **rol1n@srprolin.top** with `[porter
security]` in the subject.

Please include:

* What the issue is and which component it affects (`porter` engine,
  `porter` CLI, `porter-mcp`, or the skill assets).
* Steps to reproduce, ideally with the smallest possible input.
* The impact you believe it has.
* Any suggested fix, if you have one.

**Never include real API keys, cookies or session tokens in a report.** Use
placeholder values.

## What to expect

This is a single-maintainer project, so timelines are best-effort rather than
contractual:

| Stage | Target |
| --- | --- |
| Acknowledgement | within 7 days |
| Initial assessment | within 14 days |
| Fix or mitigation | depends on severity; a critical issue is prioritised |
| Public disclosure | coordinated, after a fix is available |

Credit is given in the advisory and the release notes unless you ask otherwise.

## Scope

**In scope** — anything in this repository:

* **Credential leakage.** Porter reads API keys from the environment and
  configuration. Any path that writes a key into output artifacts
  (`metadata.json`, job records on disk, logs, cover images) or prints it
  unmasked is a vulnerability. `porter config list` must mask secrets.
* **Command injection.** URLs, titles and platform metadata flow into FFmpeg and
  yt-dlp invocations. Anything that lets a crafted title or URL run a command is
  in scope.
* **Path traversal / arbitrary write.** Output paths are derived from video IDs
  and titles. Escaping the output directory, or overwriting files outside it, is
  a vulnerability.
* **MCP protocol integrity.** In the stdio transport, stdout is the JSON-RPC
  channel. Any path that lets user-controlled data reach stdout is a
  vulnerability, not a cosmetic bug.
* **Denial of service** that is reachable from a normal command with an
  untrusted input, such as an unbounded allocation driven by remote metadata.

**Out of scope** — report these to the right upstream project:

* Broken extractors, or download failures after a site changes → **yt-dlp**.
* FFmpeg or libass crashes and CVEs → **FFmpeg** / **libass**.
* Vulnerabilities in a declared dependency → that dependency's own tracker. We
  will still bump the pin, so a heads-up is welcome.
* Terms-of-service or copyright questions about downloading a given video.
  Porter is a tool; what you point it at is your responsibility.
* Anything that requires an already-compromised machine or an attacker who can
  write to your configuration file.

## Hardening already in place

Worth knowing before you report, so you do not spend time re-deriving it:

* The engine never calls `print()`; ruff rule `T20` fails the build if it does.
  This is what keeps untrusted data out of the MCP transport.
* Secrets are masked in configuration listings.
* Subprocesses are invoked with argument lists, never `shell=True`.
* Release videos are written to a temporary path and renamed only after
  `ffprobe` confirms they are readable, so a truncated encode is never published
  as a finished file.
* VideoCaptioner is never imported — it is only ever run as a separate process —
  which keeps its GPL-3.0 terms and its supply chain out of the engine.
