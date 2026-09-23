# 配置参考（贡献者版）

本文面向**改引擎的人**：它记录配置的解析顺序、每个键的权威定义位置、以及哪些键实际被谁消费。

只想把 porter 跑起来的用户请读 `skills/porter-skill/references/CONFIG.md`——那份是面向 agent 的操作说明。本文不重复它的内容，而是回答它不必回答的问题：**这个键是谁读的、没被读的键有哪些、以及"看起来能覆盖"的东西为什么不生效。**

所有键的唯一权威定义在 `src/porter/config.py`。任何与本文件不一致的说法，以 `config.py` 为准；发现不一致请直接改 `config.py` 或本文件，不要两边都留着。

---

## 1. 五个模型

`src/porter/config.py` 定义五个 `pydantic.BaseModel`，全部 `model_config = ConfigDict(extra="ignore")`：

| 模型 | 用途 | 在 `PorterConfig` 中的键 |
|---|---|---|
| `LLMConfig` | 字幕语义纠错与句子级翻译 | `llm` |
| `ASRConfig` | 语音识别后端与音频预处理 | `asr` |
| `FFmpegConfig` | ffmpeg/ffprobe 路径与编码参数 | `ffmpeg` |
| `SubtitleStyleConfig` | ASS 字幕样式 | `style` |
| `PorterConfig` | 顶层，聚合上面四个 | — |

> **`extra="ignore"` 是全局策略，也是一处陷阱。** 拼错的键、写错层级的键、以及属于旧版本的键，**一律被静默丢弃，不报错**。`porter config list` 只打印解析后的结果，因此"我明明配了却没生效"最常见的成因就是一个被 `ignore` 掉的键名。写配置时请以 §4 的表格逐字核对。

`PorterConfig` 额外有两个非嵌套键和一段元数据：

| 键 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `output_dir` | `Path` | `./porter_output` | 每个作业在其中建一个任务目录 |
| `cookies_file` | `Path \| None` | `None` | Netscape 格式 cookie 文件 |
| `cookies_browser` | `str \| None` | `None` | 从浏览器配置读 cookie |
| `source` | `str` | `"built-in defaults"` | **由解析器写入**，记录值来自哪个文件；不是用户可配的项 |

`source` 是 `porter config list` 里 `Source:` 那一行、以及 `porter config path` 输出的全部内容。它存在的理由很实际：报告配置问题时第一步就是确认"到底读了哪个文件"。

---

## 2. 解析顺序

### 2.1 文件选择：排他的，不合并

`resolve(explicit)` 的行为是**选中唯一一个来源**，而不是把多层配置合并：

| 优先级 | 来源 | 落空时的行为 |
|---|---|---|
| 1 | `--config <path>` 显式路径 | 文件不存在 → `ConfigError`（**不**回退） |
| 2 | 环境变量 `$PORTER_CONFIG` | 文件不存在 → `ConfigError`（**不**回退） |
| 3 | 项目配置（从 CWD 向上逐级查找） | 没找到就继续 |
| 4 | 用户配置 `platformdirs.user_config_dir("porter", appauthor=False)/config.json` | 不存在就继续 |
| 5 | 内置默认值 | 终点 |

第 1、2 条刻意不回退：用户明确指定了路径，读不到就是错，静默改用别的文件会让"我配了却不生效"变成一个无解的问题。

**这是最容易误读的一处：项目配置不是"叠加在用户配置之上"，而是整个替换掉它。** 一个项目根目录里只写了两行 `style` 的 `porter.json`，会让用户配置里的 `llm` 段整段失效。想让两者共存只能在项目文件里把需要的键抄全，或改用环境变量。

`find_project_config()` 从 `Path.cwd()` 起逐级向上，在**每个目录**里按以下顺序探测，命中即返回：

```
porter.json → porter.toml → config.json → porter_config.json
```

它不会停在仓库边界（没有 `.git` 检测），一路走到文件系统根。因此在家目录放一个 `porter.json` 会影响到家目录下的每一个项目——这是有意的简单规则，知道就好。

> `porter_config.json` 是 v0.1 的旧文件名，仍然被识别，以免升级丢配置。`.toml` 只在项目级被识别；用户级固定是 `config.json`，但 `--config` / `$PORTER_CONFIG` 指向 `.toml` 也可以。

