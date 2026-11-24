"""Configuration models and resolution.

Resolution order, highest priority first:

1. an explicit path (``--config``)
2. ``$PORTER_CONFIG``
3. environment variables (``OPENAI_API_KEY``, ``PORTER_ASR_ENGINE``, ...)
4. the nearest project-level ``porter.json`` (walking up from the CWD)
5. the user-level config file under :func:`platformdirs.user_config_dir`
6. built-in defaults

What this module deliberately does **not** do, unlike v0.1:

* it does not know where an agent skill is installed,
* it does not inspect for the presence of a ``SKILL.md`` to pick a write target,
* it does not read another project's config. Settings are porter's own.

Callers that need a non-standard location (an agent skill, a container) set
``$PORTER_CONFIG`` — see ``skills/porter-skill/scripts/``.
"""

from __future__ import annotations

import contextlib
import functools
import json
import os
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path
from typing import Any

import platformdirs
from pydantic import BaseModel, ConfigDict, Field, field_validator

from porter.core.errors import ConfigError
from porter.core.logging import get_logger
from porter.core.utils.text import sanitize_filename

_logger = get_logger(__name__)

__all__ = [
    "DEFAULT_VIDEO_QUALITY",
    "VIDEO_QUALITY_CHOICES",
    "ASRConfig",
    "FFmpegConfig",
    "LLMConfig",
    "PorterConfig",
    "SubtitleStyleConfig",
    "config_dir",
    "config_file",
    "default_config",
    "default_en_font",
    "default_zh_font",
    "find_project_config",
    "load_config_file",
    "mask_secret",
    "normalize_video_quality",
    "read_config_mapping",
    "resolve",
    "save_key",
    "target_resolution",
    "writable_config_path",
    "write_config_mapping",
]

APP_NAME = "porter"

#: Target video resolution anchor for URL downloads (shortest side).
#: Platforms degrade gracefully to the highest available stream below this cap.
DEFAULT_VIDEO_QUALITY = "1080p"

VIDEO_QUALITY_CHOICES: tuple[str, ...] = (
    "best",
    "2160p",
    "1440p",
    "1080p",
    "720p",
    "480p",
)

_QUALITY_ALIASES: dict[str, str] = {
    "best": "best",
    "source": "best",
    "max": "best",
    "4k": "2160p",
    "2160": "2160p",
    "2160p": "2160p",
    "2k": "1440p",
    "1440": "1440p",
    "1440p": "1440p",
    "fhd": "1080p",
    "1080": "1080p",
    "1080p": "1080p",
    "hd": "720p",
    "720": "720p",
    "720p": "720p",
    "480": "480p",
    "480p": "480p",
}


def normalize_video_quality(value: str | int | None) -> str:
    """Normalise a user-supplied video quality string to a canonical choice."""
    if value is None:
        return DEFAULT_VIDEO_QUALITY
    text = str(value).strip().lower()
    if not text:
        return DEFAULT_VIDEO_QUALITY
    canonical = _QUALITY_ALIASES.get(text)
    if canonical is None:
        choices = ", ".join(VIDEO_QUALITY_CHOICES)
        raise ValueError(f"invalid video quality {value!r}; choose from {choices}")
    return canonical


def target_resolution(quality: str) -> int | None:
    """Short-side pixel target for ``quality``, or ``None`` for ``best``."""
    canonical = normalize_video_quality(quality)
    if canonical == "best":
        return None
    return int(canonical[:-1])

#: Recognised file names for project-level config, in priority order.
PROJECT_CONFIG_NAMES = ("porter.json", "porter.toml", "config.json")

#: Legacy name from v0.1, still honoured so upgrades do not lose settings.
LEGACY_CONFIG_NAMES = ("porter_config.json",)

ENV_CONFIG_PATH = "PORTER_CONFIG"


class LLMConfig(BaseModel):
    """LLM used for subtitle semantic correction and translation."""

    model_config = ConfigDict(extra="ignore")

    api_key: str | None = None
    api_base: str | None = None
    model: str = "deepseek-chat"


