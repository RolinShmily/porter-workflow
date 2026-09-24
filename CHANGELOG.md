# Changelog

All notable changes to this project are documented in this file.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

The v0.2 line: a structural rewrite into one engine with three frontends. It is
what `main` currently carries. `porter.__version__` reads `0.2.0`; no release
tag has been cut yet, so the section stays under `Unreleased` until one is.

### Added

- **The MCP server shuts down gracefully on a signal.** `SIGINT`/`SIGTERM` (and
  `SIGBREAK` on Windows) now ask every running job to stop and wait briefly for
  them to unwind, instead of killing them mid-write. A job that unwinds records
  `cancelled` itself, so the registry does not have to infer an interruption from
  a dead PID later. An in-flight FFmpeg encode cannot be interrupted -- it has no
  cancellation point inside it -- so a burn finishes its current step; the wait is
  bounded for that reason, and a second signal skips it.
- **Interrupting `porter run` reports what happened.** Ctrl+C records the job as
  cancelled, prints one line, and exits `130`. Previously the `KeyboardInterrupt`
  unwound to the top level and the user was shown the frame stack of whichever
  library call was running -- ssl, socket, FFmpeg's pipe.
- **Engine/frontend split.** Business logic now lives only in the `porter`
  library. `porter_cli` and `porter_mcp` are thin consumers, and the boundary is
  enforced by `import-linter`.
- **`porter-mcp`**, an MCP server over stdio. Long jobs are exposed as
  `start` / `status` / `result` / `cancel` rather than one blocking call.
- **Persistent job registry**, written to the platform cache directory and
  visible across processes, so a job can be inspected or cancelled from another
  shell.
- **New CLI subcommands**: `inspect`, `plan`, `jobs`, `doctor`, `config`.
- **Local speech recognition** via `faster-whisper` behind the `asr-local`
  extra: no API key, no network, no GPL code.
- **Local media input** — a path or `file://` URL is accepted in addition to a
  remote URL.
- **`porter doctor`** probes FFmpeg, yt-dlp, the JavaScript runtime and every
  ASR/translation backend, and reports which route a job will actually take.
- **LLM translation backend**, alongside the key-free Bing, Google and MyMemory
  backends.
- **ASS subtitle styling** and bilingual / Chinese-only release renders.
- **Launcher binaries** on GitHub Releases: a ~hundreds-of-KB bootstrap that
  installs and updates the engine in `~/.porter/venv`, rather than a frozen
  bundle that would pin a stale yt-dlp.

### Changed

- **Dependencies are locked.** `uv.lock` is committed and CI installs from it
  with `uv sync --locked`, so the environment that is tested is the environment
  that is described. Changing a dependency requires re-locking in the same
  commit, which the gate enforces.
- **Distribution renamed** from `porter-skill` to `porter-workflow`. The
  installable Agent Skill keeps the name `porter-skill`.
- **Library import path** changed from `porter_skill` to `porter`.
- **Configuration** is resolved from `--config`, `$PORTER_CONFIG`, environment
  variables, `./porter.json`, the platform user-config directory, then built-in
  defaults. v0.1's probe for `SKILL.md` to decide where to write is gone; the
  engine no longer knows how it is deployed.
- **CLI is subcommand-based.** The v0.1 flags (`--config-show`, `--doctor`,
  `--inspect`, `--skip-burn`, …) are replaced.
- **YouTube downloads now require an external JavaScript runtime** (Deno
  recommended, Node ≥ 20 accepted), because yt-dlp ≥ 2025.11.12 needs one to
  solve YouTube's JS challenges.
- **Optional backends are behind extras** (`llm`, `stt`, `asr-local`, `images`,
  `mcp`, `all`), so the default install is pure Python plus FFmpeg.

### Fixed

- **`porter jobs list` no longer crashes on Windows.** `os.kill(pid, 0)` reports a
  dead PID as `OSError(ERROR_INVALID_PARAMETER)` there rather than
  `ProcessLookupError`, and it escaped unhandled -- so a single stale record, which
  is exactly what a killed job leaves behind, aborted the whole registry read.
  The command that tells you about interrupted jobs was the one they broke.
- **The recycled-PID guard now works on Windows.** `process_marker` reads the
  process creation time via `GetProcessTimes` instead of falling back to a bare
  PID, and checks the exit code first: `OpenProcess` keeps succeeding for a
  terminated process for a moment after it dies, so the creation time alone would
  have reported a dead owner as identifiable. Liveness is answered per platform
  rather than through `os.kill(pid, 0)`, which has the same blind spot.
- **`ffmpeg.auto_tune` and `asr.audio_denoise` are honoured.** Both were parsed,
  documented, and read by nothing. `auto_tune=false` now skips the hardware-tier
  trial encode and uses `ffmpeg.preset`/`ffmpeg.crf` as configured, in BURN,
  `porter_burn` and `porter doctor` alike; `audio_denoise` is the default for
  `JobOptions.audio_denoise`, which the CLI and MCP override only when asked to.