### 2.2 环境变量：在文件之后应用

选定文件后，`_from_mapping()` 会把环境变量**覆盖在文件值之上**：

```python
api_key = _first_env("OPENAI_API_KEY") or llm_raw.get("api_key")
```

所以真实的有效优先级是：

```
内置默认值  <  所选文件的键值  <  环境变量  <  显式传入的 CLI flag（见 §6）
```

环境变量**高于项目配置**。这一点与"默认值 → 环境变量 → 用户配置 → 项目配置"这种直觉顺序不同，而它是刻意的：把密钥放进环境变量是推荐做法，推荐做法不应该被一个仓库里的文件压过去。它的代价是"项目文件里明确写了这个值却没生效"——排查时先看 `porter config list` 里的 `Source:` 再怀疑环境变量。

`_first_env()` 只返回**非空**值（`if value:`），所以 `OPENAI_API_KEY=""`（空串）等同于未设置。

### 2.3 解析失败一定是硬错误

`load_config_file()` 对以下情况抛 `ConfigError`，绝不静默降级：

- JSON 语法错误（`json.JSONDecodeError`）
- TOML 语法错误（`tomllib.TOMLDecodeError`）
- 文件读不了（`OSError`）
- 顶层不是 mapping

一个坏掉的配置文件如果被忽略，用户看到的是"配置没生效"而不是"配置写错了"，后者才是可修的。

---

## 3. 环境变量

`_first_env` 的权威调用点全部在 `src/porter/config.py`：

| 变量 | 覆盖 | 备注 |
|---|---|---|
| `OPENAI_API_KEY` | `llm.api_key` | 同时是 `asr.whisper_api_key` 的兜底来源 |
| `OPENAI_BASE_URL` | `llm.api_base` | |
| `OPENAI_API_BASE` | `llm.api_base` | 与上一项同义，按此顺序取 |
| `OPENAI_MODEL` | `llm.model` | 优先于 `PORTER_LLM_MODEL` |
| `PORTER_LLM_MODEL` | `llm.model` | |
| `PORTER_ASR_ENGINE` | `asr.engine` | |
| `WHISPER_API_KEY` | `asr.whisper_api_key` | 优先于 `llm.api_key` 兜底 |
| `WHISPER_API_BASE` | `asr.whisper_api_base` | |
| `WHISPER_MODEL` | `asr.whisper_model` | |
| `PORTER_OUTPUT_DIR` | `output_dir` | |

不经过 `_first_env`、但代码确实会读的环境变量：

| 变量 | 读取点 | 作用 |
|---|---|---|
| `PORTER_CONFIG` | `config.resolve()` | 显式配置文件路径（§2.1） |
| `PORTER_LOG_LEVEL` | `porter/logging.py:resolve_level()` | 引擎日志级别，默认 `INFO` |
| `XDG_CACHE_HOME` 等 | 间接经 `platformdirs` | 作业注册表所在目录（见 §6） |

`src/porter/translate/llm.py` 与 `src/porter/asr/whisper_api.py` 各自还有一处 `os.environ.get("OPENAI_API_KEY"/"OPENAI_BASE_URL")` 兜底。这两处是**防御性二次读取**：`available()` 探测可能在 `RunContext` 之外被调用（例如 `porter doctor` 的能力探测），此时 `ctx.config` 未必已经解析过。正常情况下配置里的值已经等价，读到的是同一个数。

**没有** `PORTER_` 前缀的 `PORTER_LLM_*` / `PORTER_ASR_*` 之外的别名，也没有 `PORTER_FFMPEG_*`。ffmpeg 路径、样式、`cookies_file` 这些**只能通过配置文件设置**，没有环境变量入口。

---

## 4. 全部配置键

以下每张表都直接对应 `config.py` 中的字段定义。类型取自注解。

### 4.1 `llm`

用于字幕语义纠错与句子级翻译。不配也能跑：翻译链会退到免 Key 的后端。

| 键 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `api_key` | `str \| None` | `None` | OpenAI 兼容 Key |
| `api_base` | `str \| None` | `None` | 兼容端点，如 `https://api.deepseek.com/v1` |
| `model` | `str` | `deepseek-chat` | 模型名 |