class RefineConfig(BaseModel):
    """Transcript refinement settings (ASR correction, punctuation, homophones)."""

    model_config = ConfigDict(extra="ignore")

    enabled: bool = True
    model: str | None = None


class ASRConfig(BaseModel):
    """Speech-to-text settings. ``engine=None`` means "walk the fallback chain"."""

    model_config = ConfigDict(extra="ignore")

    engine: str | None = None
    language: str = "auto"
    whisper_api_key: str | None = None
    whisper_api_base: str | None = None
    whisper_model: str = "whisper-1"
    #: Local Whisper (``[asr-local]``). ``model`` is a faster-whisper size
    #: (``base``/``small``/``medium``/``large-v3``) or a Hugging Face repo id for
    #: a fine-tune. ``device``/``compute_type`` accept ``auto``.
    whisper_local_model: str = "small"
    whisper_local_device: str = "auto"
    whisper_local_compute_type: str = "auto"
    audio_denoise: bool = True


class FFmpegConfig(BaseModel):
    """FFmpeg/ffprobe invocation and encoding parameters."""

    model_config = ConfigDict(extra="ignore")

    ffmpeg_path: str = "ffmpeg"
    ffprobe_path: str = "ffprobe"
    video_codec: str = "libx264"
    preset: str = "veryfast"
    crf: int = 18
    pixel_format: str = "yuv420p"
    audio_codec: str = "aac"
    audio_bitrate: str = "192k"
    audio_sample_rate: int = 44100
    wav_sample_rate: int = 16000
    #: Probe the host and tune preset/CRF to the detected hardware tier.
    auto_tune: bool = True