- **`porter plan` no longer drops `--config`.** `plan_for` re-resolved the
  configuration whenever it was given neither a context nor options, so the plan
  described a different run from the one the same command line would perform --
  every setting that comes from configuration (`asr.engine`, `translator`, ffmpeg,
  subtitle style) was read from defaults. The CLI and the MCP tool now pass a
  context built from the configuration they already resolved.
- **Naming an unavailable ASR backend is no longer silent.** Naming a backend
  promotes it and keeps the rest as fallbacks, so a missing one is not fatal --
  which is exactly why it needs saying: asking for VideoCaptioner's `bijian` and
  quietly getting Bcut's output is indistinguishable from success. It is now
  reported at assembly time, before the expensive work.
- **`porter_doctor` no longer tells agents to call a tool that does not exist.**
  Its description referred to `porter_run`.
- **Truncated encodes are no longer published as finished videos.** Release
  output is written to a temporary path and renamed only after `ffprobe`
  confirms it is readable. In v0.1 the rename was unconditional, so a failed
  encode could be mistaken for a complete render.
- **`import porter` no longer fails on Python 3.10.** `tomli` is declared for
  the 3.10 floor instead of relying on the 3.11 standard-library `tomllib`.
- **Non-ASCII paths and titles no longer crash subprocess reads.** Child output
  is decoded with `errors="replace"` rather than the locale codec, which is
  ASCII under the `POSIX`/`C` locale.
- **CLI, MCP and skill agree on one engine**, so a fix lands in all three at
  once; v0.1 duplicated the pipeline across entry points.
- **yt-dlp failures are reported as data, not as a traceback.** A removed video,
  a geo-block, a bot check or a format selector that matches nothing all arrive
  as `yt_dlp.utils.YoutubeDLError`; only the metadata path let one escape, so the
  user got a Python stack trace for a condition the tool is meant to explain. It
  is now mapped to `ExtractionError` with the URL, platform and original message.
- **Subtitles no longer cut an English word in half.** The midpoint pass in
  `split_chinese_text_by_phrase` used a raw character index, serving a real
  subtitle as `这里的所有内容都在 Wi` / `ndows 上本地运行` — despite the function
  promising not to cut words apart. The cut now snaps to the nearest word
  boundary, and a piece that is one unbreakable token is left whole.
- **`porter plan` accepts `--cookies` / `--cookies-from-browser`.** It inspects
  the source, so it needs the credentials a run needs; its own blocker text told
  users to pass flags the command did not have. (`inspect` always had them.)
- **Glyphs degrade to ASCII on a non-UTF-8 console.** A GBK or cp1252 terminal
  cannot encode `✓`/`✗`, and `errors="backslashreplace"` printed a literal
  `\u2713` at the user. Symbols the console *can* encode (`→`, `…`, `·`) are
  still used; only the impossible ones fall back.

### Removed

- **Unused checkpoint scaffolding on `RunContext`** (`checkpoint_dir`,
  `stage_dir`, `stage_cached`). Nothing called it, no code ever set
  `checkpoint_dir`, and its docstrings advertised a resume-from-disk feature
  that did not exist. Stage-output reuse is real, but it lives in the PREPARE,
  TRANSCRIBE and BURN implementations instead, gated on `--force`.
- **The claimed key-free speech-to-text path.** Every key-free ASR endpoint
  (Bcut, Google Web) was measured and found non-functional as of 2026-09-22.
  Transcription now needs an API key or the VideoCaptioner CLI, and
  `porter doctor` says so before a job starts.
- **Hard dependencies on `openai`, `pillow`, `SpeechRecognition`** in the
  default install.

### Security

- Added [`SECURITY.md`](SECURITY.md) and a private vulnerability reporting
  process.
- The engine never writes to stdout (enforced by ruff rule `T20`); stdout is the
  MCP JSON-RPC channel, so this is a protocol-integrity guarantee, not style.

## [0.1.0] - 2026-08-30

Initial release, published as `porter-skill` (Python distribution and Agent
Skill of the same name).

### Added

- Automated video localization pipeline for YouTube, X/Twitter, Instagram,
  TikTok and Bilibili: download, transcribe, translate, burn.
- Multi-engine ASR chain and multi-backend subtitle translation chain.
- Bilingual and Chinese-only hard-subbed release videos, plus ASS/SRT subtitle
  assets and a standardised `raw/` + `cooked/` output layout.
- Agent Skill packaging (`SKILL.md`, scripts, references, example config).

[Unreleased]: https://github.com/RolinShmily/porter-workflow/compare/b5fd577...HEAD
[0.1.0]: https://github.com/RolinShmily/porter-workflow/commit/c2e4286