### 4.2 `asr`

| 键 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `engine` | `str \| None` | `None` | `None` = 走回退链 |
| `language` | `str` | `auto` | 传给后端的源语言 |
| `whisper_api_key` | `str \| None` | `None` | 未设时回落 `llm.api_key` |
| `whisper_api_base` | `str \| None` | `None` | 未设时回落 `llm.api_base` |
| `whisper_model` | `str` | `whisper-1` | |
| `audio_denoise` | `bool` | `True` | **当前不生效，见 §4.6** |

`engine` 的取值不会在配置层被校验（没有 enum），实际判定发生在 `_default_transcriber()`：只有 `bijian` / `jianying` / `whisper-cpp` 这三个名字会被当作"点名要 VideoCaptioner"，把它们放到链首；其他任何值都不影响链序。详见 `docs/ARCHITECTURE.md` 的转录链小节。

### 4.3 `ffmpeg`

| 键 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `ffmpeg_path` | `str` | `ffmpeg` | 可执行文件路径或命令名 |
| `ffprobe_path` | `str` | `ffprobe` | |
| `video_codec` | `str` | `libx264` | 软件回退编码器；实际编码器由试编码探测决定 |
| `preset` | `str` | `veryfast` | `auto_tune=true` 时会被硬件档位覆盖 |
| `crf` | `int` | `18` | 同上 |
| `pixel_format` | `str` | `yuv420p` | |
| `audio_codec` | `str` | `aac` | 成片音频实际是 `-c:a copy`，此项仅在需要重编码时使用 |
| `audio_bitrate` | `str` | `192k` | |
| `audio_sample_rate` | `int` | `44100` | |
| `wav_sample_rate` | `int` | `16000` | 交给 ASR 的 WAV 采样率 |
| `auto_tune` | `bool` | `True` | 是否按探测到的硬件档位调整 `preset` / `crf` |

构造 `FFmpegConfig` 时只透传 `model_fields` 里存在的键：

```python
FFmpegConfig(**{k: v for k, v in ffmpeg_raw.items() if k in FFmpegConfig.model_fields})
```

`SubtitleStyleConfig` 用同样的写法。这是**刻意的双重保险**：即便将来有人把 `extra="ignore"` 改成 `forbid`，多余的键（比如 `config.example.json` 里的 `_comment`）也不会让整个配置解析失败。

### 4.4 `style`

ASS 样式。字号单位是视频自身分辨率下的像素；竖屏视频会被识别并自适应边距。

| 键 | 类型 | 默认 |
|---|---|---|
| `zh_font` | `str` | `Microsoft YaHei` |
| `en_font` | `str` | `Arial` |
| `zh_font_size` | `int` | `52` |
| `en_font_size` | `int` | `34` |
| `zh_primary_color` | `str` | `&H00FFFFFF` |
| `en_primary_color` | `str` | `&H0000FFFF` |
| `outline_color` | `str` | `&H00000000` |
| `outline_width` | `float` | `3.5` |
| `shadow` | `float` | `1.5` |
| `margin_v` | `int` | `35` |
| `margin_l` | `int` | `20` |
| `margin_r` | `int` | `20` |
| `bilingual_zh_margin_v` | `int` | `90` |
| `bilingual_en_margin_v` | `int` | `35` |
| `fade_in_ms` | `int` | `120` |
| `fade_out_ms` | `int` | `120` |

颜色是 ASS 的 `&HAABBGGRR` 十六进制——**字节序是 ABGR，不是 RGB**。改动顺序不会报错，只会得到颜色不对的字幕。

### 4.5 顶层

| 键 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `output_dir` | `Path` | `./porter_output` | 相对路径按进程 CWD 解析 |
| `cookies_file` | `Path \| None` | `None` | 见 §4.6 |
| `cookies_browser` | `str \| None` | `None` | 见 §4.6 |

解析时 `cookies` 是 `cookies_file` 的别名、`cookies_from_browser` 是 `cookies_browser` 的别名（`data.get("cookies_file") or data.get("cookies")`）。保留别名是为了兼容早期写法；新配置请用长名。

### 4.6 已知不生效 / 半生效的键

