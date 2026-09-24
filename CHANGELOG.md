# Changelog

All notable changes to this project are documented in this file.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.2.3] - 2026-09-24

### Added

- **Domestic mirrors for China, chosen automatically.** Both downloads defaulted
  to endpoints that are slow or unreachable from mainland China: the packages
  come from PyPI, and the Whisper weights from Hugging Face. On a machine that
  looks Chinese -- by timezone, by a UTC+8 offset, or by a `zh` locale -- porter
  now uses USTC's PyPI index for packages and ModelScope for the weights.
  `PORTER_MIRROR=cn` / `~off` forces either direction; an index set by the user
  always wins. ModelScope's `faster-whisper-*` repositories are the official
  CTranslate2 weights rather than a re-conversion -- their `config.json` has the
  same SHA-256 as `Systran/faster-whisper-small`'s -- and they are fetched over
  plain HTTP, so no `modelscope` dependency was needed.
- The ModelScope download resumes an interrupted transfer instead of restarting
  it, writes into a `.part` file that is renamed only when complete, and checks
  for cancellation between chunks so a 480 MB fetch can be stopped while it runs.

### Fixed

- **`PIP_INDEX_URL` now reaches uv.** uv ignores it -- verified: pointed at an
  unreachable host it still resolved from PyPI -- so the most common way to
  configure a mirror in China (`pip config set global.index-url ...`) silently
  did nothing when uv was on `PATH`. The launcher now copies it to
  `UV_DEFAULT_INDEX` rather than letting it be quietly discarded.
- A local Whisper run no longer downloads several hundred megabytes of weights
  before discovering that the `[asr-local]` extra is not installed; the package
  is checked first, and the model second.

## [0.2.2] - 2026-09-24

The launcher binaries were broken. This fixes them.

### Fixed

- **The launcher executables can set themselves up again.** v0.2.0 and v0.2.1
  shipped binaries that could not install on a clean machine. The launcher
  passed `sys.executable` to `uv venv --python`, and `sys.executable` is a Python
  only when the file runs as a script: frozen by PyInstaller it is the launcher
  binary itself. `uv` inspects a `--python` path by *executing* it, so the probe
  re-entered the launcher, which called `uv` again — one `porter.exe --version`
  became 970 nested retries and 219 seconds before it died. Frozen, the
  interpreter is now left to `uv`, which resolves (or fetches) one that satisfies
  `requires-python`; guessing from `PATH` would only turn an old `python3` into a
  confusing pip error later.
- **Re-entry now fails immediately instead of recursing.** The launcher marks
  every child process it spawns, and a marked process refuses with one line and
  exit 1. This is what kept a single wrong argument from becoming a fork bomb —
  the same bug is one clear error rather than 219 seconds of nested output.

### Added

- **Tests for `packaging/launcher.py`, which had none.** That is why a broken
  binary could ship at all: nothing exercised venv creation, and the workflow's
  smoke test pre-created the venv, so it only ever proved argument hand-over.
  The new suite pins the interpreter choice frozen and unfrozen, both no-`uv`
  fallbacks, and the re-entry guard.
- **`packaging/` is now linted and type-checked.** It was outside both scopes and
  carried four latent lint errors, including a `# noqa justification:` comment
  that ruff reads as a noqa directive. CI now runs
  `ruff check src tests packaging` and `mypy src packaging`.
- The `release-exe` workflow walks the real first-run path — empty `PORTER_HOME`,
  create a venv, install this checkout, hand over — which is the path no existing
  test covered.

## [0.2.1] - 2026-09-24

A patch release. v0.2.0 reached PyPI without its downloadable binaries, and its
README carried a claim that had stopped being true.

### Fixed

- **The launcher binaries are actually attached to the release.** The
  `release-exe` workflow built all three platforms successfully and then failed
  at the step that flattens them, so v0.2.0 shipped with no downloadable
  binaries at all. Each artifact lands in a directory *named* `porter-<target>`,
  and the step renamed the file inside it to `porter-<target>` — which resolves
  to that same directory — so `mv` moved each file into itself. Only the Windows
  case looked correct, because `porter-<target>.exe` happens not to be a
  directory name.
- **The README no longer says there is no key-free speech-to-text path.** Local
  Whisper (`[asr-local]`) has been the first backend the chain tries since
  v0.2.0: no key, no network after a one-time model download. Measured on real
  speech — 18 s of audio became 3 cues in 7.2 s on CPU with the default `small`
  model. The two dead key-free *endpoints* (Bcut, Google Web) are still
  documented as dead; what changed is that "the endpoint is gone" and "the
  backend is not installed" are no longer conflated.

## [0.2.0] - 2026-09-24

The first published release. v0.2 is a structural rewrite into one engine with
three frontends (CLI, MCP server, Agent Skill). The `0.1.0` entry below was a
commit, never a tag or an upload, so this is the first version anyone can install.

### Added

- **`PORTER_CACHE_DIR`** overrides where the job registry lives. Needed for more
  than tidiness: `platformdirs` ignores `XDG_CACHE_HOME` on Windows (it asks the
  Known Folder API), so there was no way to point two processes at a temporary
  registry — which is exactly what the cross-process cancellation test has to do.
  It is also the knob for a cache directory that is shared or read-only.
