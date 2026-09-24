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
* it does not silently read another project's config (``videocaptioner``'s
  ``config.toml``). That is available explicitly via
  ``porter config import-videocaptioner``.

Callers that need a non-standard location (an agent skill, a container) set
``$PORTER_CONFIG`` — see ``skills/porter-skill/scripts/``.
"""

from __future__ import annotations

import json
import os
import tomllib
from pathlib import Path
from typing import Any

import platformdirs
from pydantic import BaseModel, ConfigDict, Field

from porter.errors import ConfigError
from porter.utils.text import sanitize_filename

__all__ = [
    "ASRConfig",
    "FFmpegConfig",
    "LLMConfig",
    "PorterConfig",
    "SubtitleStyleConfig",
    "config_dir",
    "config_file",
    "default_config",
    "find_project_config",
    "load_config_file",
    "mask_secret",
    "resolve",
    "save_key",
]

APP_NAME = "porter"

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


class SubtitleStyleConfig(BaseModel):
    """ASS styling. Field names match v0.1 ``config.json`` keys."""

    model_config = ConfigDict(extra="ignore")

    zh_font: str = "Microsoft YaHei"
    en_font: str = "Arial"
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
    asr: ASRConfig = Field(default_factory=ASRConfig)
    ffmpeg: FFmpegConfig = Field(default_factory=FFmpegConfig)
    style: SubtitleStyleConfig = Field(default_factory=SubtitleStyleConfig)
    output_dir: Path = Path("./porter_output")
    cookies_file: Path | None = None
    cookies_browser: str | None = None
    #: Where the values came from, for ``porter config list`` and bug reports.
    source: str = "built-in defaults"

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
    cookies_file = data.get("cookies_file") or data.get("cookies")
    cookies_browser = data.get("cookies_browser") or data.get("cookies_from_browser")

    return PorterConfig(
        llm=llm,
        asr=asr,
        ffmpeg=ffmpeg,
        style=style,
        output_dir=Path(str(output_dir)),
        cookies_file=Path(str(cookies_file)) if cookies_file else None,
        cookies_browser=str(cookies_browser) if cookies_browser else None,
        source=source,
    )


# ---------------------------------------------------------------------------
# Mutation
# ---------------------------------------------------------------------------


def save_key(key_path: str, value: Any, target: Path | None = None) -> Path:
    """Set a dotted key (``llm.api_key``) in the target config file.

    Creates the file and its parents as needed. Defaults to the user-level
    config file so that a ``porter config set`` never depends on the CWD.
    """
    destination = Path(target) if target else config_file()
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