这几项不是笔误，是当前实现的真实状态，读代码时容易误解，写文档时更不能省略：

| 键 | 现状 | 证据 |
|---|---|---|
| `asr.audio_denoise` | **完全不生效**。只有 `JobOptions.audio_denoise` 被读取（`media/prepare.py:132`），而两个前端构造 `JobOptions` 时用的是各自的 flag 默认值（CLI 用 `--no-denoise` 的反，MCP 用 `audio_denoise` 参数），从不参考 config | `grep -rn "config.asr.audio_denoise" src/` 无结果 |
| `cookies_file` / `cookies_browser` | **只对 inspect 路径生效**（`porter inspect`、MCP `porter_inspect` 会读 `config.cookies_file`）。`porter run` 与 `porter_job_start` 走的是 `JobOptions.cookies_file`，而它们只从各自的命令行 flag / 工具参数取值，不回落到 config | `platforms/base.py:292` 读 `options.cookies_file`；`porter_cli/commands/run.py` 传 `cookies_file=args.cookies` |

修复方向是明确的（让 `JobOptions` 的构造回落到 `config`，或把 `asr.audio_denoise` 并进 `JobOptions` 的默认值），但在那之前，本文件如实记录现状，不要让文档比代码更乐观。

---

## 5. 派生与兜底规则

Whisper 的凭证有两条兜底链，都在 `_from_mapping()` 里：

```python
whisper_api_key  = WHISPER_API_KEY  or asr.whisper_api_key  or llm.api_key
whisper_api_base = WHISPER_API_BASE or asr.whisper_api_base or llm.api_base
```

含义是：**只配 `llm.api_key` 就足以让 Whisper 后端可用**。`whisper_api_base` 同理——填了 `llm.api_base`（比如 DeepSeek）而对方不提供 Whisper 兼容端点时，转录会失败，但 `asr.engine` 的链会继续尝试下一个后端。想用 OpenAI 的 Whisper 而用别家的 LLM，必须显式写 `whisper_api_base`。

`asr.engine` 的解析多一个 `or None`：空字符串会被归一成 `None`（即"走回退链"），这样 `PORTER_ASR_ENGINE=` 不会变成一个永远匹配不上的引擎名。

`output_dir` 的兜底是 `PORTER_OUTPUT_DIR` → `output_dir` → `./porter_output`。

---

## 6. 安装级设置 vs 每作业选项

`PorterConfig` 描述的是**这台机器**（ffmpeg 在哪、字体多大、凭证是什么），`JobOptions`（`models/request.py`）描述的是**这一次作业**（烧哪些版本、目标语言、跑哪些阶段）。

判断标准很实用：**换一台机器会不会变？** 会变的（编码器、字体、Key）属于 config；同一个安装里跑两次可能不同的属于 options。

`JobOptions` 与 config 重名或易混的键：

| `JobOptions` | 默认 | 与 config 的关系 |
|---|---|---|
| `output_dir` | `./porter_output` | CLI 的 `-o` 覆盖 config（`_output_dir()` 显式实现） |
| `cookies_file` / `cookies_browser` | `None` | **不**回落到 config，见 §4.6 |
| `subtitle_file` | `None` | 显式指定源字幕（`.srt` / `.vtt`），优先于平台字幕轨与语音识别；config 里没有对应键，见下 |
| `audio_denoise` | `True` | **只有它生效**，config 里同名键不生效，见 §4.6 |
| `asr_engine` | `None` | 见下 |
| `translator` | `None` | 见下 |
| `llm_model` | `None` | 见下 |
| `target_lang` | `zh-Hans` | config 里没有对应键 |
| `burn` | `BurnMode.DUAL` | config 里没有对应键 |
| `only_phase` / `force` | `None` / `False` | 纯运行控制，config 里没有对应键 |