- **TRANSLATE reuses its translation.** A re-run over unchanged cues skips the
  backends and re-renders the subtitles from the cached text, so editing
  `style.*` and re-running is free and still gives the new look. This was the one
  phase with no reuse, on the grounds that its output includes the rendered
  subtitle files and those depend on the style — which is true of the *files*,
  and is why the **text** is cached and never the files. The cache is keyed on a
  content hash of the sentences plus the target language and the engine's
  identity (backend, model, endpoint), so changing the model or editing the cues
  misses while changing the style hits. `--force` bypasses it.
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

- **Python 3.11 is now the floor**, up from 3.10. The declared `>=3.10` was not
  true: the recommended `[asr-local]` extra could not install there at all,
  because `faster-whisper` pulls in `onnxruntime`, which stopped publishing
  `cp310` wheels after 1.23.2 — so `pip install porter-workflow[asr-local]`
  failed outright on 3.10 while `requires-python` claimed it worked. Python 3.10
  reaches end of life on 2026-10-31. The `tomli` dependency and its conditional
  import are gone with it, since `tomllib` is standard from 3.11.
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

- **The job registry is safe for concurrent writers in one process.** The lock
  claimed to make that safe and did not: `msvcrt.locking` refuses rather than
  waits when the same process already holds the byte range through another
  handle, raising `OSError(EDEADLK, "Resource deadlock avoided")`. That is not an
  exotic case — the MCP server publishes to the registry from one thread per job —
  and measured, 16 concurrent publishers lost up to 5 records. A process-local
  lock now serialises threads, with the file lock still serialising processes.
- **A failed lock no longer reports the wrong error.** When taking the lock
  failed, the cleanup tried to unlock a handle that had never locked it, which
  raises `PermissionError` on Windows and *replaced* the `EDEADLK` that explained
  the failure. The caller saw "permission denied" and never the cause.
- **`--asr-engine` works.** It was written into `JobOptions.asr_engine` by all
  three frontends and read by nobody, so `porter run --asr-engine whisper-api`,
  MCP `porter_job_start(asr_engine=…)` and `porter_transcribe(engine=…)` were
  silently ignored while the same value in `asr.engine` worked — which is
  precisely what made it hard to notice. The flag now wins over the config key.
  The test that appeared to cover this set the *config key* while its docstring
  described the *flag*, which is how the gap survived.
- **Platform subtitle tracks are written as UTF-8.** The `.json` (Bilibili) and
  `.vtt` branches of the subtitle downloader read with `encoding="utf-8"` and
  then wrote with `Path.write_text`'s default — the *locale* encoding, GBK on a
  Windows console. Any cue holding an emoji or a replacement character raised
  `UnicodeEncodeError` and killed a job whose subtitle had just been fetched
  successfully. A mechanical test now rejects locale-encoded text writes across
  `src/`, because a behavioural test cannot catch this on CI: Ubuntu's locale is
  UTF-8, so the same code produces the same bytes there.
- **The two release videos are announced as themselves.** BURN emitted both the
  bilingual and the Chinese-only release as the generic `video` kind, so a
  consumer matching on `ArtifactKind` — which its own docstring says downstream
  agents do — could not tell them apart, even though `video_bilingual` and
  `video_zh` existed for exactly that.
- **`porter jobs cancel` no longer promises a checkpoint.** Its message said the
  owning process "stops at the next checkpoint", referring to a resume mechanism
  that was removed; it now says cancellation check.
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

- **`porter_mcp/progress.py`, and the claim that went with it.** A complete
  bridge from engine events to MCP progress notifications — phase weights,
  monotonic percent, the lot — imported by nothing, while `server.py` listed
  "progress notifications" among the things the frontend owns. It does not send
  any: jobs run on background threads and are polled, and the blocking stage
  tools are short enough that a token would buy nothing. The docstring now says
  so instead of the module implying otherwise.
- **Dead helpers whose docstrings described capabilities they did not have.**
  `JobStore._observe_cancel_now` ("exists for the watchdog's own tests"; no test
  called it), `translate.base.apply_texts_to_items` (promised a `strict=True`
  length check that never ran, because the chain applies text through
  `_cues_from_sentences`), `doctor.guides.platform_hint`, and
  `SubtitleSet.has_translation` — whose real check is
  `has_chinese_translation`, since a backend echoing its input satisfies the
  former.
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

[Unreleased]: https://github.com/RolinShmily/porter-workflow/compare/v0.2.3...HEAD
[0.2.3]: https://github.com/RolinShmily/porter-workflow/compare/v0.2.2...v0.2.3
[0.2.2]: https://github.com/RolinShmily/porter-workflow/compare/v0.2.1...v0.2.2
[0.2.1]: https://github.com/RolinShmily/porter-workflow/compare/v0.2.0...v0.2.1
[0.2.0]: https://github.com/RolinShmily/porter-workflow/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/RolinShmily/porter-workflow/commit/c2e4286
