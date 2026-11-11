# porter 配置参考

## 搜索顺序

从高到低：

1. **`--config <path>`**——文件不存在会报错，不会静默回退
2. **`$PORTER_CONFIG`**——同样要求文件存在
3. **环境变量**——覆盖所选文件中的对应键
4. **最近的项目配置**——从当前目录向上查找 `porter.json`、`porter.toml`、
   `config.json`；旧文件名 `porter_config.json` 仍被识别
5. **用户配置**——`platformdirs` 的用户配置目录（仅当没有项目配置时）
6. **内置默认值**

项目配置与用户配置是**二选一**，不是深合并：找到项目配置后，用户配置不再参与。
环境变量再覆盖所选文件里的具体键。`porter config list` 的 `source` 字段就是
“这些值从哪来”的答案。

---

## 环境变量

| 变量 | 覆盖 |
|---|---|
| `OPENAI_API_KEY` | `llm.api_key`，并作为 `asr.whisper_api_key` 的兜底 |
| `OPENAI_BASE_URL` / `OPENAI_API_BASE` | `llm.api_base` |
| `OPENAI_MODEL` / `PORTER_LLM_MODEL` | `llm.model` |
| `WHISPER_API_KEY` | `asr.whisper_api_key` |
| `WHISPER_API_BASE` | `asr.whisper_api_base` |
| `WHISPER_MODEL` | `asr.whisper_model` |
| `PORTER_ASR_ENGINE` | `asr.engine` |
| `PORTER_ASR_LOCAL_MODEL` | `asr.whisper_local_model` |
| `PORTER_ASR_LOCAL_DEVICE` | `asr.whisper_local_device` |
| `PORTER_ASR_LOCAL_COMPUTE_TYPE` | `asr.whisper_local_compute_type` |
| `PORTER_OUTPUT_DIR` | `output_dir` |
| `PORTER_CONFIG` | 显式配置文件路径 |
| `PORTER_LOG_LEVEL` | CLI 日志级别（仅当未传 `--log-level`） |
| `PORTER_MIRROR` | 下载镜像策略：`cn` 强制启用、`off` 强制关闭 |
| `PORTER_CACHE_DIR` | 作业注册表所在目录（受限宿主/测试用） |

**把密钥放环境变量比放配置文件安全**——配置文件可能被提交进仓库。MCP 侧的 `porter_config` 只读且密钥打码，**无法通过 MCP 写入密钥**（否则密钥会进入对话记录与遥测）。

---

## 全部配置键

### 顶层

| 键 | 默认 | 说明 |
|---|---|---|
| `output_dir` | `./porter_output` | 每个作业在其中建一个任务目录 |
| `cookies_file` | `null` | Netscape 格式 `cookies.txt`，供需要登录的视频 |
| `cookies_browser` | `null` | 从浏览器配置读 cookie：`chrome` / `firefox` / `edge` / `brave` 等 |

### `llm`

用于字幕语义纠错与翻译。**不配也能跑**——翻译会走免 Key 的 `bing` / `google` / `mymemory`。

| 键 | 默认 | 说明 |
|---|---|---|
| `api_key` | `null` | OpenAI 兼容的 Key |
| `api_base` | `null` | 兼容端点，如 `https://api.deepseek.com/v1` |
| `model` | `deepseek-chat` | 模型名 |

### `asr`

| 键 | 默认 | 说明 |
|---|---|---|
| `engine` | `null` | `null` = 走回退链；也可强制 `whisper-local` / `whisper-api` / `videocaptioner` |
| `language` | `auto` | 源语言 |
| `whisper_api_key` | `null` | 未设时回落到 `llm.api_key` |
| `whisper_api_base` | `null` | 未设时回落到 `llm.api_base` |
| `whisper_model` | `whisper-1` | |
| `whisper_local_model` | `small` | 本地 Whisper 模型尺寸（`tiny` / `base` / `small` / `medium` / `large-v3` 或 HuggingFace 模型 ID） |
| `whisper_local_device` | `auto` | 计算设备（`auto` / `cpu` / `cuda`） |
| `whisper_local_compute_type` | `auto` | 计算精度（`auto` / `int8` / `float16` 等） |
| `audio_denoise` | `true` | 是否生成 ASR 专用增强音轨（不影响成片音质） |