> **`asr_engine` / `translator` / `llm_model` 三者的语义与来源不同，别混。**
>
> | 字段 | 来源 | 语义 |
> |---|---|---|
> | `asr_engine` | 命令行 / MCP **覆盖** `config.asr.engine` | 把该后端**提到链首**，其余保留为回退（见 §4.6 下方） |
> | `translator` | **只能**来自命令行 / MCP | 同上，把该翻译后端提到链首 |
> | `llm_model` | 命令行 / MCP **覆盖** `config.llm.model` | 只影响**本次作业**的 LLM 模型 |
>
> 三者在 §13.48 之前**都是死字段**：两个前端的入口都接受它们，却没有任何引擎代码读取——`_default_transcriber()` 只读 `ctx.config.asr.engine`，`_default_translator()` 无条件装配整条链，LLM 模型只取自 `ctx.config.llm.model`。现已全部接通。
>
> `translator` 没有对应的配置键（`PorterConfig` 根本没有 `translate` 段，且 `extra="ignore"` 会默默丢弃它），所以它**只能**由 `porter run --translator` / MCP `porter_job_start(translator=...)` 给出。`asr_engine` 和 `llm_model` 则是"命令行覆盖配置"。
>
> **"提到链首"不等于"只用它"**：点名一个后端是要求**先试它**，其余后端仍作为回退。这是刻意的——若 `--translator bing` 变成"只准用 bing"，在 bing 被限流时（§13.48 实测过）作业会直接失败，而链的意义正是这个时候救场。
>
> **`subtitle_file`（`--subtitle-file FILE`）是另一个类别**：它不是选后端，而是**直接给出源字幕**。设了它，TRANSCRIBE 阶段既不读平台字幕轨也不跑语音识别。支持 `.srt` 与 `.vtt`。
>
> 为什么需要它：本地视频旁边同名的 `.srt` **不会**被自动接管（§13.29）——它可能是源字幕也可能是译文字幕，猜错会静默跳过 ASR 或覆盖用户文件。**点名文件是移除歧义，而不是靠猜解决歧义。** 它同时也是"视频没有字幕轨、又没有可用 ASR"时的出口（失败信息会指向它）。
>
> 与平台字幕轨不同，这个文件**不可用就报错**，不会静默退回语音识别：文件不存在、后缀不是 `.srt`/`.vtt`、内容没有 cue，三种情况各自给出明确错误。用户点名了一个文件，静默忽略它比报错更糟。

作业注册表的存放位置由 `platformdirs` 决定（`~/.cache/porter/jobs.json`，受 `XDG_CACHE_HOME` 影响），**不是**配置项，也不应通过配置去改。跨进程可见性依赖"两个进程解析到同一个路径"，所以它必须来自环境而不是文件。

---

## 7. 检查与修改

```bash
porter config list          # 打印解析后的全部值 + Source:
porter config get llm       # 单独看一段（也可 get llm.model 这样点到叶子）
porter config path          # 只打印生效的文件路径 / source
porter config set llm.model=deepseek-chat
```

行为细节：

- `list` 先打印 `Source: <路径>`，再打印 `config.masked()` 的 JSON。**掩码由 `mask_secret()` 统一实施**：未设置显示 `<not set>`，长度 ≤8 显示 `***`，否则显示 `首3位...末4位`。因此 `list` 的输出可以安全贴进 issue。
- `get` 走 `_lookup()` 按点号逐层取值；键不存在时打一条 warning 并返回非零退出码（而不是打印空行——一个不存在的键和一个值为空的键必须区分得开）。它读的同样是 `masked()` 之后的数据，所以 `porter config get llm.api_key` 也只会给出掩码。
- `set` 把值**一律作为字符串写入**（`raw_value.strip()`），由 pydantic 在下次读取时强制转换。`porter config set ffmpeg.crf=20` 落盘的是 `"20"` 而不是 `20`，读回来仍是 `int`。布尔值同理，写 `"true"` / `"false"`。
- `set` 默认写入**用户级** `config_file()`，与 CWD 无关；`--file PATH` 可以改到别的文件（例如项目里的 `porter.json`）。目录会被自动创建。
- `set` 的落盘是 `json.dumps(indent=2, ensure_ascii=False)`，不是原子写——它是交互式命令，不是热路径。
- MCP 侧的 `porter_config` 工具是**只读**的（只有 `list` / `get` 两个动作），密钥经同一套掩码。密钥不允许从对话进入配置文件，否则会进入对话记录与遥测。

---

## 8. 常见配置