@functools.lru_cache(maxsize=4)
def _fc_match_pattern(pattern: str) -> str | None:
    """Resolve a fontconfig pattern to its primary family name, or None."""
    binary = shutil.which("fc-match")
    if binary is None:
        return None
    try:
        proc = subprocess.run(  # noqa: S603 - resolved binary with constant pattern
            [binary, "-f", "%{family}", pattern],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=2.0,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    raw = proc.stdout.strip()
    if not raw:
        return None
    return raw.split(",", 1)[0].strip() or None


def default_zh_font() -> str:
    """Return a cross-platform default Chinese sans-serif font family.

    Avoids hardcoding Windows-only ``Microsoft YaHei`` on Linux/macOS where
    fontconfig would substitute a monospace or Latin-only font and render
    Chinese subtitles as empty boxes.
    """
    if sys.platform == "win32":
        return "Microsoft YaHei"
    if sys.platform == "darwin":
        return "PingFang SC"
    return _fc_match_pattern(":lang=zh-cn:family=sans-serif") or "Noto Sans CJK SC"


def default_en_font() -> str:
    """Return a cross-platform default Latin sans-serif font family."""
    if sys.platform == "win32":
        return "Arial"
    if sys.platform == "darwin":
        return "Helvetica Neue"
    return _fc_match_pattern(":lang=en:family=sans-serif") or "Noto Sans"


class SubtitleStyleConfig(BaseModel):
    """ASS styling. Field names match v0.1 ``config.json`` keys."""

    model_config = ConfigDict(extra="ignore")

    zh_font: str = Field(default_factory=default_zh_font)
    en_font: str = Field(default_factory=default_en_font)
    zh_font_size: int = 52
    en_font_size: int = 34
    zh_primary_color: str = "&H00FFFFFF"
    en_primary_color: str = "&H0000FFFF"
    outline_color: str = "&H00000000"
    outline_width: float = 3.5
    shadow: float = 1.5
    margin_v: int = 35
    margin_l: int = 20
    margin_r: int = 20
    bilingual_zh_margin_v: int = 90
    bilingual_en_margin_v: int = 35
    fade_in_ms: int = 120
    fade_out_ms: int = 120


class PorterConfig(BaseModel):
    """Fully resolved engine configuration."""

    model_config = ConfigDict(arbitrary_types_allowed=True, extra="ignore")

    llm: LLMConfig = Field(default_factory=LLMConfig)
    refine: RefineConfig = Field(default_factory=RefineConfig)
    asr: ASRConfig = Field(default_factory=ASRConfig)
    ffmpeg: FFmpegConfig = Field(default_factory=FFmpegConfig)
    style: SubtitleStyleConfig = Field(default_factory=SubtitleStyleConfig)
    output_dir: Path = Path("./porter_output")
    video_quality: str = DEFAULT_VIDEO_QUALITY
    cookies_file: Path | None = None
    cookies_browser: str | None = None
    #: Where the values came from, for ``porter config list`` and bug reports.
    source: str = "built-in defaults"

    @field_validator("video_quality", mode="before")
    @classmethod
    def _validate_video_quality(cls, value: Any) -> str:
        return normalize_video_quality(value)

    @field_validator("cookies_file", "cookies_browser", mode="before")
    @classmethod
    def _empty_string_to_none(cls, value: Any) -> Any:
        # Empty strings in JSON (e.g. saved by an older UI form) must never
        # become ``Path('.')`` — passing ``cookiefile='.'`` makes every yt-dlp
        # invocation fail with IsADirectoryError.
        if isinstance(value, str) and not value.strip():
            return None
        return value

    def masked(self) -> dict[str, Any]:
        """``model_dump`` with every secret replaced by a masked preview."""
        data = self.model_dump(mode="json")
        data["llm"]["api_key"] = mask_secret(self.llm.api_key)
        data["asr"]["whisper_api_key"] = mask_secret(self.asr.whisper_api_key)
        return data


# ---------------------------------------------------------------------------
# Locations
# ---------------------------------------------------------------------------


def config_dir() -> Path:
    """User-level configuration directory (``platformdirs``)."""
    return Path(platformdirs.user_config_dir(APP_NAME, appauthor=False))


def config_file() -> Path:
    """Default user-level configuration file path."""
    return config_dir() / "config.json"


def find_project_config(start: Path | None = None) -> Path | None:
    """Walk up from ``start`` looking for a project-level config file.

    Stops at the filesystem root. Never crosses into a parent that is not part
    of the project — there is no such concept as "the project" here, so the
    nearest file simply wins.
    """
    current = (start or Path.cwd()).resolve()
    names = (*PROJECT_CONFIG_NAMES, *LEGACY_CONFIG_NAMES)
    for directory in (current, *current.parents):
        for name in names:
            candidate = directory / name
            if candidate.is_file():
                return candidate
    return None


def load_config_file(path: Path) -> dict[str, Any]:
    """Read a JSON or TOML config file.

    Raises:
        ConfigError: the file exists but cannot be parsed. A malformed config is
            a hard error — silently ignoring it would make misconfiguration
            invisible.
    """
    try:
        if path.suffix.lower() == ".toml":
            with path.open("rb") as handle:
                return dict(tomllib.load(handle))
        with path.open(encoding="utf-8") as handle:
            data = json.load(handle)
    except json.JSONDecodeError as exc:
        raise ConfigError(f"invalid JSON in {path}: {exc}", path=str(path)) from exc
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"invalid TOML in {path}: {exc}", path=str(path)) from exc
    except OSError as exc:
        raise ConfigError(f"cannot read {path}: {exc}", path=str(path)) from exc

    if not isinstance(data, dict):
        raise ConfigError(f"{path} must contain a mapping at the top level", path=str(path))
    return data


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------


def _first_env(*names: str) -> str | None:
    for name in names:
        value = os.environ.get(name)
        if value:
            return value
    return None


def default_config() -> PorterConfig:
    """Built-in defaults with environment-variable overrides applied."""
    return _from_mapping({}, source="built-in defaults / environment")