`whisper-local` 需要 `[asr-local]` extra：免 Key，首次下载模型后可离线。`whisper-api`
需要 OpenAI 兼容 Key；`videocaptioner` 需要用户自行安装外部 CLI。

### `refine`

转录后字幕文本纠错与语义精炼（标点修复、同音字校对）。

| 键 | 默认 | 说明 |
|---|---|---|
| `enabled` | `true` | 是否启用 LLM 语义校对 |
| `model` | `null` | 指定精炼模型（未指定时继承 `llm.model`） |

### `ffmpeg`

| 键 | 默认 | 说明 |
|---|---|---|
| `ffmpeg_path` | `ffmpeg` | 可执行文件路径 |
| `ffprobe_path` | `ffprobe` | |
| `video_codec` | `libx264` | 仅作为**回退**：压制阶段自己探测硬件编码器 |
| `preset` | `veryfast` | `auto_tune=true` 时会按硬件分级调整 |
| `crf` | `18` | |
| `pixel_format` | `yuv420p` | |
| `audio_codec` | `aac` | 成片音频实际是**直通复制**，此项仅用于需要重编码时 |
| `audio_bitrate` | `192k` | |
| `audio_sample_rate` | `44100` | |
| `wav_sample_rate` | `16000` | 提取给 ASR 的 WAV 采样率 |
| `auto_tune` | `true` | 按探测到的硬件分级自动调 preset / CRF |

### `style`

ASS 字幕样式。字号单位是**视频自身分辨率下的像素**；竖屏视频会被识别，边距自适应。

| 键 | 默认 |
|---|---|
| `zh_font` | `Microsoft YaHei` |
| `en_font` | `Arial` |
| `zh_font_size` | `52` |
| `en_font_size` | `34` |
| `zh_primary_color` | `&H00FFFFFF` |
| `en_primary_color` | `&H0000FFFF` |
| `outline_color` | `&H00000000` |
| `outline_width` | `3.5` |
| `shadow` | `1.5` |
| `margin_v` | `35` |
| `margin_l` / `margin_r` | `20` |
| `bilingual_zh_margin_v` | `90` |
| `bilingual_en_margin_v` | `35` |
| `fade_in_ms` / `fade_out_ms` | `120` |

颜色是 ASS 的 `&HAABBGGRR` 十六进制（**注意字节序是 ABGR 而非 RGB**）。

---

## 模板

`../assets/config.example.json` 是带注释的完整模板。复制到项目根目录命名为 `porter.json` 即可生效。

---

## 常见配置

```jsonc
// 用 DeepSeek 做翻译（并顺带作为 Whisper 的兜底 Key）
{
  "llm": {
    "api_key": "sk-...",
    "api_base": "https://api.deepseek.com/v1",
    "model": "deepseek-chat"
  }
}
```

```jsonc
// 只要更快出片：跳过人声增强，强制软件编码
{
  "asr": { "audio_denoise": false },
  "ffmpeg": { "auto_tune": false, "preset": "veryfast" }
}
```

```jsonc
// 竖屏视频把中文字幕抬高一点
{
  "style": { "margin_v": 55, "bilingual_zh_margin_v": 120 }
}
```

---

## 排查

```bash
porter config list          # 值从哪来（含实际配置文件路径）
porter doctor               # 这台机器缺什么能力
porter config get llm       # 看某一段（密钥已打码）
```

配置解析失败时 CLI 会以非零退出码报错并说明是哪个文件、哪一行——不会静默用默认值继续跑。