```json
// 1. 用 DeepSeek 做翻译；顺带作为 Whisper 的兜底 Key
{
  "llm": {
    "api_key": "sk-...",
    "api_base": "https://api.deepseek.com/v1",
    "model": "deepseek-chat"
  }
}
```

```json
// 2. 又要 LLM 翻译、又要用 OpenAI 的 Whisper 转录
{
  "llm": { "api_key": "sk-...", "api_base": "https://api.deepseek.com/v1" },
  "asr": {
    "engine": "whisper-api",
    "whisper_api_key": "sk-openai-...",
    "whisper_api_base": "https://api.openai.com/v1"
  }
}
```

注意这里 `asr.engine` 是必要的：不写它，链会从第一个后端开始逐个尝试。写 `whisper-api` 只是让它排在最前——**链不会因为点名了某个引擎就跳过其他后端**，除了 `bijian` / `jianying` / `whisper-cpp` 这三个走 VideoCaptioner 的名字。

`porter run --asr-engine` 与 `--translator` 是同一条规则（提到链首、其余回退）。可用的名字是后端自身的 `name`，不是 `asr.engine` 那种额外别名：

| 旗标 | 可用的名字 |
|---|---|
| `--asr-engine` | `whisper-local`、`whisper-api`、`bcut`、`google-web`、`videocaptioner`，或 VideoCaptioner 引擎名 `bijian` / `jianying` / `whisper-cpp`（映射到 `videocaptioner`） |
| `--translator` | `llm`、`bing`、`google`、`mymemory`、`videocaptioner-llm`、`videocaptioner` |

名字写错**不会**让作业失败，也不会静默：链序不变，并在日志里记一条 warning。`videocaptioner` 与 `videocaptioner-llm` 是两个独立后端（后者额外需要 API key），点名哪个就只提哪个——不做别名猜测。

`--llm-model` 只覆盖**本次**的 LLM 模型，配置里的 `llm.model` 不受影响；它同时作用于 LLM 翻译后端与 `videocaptioner-llm` 适配器（后者把它作为 `--model` 转发给外部 CLI）。

```json
// 3. 竖屏视频：把中文字幕抬高，并固定用软件编码
{
  "style": { "margin_v": 55, "bilingual_zh_margin_v": 120 },
  "ffmpeg": { "auto_tune": false, "preset": "veryfast", "crf": 20 }
}
```

```json
// 4. 项目级最小配置：复用本机代理端口，其余全用默认值
{
  "llm": { "api_base": "http://127.0.0.1:8080/v1" }
}
```

第 4 例要留意 §2.1 的排他规则：这个 `porter.json` 会让用户配置里的任何内容（包括 Key）整段失效。

---

## 9. 贡献者注意

- **改动键名 = 破坏契约。** `SubtitleStyleConfig` 的字段名刻意与 v0.1 的 `config.json` 保持一致，用户的旧文件必须仍然可用。新增键要在本文件和 `skills/porter-skill/references/CONFIG.md` 同步登记。
- **`extra="ignore"` 意味着拼错不会报错。** 如果将来要改成 `forbid`，必须同时处理 `config.example.json` 里的 `_comment` 键，否则模板文件自己就会解析失败。当前 `FFmpegConfig` / `SubtitleStyleConfig` 的字段白名单过滤是为此准备的双保险。
- **`assets/config.example.json` 不是权威。** 它是一份人写的模板，可能与模型默认值漂移（当前已经漂移，见该文件与 §4.1/§4.2 的差异）。改默认值时要检查它，但不要把它当来源。
- **不要把部署形态写进配置解析。** v0.1 的 `get_default_user_config_path()` 会去猜 agent skill 装在哪、甚至用 `SKILL.md` 是否存在来选写入目标。v0.2 明确不做这件事：需要非标准位置的调用方（skill 的 `scripts/`、容器）自行设 `PORTER_CONFIG`。新增配置查找逻辑时不要重新引入这类知识。
- **解析函数是纯的。** `resolve()` / `find_project_config()` 之外没有缓存，每次调用都重新读盘。`RunContext` 在构造时解析一次并把结果固定下来，这正是 v0.1 做不到的："配置改了一半，运行到一半才生效"在 v0.2 是不可能的。不要在阶段内部再调 `resolve()`。