def resolve(explicit: Path | str | None = None) -> PorterConfig:
    """Resolve the effective configuration.

    Args:
        explicit: Path from ``--config``. Must exist; a missing explicit path is
            an error rather than a silent fallback, because the user clearly
            intended to use it.
    """
    if explicit is not None:
        path = Path(explicit).expanduser()
        if not path.is_file():
            raise ConfigError(f"config file not found: {path}", path=str(path))
        return _from_mapping(load_config_file(path), source=str(path))

    env_path = os.environ.get(ENV_CONFIG_PATH)
    if env_path:
        path = Path(env_path).expanduser()
        if not path.is_file():
            raise ConfigError(
                f"${ENV_CONFIG_PATH} points at a missing file: {path}", path=str(path)
            )
        return _from_mapping(load_config_file(path), source=str(path))

    project = find_project_config()
    if project is not None:
        return _from_mapping(load_config_file(project), source=str(project))

    user = config_file()
    if user.is_file():
        return _from_mapping(load_config_file(user), source=str(user))

    return default_config()


def _from_mapping(data: dict[str, Any], *, source: str) -> PorterConfig:
    """Merge a raw mapping with environment overrides into a PorterConfig."""
    llm_raw = data.get("llm") or {}
    refine_raw = data.get("refine") or {}
    asr_raw = data.get("asr") or {}
    ffmpeg_raw = data.get("ffmpeg") or {}
    style_raw = data.get("style") or {}

    api_key = _first_env("OPENAI_API_KEY") or llm_raw.get("api_key")
    api_base = _first_env("OPENAI_BASE_URL", "OPENAI_API_BASE") or llm_raw.get("api_base")
    model = _first_env("OPENAI_MODEL", "PORTER_LLM_MODEL") or llm_raw.get("model")

    llm = LLMConfig(
        api_key=api_key,
        api_base=api_base,
        model=model or "deepseek-chat",
    )
    refine = RefineConfig(
        enabled=refine_raw.get("enabled", True),
        model=refine_raw.get("model"),
    )

    asr = ASRConfig(
        engine=_first_env("PORTER_ASR_ENGINE") or asr_raw.get("engine") or None,
        language=asr_raw.get("language", "auto"),
        # Whisper falls back to the LLM credentials when not set separately.
        whisper_api_key=(
            _first_env("WHISPER_API_KEY") or asr_raw.get("whisper_api_key") or llm.api_key
        ),
        whisper_api_base=(
            _first_env("WHISPER_API_BASE") or asr_raw.get("whisper_api_base") or llm.api_base
        ),
        whisper_model=_first_env("WHISPER_MODEL") or asr_raw.get("whisper_model") or "whisper-1",
        whisper_local_model=(
            _first_env("PORTER_ASR_LOCAL_MODEL")
            or asr_raw.get("whisper_local_model")
            or "small"
        ),
        whisper_local_device=(
            _first_env("PORTER_ASR_LOCAL_DEVICE")
            or asr_raw.get("whisper_local_device")
            or "auto"
        ),
        whisper_local_compute_type=(
            _first_env("PORTER_ASR_LOCAL_COMPUTE_TYPE")
            or asr_raw.get("whisper_local_compute_type")
            or "auto"
        ),
        audio_denoise=asr_raw.get("audio_denoise", True),
    )

    ffmpeg = FFmpegConfig(
        **{
            key: value
            for key, value in ffmpeg_raw.items()
            if key in FFmpegConfig.model_fields
        }
    )
    style = SubtitleStyleConfig(
        **{key: value for key, value in style_raw.items() if key in SubtitleStyleConfig.model_fields}
    )

    output_dir = _first_env("PORTER_OUTPUT_DIR") or data.get("output_dir") or "./porter_output"
    video_quality = (
        _first_env("PORTER_VIDEO_QUALITY")
        or data.get("video_quality")
        or DEFAULT_VIDEO_QUALITY
    )
    cookies_file = data.get("cookies_file") or data.get("cookies")
    cookies_browser = data.get("cookies_browser") or data.get("cookies_from_browser")

    return PorterConfig(
        llm=llm,
        refine=refine,
        asr=asr,
        ffmpeg=ffmpeg,
        style=style,
        output_dir=Path(str(output_dir)),
        video_quality=normalize_video_quality(video_quality),
        cookies_file=Path(str(cookies_file)) if cookies_file else None,
        cookies_browser=str(cookies_browser) if cookies_browser else None,
        source=source,
    )


