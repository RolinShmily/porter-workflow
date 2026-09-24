# Third-Party Notices

Porter Workflow is released under the [MIT License](LICENSE). It builds on the
open-source work listed below. This file records **what those components are,
what they are licensed under, and how porter interacts with them** — because
"how" is what determines whether the licences are satisfied, not just "which".

Nothing in this repository vendors third-party source code. Every component is
consumed in exactly one of three ways:

| Interaction | Meaning for licensing |
| --- | --- |
| **Dependency** | Installed by the user separately (`pip` / `uvx`). Porter imports it in-process. Permissive licences only. |
| **Optional dependency** | Same as above, but only installed when the matching extra is requested. |
| **External program** | Never installed, bundled or imported. Porter spawns it as a subprocess and talks over stdin/stdout. Strong copyleft here cannot reach porter's code. |

Porter deliberately keeps the third category for copyleft tools, so that its own
MIT terms stay intact for everyone downstream.

---

## 1. Runtime dependencies

These are declared in `[project.dependencies]` and installed with every copy of
`porter-workflow`.

| Component | License | Used for |
| --- | --- | --- |
| [yt-dlp](https://github.com/yt-dlp/yt-dlp) | Unlicense (public domain) | Downloading video, audio and platform subtitle tracks; extracting metadata. |
| [requests](https://github.com/psf/requests) | Apache-2.0 | HTTP for platform inspection, link resolution and the key-free translation backends. |
| [pydantic](https://github.com/pydantic/pydantic) | MIT | Configuration schema and the typed pipeline models. |
| [platformdirs](https://github.com/platformdirs/platformdirs) | MIT | Locating the per-user configuration directory. |

### 1.1 What `yt-dlp[default]` pulls in

Porter depends on `yt-dlp[default]` rather than bare `yt-dlp`, because without
the extra, downloads of several sites break. The extra is yt-dlp's own
selection, and it transitively installs:

| Component | License | Note |
| --- | --- | --- |
| [brotli](https://github.com/google/brotli) / [brotlicffi](https://github.com/python-hyper/brotlicffi) | MIT | Brotli decoding. |
| [certifi](https://github.com/certifi/python-certifi) | MPL-2.0 | CA bundle. Weak, file-level copyleft; unmodified, so no obligation beyond keeping the notice. |
| [mutagen](https://github.com/quodlibet/mutagen) | **GPL-2.0-or-later** | Media tag reading/writing. **See §5.** |
| [pycryptodomex](https://github.com/Legrandin/pycryptodome) | Public domain / BSD-2-Clause | AES decryption for some streams. |
| [urllib3](https://github.com/urllib3/urllib3) | MIT | Transport for `requests`. |
| [websockets](https://github.com/python-websockets/websockets) | BSD-3-Clause | WebSocket-based extractors. |
| [yt-dlp-ejs](https://github.com/yt-dlp/yt-dlp-ejs) | Unlicense AND MIT AND ISC | JavaScript challenge solver used by recent YouTube support. |
| [typing-extensions](https://github.com/python/typing_extensions) | PSF-2.0 | Typing backports used by transitive dependencies. |

---

## 2. Optional dependencies

Declared as extras. None of these is installed unless the user asks for it, and
none is imported unless the corresponding capability is used.

| Extra | Component | License | Used for |
| --- | --- | --- | --- |
| `llm` | [openai](https://github.com/openai/openai-python) | Apache-2.0 | Whisper API transcription and LLM-based translation/subtitle repair. |
| `llm` | [json-repair](https://github.com/mangiucugna/json_repair) | MIT | Recovering JSON from imperfect LLM output. |
| `stt` | [SpeechRecognition](https://github.com/Uberi/speech_recognition) | BSD-3-Clause | Google Web STT. Bundles a FLAC decoder under its own bundled licence (`LICENSE-FLAC.txt` in that distribution). **Measured non-functional** — see the README. |
| `asr-local` | [faster-whisper](https://github.com/SYSTRAN/faster-whisper) | MIT | Local Whisper inference with no API key and no network. |
| `images` | [pillow](https://github.com/python-pillow/Pillow) | MIT-CMU (HPND) | Cover image handling. |
| `mcp` | [fastmcp](https://github.com/jlowin/fastmcp) | Apache-2.0 | The `porter-mcp` server. |

### 2.1 What `faster-whisper` pulls in

| Component | License |
| --- | --- |
| [ctranslate2](https://github.com/OpenNMT/CTranslate2) | MIT |
| [huggingface-hub](https://github.com/huggingface/huggingface_hub) | Apache-2.0 |
| [tokenizers](https://github.com/huggingface/tokenizers) | Apache-2.0 |
| [onnxruntime](https://github.com/microsoft/onnxruntime) | MIT |
| [PyAV](https://github.com/PyAV-Org/PyAV) | BSD-3-Clause |
| [numpy](https://github.com/numpy/numpy) | BSD-3-Clause AND 0BSD AND MIT AND Zlib AND CC0-1.0 |

### 2.2 What `fastmcp` pulls in

| Component | License |
| --- | --- |
| [mcp](https://github.com/modelcontextprotocol/python-sdk) | MIT |

---

## 3. Development dependencies

Declared under the `dev` extra. Not installed by users, not shipped.

| Component | License |
| --- | --- |
| [pytest](https://github.com/pytest-dev/pytest) | MIT |
| [pytest-asyncio](https://github.com/pytest-dev/pytest-asyncio) | Apache-2.0 |
| [pytest-cov](https://github.com/pytest-dev/pytest-cov) | MIT |
| [ruff](https://github.com/astral-sh/ruff) | MIT |
| [mypy](https://github.com/python/mypy) | MIT |
| [import-linter](https://github.com/seddonym/import-linter) | BSD-2-Clause |

---

## 4. External programs (not distributed)

These are installed by the user, never bundled, and invoked only as **separate
processes**. Porter does not link against them, so their copyleft terms do not
extend to porter's source.

| Program | License | Used for |
| --- | --- | --- |
| [FFmpeg](https://ffmpeg.org/) (`ffmpeg`, `ffprobe`) | LGPL-2.1-or-later by default; GPL-2.0-or-later when built with GPL parts such as `libx264` | Standardisation, encoding, audio extraction/denoise, burning subtitles. Distributions vary — run `ffmpeg -version` to check the build you actually have. |
| [libass](https://github.com/libass/libass) | ISC | ASS/SSA subtitle rendering, used through FFmpeg's `subtitles` filter. FFmpeg must be compiled with it. |
| [Deno](https://github.com/denoland/deno) | MIT | JavaScript runtime that yt-dlp needs to solve YouTube's JS challenges. Node.js ≥ 20 (MIT) is an accepted substitute. |
| [VideoCaptioner](https://github.com/WEIFENG2333/VideoCaptioner) | **GPL-3.0** | Optional external ASR / translation CLI. **See §5.** |

---

## 5. Copyleft components — why porter stays MIT

Two components above are copyleft. Both are handled the same way, and it is not
an accident.

`mutagen` (GPL-2.0-or-later) and `VideoCaptioner` (GPL-3.0) are **separate
programs**, not part of porter's source distribution:

* **`mutagen`** arrives transitively through `yt-dlp[default]`, as its own
  installed distribution. Porter neither imports it nor ships it in its sdist or
  wheel; it runs inside the `yt-dlp` process. Porter's own code remains MIT.
  A user who wants to avoid GPL components entirely can install
  `yt-dlp` without the `default` extra, at the cost of some site support.
* **`VideoCaptioner`** is never a declared dependency and never imported. Porter
  probes for the `videocaptioner` binary at runtime and, if present, runs it in a
  subprocess and parses its output files. Its `python<3.13` pin also makes it
  unimportable in porter's own supported range, which is a second reason the
  subprocess boundary is the only option. Porter's own optional local ASR
  (`asr-local`, via `faster-whisper`) exists precisely so that key-free
  transcription is available on a permissively licensed stack instead of by
  borrowing GPL code.

`VideoCaptioner`'s design informed porter's ASR-orchestration approach. That is
an acknowledgement of ideas, not of copied code.

---

## 6. Acknowledgements

Porter stands on the shoulders of these projects, and is grateful to them:

* **[yt-dlp](https://github.com/yt-dlp/yt-dlp)** — the reason downloading from
  five platforms is a solved problem rather than five scrapers.
* **[VideoCaptioner](https://github.com/WEIFENG2333/VideoCaptioner)** — its
  sentence segmentation, alignment and subtitle-processing design shaped the
  approach taken in `porter.subtitles` and the ASR chain.
* **[FFmpeg](https://ffmpeg.org/) + [libass](https://github.com/libass/libass)** —
  the entire media layer: standardisation, denoising and professional ASS
  rendering.
* **[Pydantic](https://github.com/pydantic/pydantic)** — the typed configuration
  and model layer that keeps the pipeline honest.
* **[Model Context Protocol](https://modelcontextprotocol.io/)** and
  **[FastMCP](https://github.com/jlowin/fastmcp)** — the agent-facing frontend.

---

## 7. Verifying this list

The dependency list is generated from `pyproject.toml`, which is the single
source of truth. To audit the installed tree with licences resolved:

```bash
uv venv && uv pip install -e ".[all,dev]"
uv pip install pip-licenses
uv run pip-licenses --format=markdown --with-urls --order=license
```

`pip-licenses` reports transitive packages as resolved on your platform, which
is why the tables above name only the direct and notable transitive components
rather than pretending to be exhaustive.
