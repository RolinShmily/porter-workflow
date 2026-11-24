# 配置键与优先级

配置由 `porter.core.config.resolve()` 统一解析，优先级从高到低：

1. `--config <path>` 显式指定的文件
2. 环境变量 `$PORTER_CONFIG`
3. 环境变量（覆盖所选配置文件中的对应字段）
4. 最近的项目级配置：`porter.json` → `porter.toml` → `config.json`
5. 用户级配置目录：`platformdirs.user_config_dir("porter")` 下的 `config.json`
6. 内置默认值

## 常用配置命令

```bash
python3 <SKILL_DIR>/scripts/porter_cli.py config path
python3 <SKILL_DIR>/scripts/porter_cli.py config show                # 密钥自动脱敏
python3 <SKILL_DIR>/scripts/porter_cli.py config set video_quality=1080p
python3 <SKILL_DIR>/scripts/porter_cli.py config set cookies_browser=firefox
python3 <SKILL_DIR>/scripts/porter_cli.py config set llm.model=deepseek-chat
```

`config set` 写入 `porter.writable_config_path()`（优先 `$PORTER_CONFIG` 的 JSON，
其次项目级 JSON，最后用户级 `config.json`；绝不覆写 TOML）。

## 主要配置键

| 配置段 | 键 | 默认值 | 说明 |
|---|---|---|---|
| 顶层 | `output_dir` | `./porter_output` | 任务输出根目录（env: `PORTER_OUTPUT_DIR`） |
| 顶层 | `video_quality` | `1080p` | 目标画质：`best` / `2160p` / `1440p` / `1080p` / `720p` / `480p`（env: `PORTER_VIDEO_QUALITY`） |
| 顶层 | `cookies_file` | `null` | Netscape `cookies.txt` 路径 |
| 顶层 | `cookies_browser` | `null` | 浏览器名（`chrome` / `firefox` / `edge` 等） |
| `llm` | `api_key` / `api_base` / `model` | `null` / `null` / `deepseek-chat` | 显式选择 `translate.llm` 时使用（env: `OPENAI_API_KEY`、`OPENAI_BASE_URL`、`OPENAI_MODEL`） |
| `asr` | `engine` | `null` | 自动选择 ASR 时的偏好（非付费授权） |
| `asr` | `language` | `auto` | 识别源语言 |
| `asr` | `audio_denoise` | `true` | 是否生成降噪音轨用于 ASR |
| `asr` | `whisper_local_model` | `small` | 本地 `faster-whisper` 模型大小（env: `PORTER_ASR_LOCAL_MODEL`） |
| `asr` | `whisper_local_device` | `auto` | `auto`（先 CUDA 后 CPU）/ `cuda` / `cpu` |
| `asr` | `whisper_local_compute_type` | `auto` | `auto` / `float16` / `int8` |
| `asr` | `whisper_api_key` / `whisper_api_base` / `whisper_model` | 回退 `llm` / `whisper-1` | 付费 Whisper API（env: `WHISPER_API_KEY`） |
| `ffmpeg` | `ffmpeg_path` / `ffprobe_path` / `video_codec` / `preset` / `crf` / `auto_tune` | `ffmpeg` / `ffprobe` / `libx264` / `veryfast` / `18` / `true` | 编码与硬件加速自动调优 |
| `style` | `zh_font` / `en_font` / `zh_font_size` / `en_font_size` / `bilingual_*_margin_v` | 跨平台默认无衬线字体 | ASS 字幕排版 |