# ---------------------------------------------------------------------------
# Mutation
# ---------------------------------------------------------------------------


def writable_config_path() -> Path:
    """Resolve the JSON config file frontends and skills should edit.

    Honours ``$PORTER_CONFIG`` first (when it points at JSON), then the nearest
    project-level JSON config, falling back to the user-level ``config.json``.
    TOML files are never overwritten with JSON.
    """
    env_path = os.environ.get(ENV_CONFIG_PATH, "").strip()
    if env_path:
        candidate = Path(env_path).expanduser()
        if candidate.suffix.lower() != ".toml":
            return candidate
        _logger.warning(
            "$%s points at TOML (%s); falling back to user JSON config",
            ENV_CONFIG_PATH,
            candidate,
        )

    found = find_project_config()
    if found is not None:
        if found.suffix.lower() == ".json":
            return found
        _logger.warning(
            "project config %s is not JSON; falling back to user JSON config",
            found,
        )
    return config_file()


def read_config_mapping(target: Path | None = None) -> tuple[dict[str, Any], Path]:
    """Read the writable JSON config as a mapping, or return built-in defaults."""
    dest = Path(target) if target else writable_config_path()
    if dest.is_file():
        try:
            return load_config_file(dest), dest
        except (ConfigError, OSError) as exc:
            _logger.warning("Failed to parse config %s: %s", dest, exc)
    return default_config().model_dump(mode="json"), dest


def write_config_mapping(data: dict[str, Any], target: Path | None = None) -> Path:
    """Write ``data`` as UTF-8 JSON with owner-only permissions where supported."""
    dest = Path(target) if target else writable_config_path()
    dest.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(data, indent=2, ensure_ascii=False) + "\n"
    dest.write_text(text, encoding="utf-8")
    with contextlib.suppress(OSError):
        dest.chmod(0o600)
    return dest


def save_key(key_path: str, value: Any, target: Path | None = None) -> Path:
    """Set a dotted key (``llm.api_key``) in the target config file.

    Creates the file and its parents as needed. Defaults to the user-level
    config file so that a ``porter config set`` never depends on the CWD.
    """
    destination = Path(target) if target else config_file()
    if destination.suffix.lower() == ".toml":
        raise ConfigError(
            f"cannot edit TOML config {destination} automatically; edit it manually",
            path=str(destination),
        )
    destination.parent.mkdir(parents=True, exist_ok=True)

    data: dict[str, Any] = {}
    if destination.is_file():
        data = load_config_file(destination)

    parts = [part for part in key_path.split(".") if part]
    if not parts:
        raise ConfigError("empty configuration key", key=key_path)

    cursor: dict[str, Any] = data
    for part in parts[:-1]:
        node = cursor.get(part)
        if not isinstance(node, dict):
            node = {}
            cursor[part] = node
        cursor = node
    cursor[parts[-1]] = value

    try:
        destination.write_text(
            json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
    except OSError as exc:
        raise ConfigError(f"cannot write {destination}: {exc}", path=str(destination)) from exc

    return destination


def mask_secret(secret: str | None) -> str:
    """Render a secret for display: ``sk-...wxyz`` or ``<not set>``.

    Shows the first three and last four characters, which is enough to tell two
    keys apart without revealing either.
    """
    if not secret:
        return "<not set>"
    if len(secret) <= 8:
        return "***"
    return f"{secret[:3]}...{secret[-4:]}"


def task_dir_name(video_id: str, title: str) -> str:
    """Directory name for one task: ``<video_id>_<safe_title>``."""
    return f"{video_id}_{sanitize_filename(title)}"
