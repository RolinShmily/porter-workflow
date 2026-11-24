# 计划与结果契约（WorkflowPlan / WorkflowResult）

本页展开 [`SKILL.md`](../SKILL.md) 第 2–3 节的数据结构、能力目录与问题码。

---

## 1. 核心不变量

1. **一阶段一能力**：`stages[].capability` 是唯一执行能力，不存在运行期跨能力回退。
2. **计划即执行契约**：`plan --out plan.json` → `run --plan plan.json` 按计划逐字执行。
3. **付费显式授权**：`cost=paid` 默认排除；显式命名或传 `--allow-paid` 才会被选中。
4. **配置指纹防漂移**：计划记录非密钥配置哈希；配置改变后执行被 `PLAN_CONFIG_CHANGED` 拒绝。
5. **目标画质锚定**：URL 下载默认按 `1080p` 短边分辨率排序（横屏 `1920x1080` 与竖屏
   `1080x1920` 同享基准），达不到时平滑降级并附带 `QUALITY_DEGRADED` warning。

---

## 2. 能力目录

| 阶段 | 能力 id | 成本 | 数据出口 | 说明 |
|---|---|---|---|---|
| `probe` | `probe.ytdlp` | network | none | 读取平台元数据 |
| `probe` | `probe.ffprobe` | free | none | 读取本地媒体流信息 |
| `probe` | `probe.subtitle` | free | none | 解析本地 `.srt`/`.vtt` |
| `acquire` | `acquire.ytdlp-video` | network | none | 下载平台音视频与原生字幕 |
| `acquire` | `acquire.ytdlp-audio` | network | none | 仅下载平台音轨（`--task audio`/`transcribe`） |
| `acquire` | `acquire.ytdlp-subs` | network | none | 仅拉取平台原生字幕（`--task subtitle`） |
| `acquire` | `acquire.local` | free | none | 接入本地媒体文件 |
| `normalize` | `normalize.ffmpeg` | free | none | 生成 H.264/AAC 母版与 16 kHz 单声道音轨 |
| `transcript` | `transcript.platform-track` | free | none | 读取平台原生字幕轨 |
| `transcript` | `transcript.supplied` | free | none | 读取用户提供的字幕文件 |
| `transcript` | `transcript.whisper-local` | free | none | 本地离线 `faster-whisper`（内置） |
| `transcript` | `transcript.whisper-api` | **paid** | **audio** | OpenAI 兼容 Whisper API |
| `translate` | `translate.bing` | network | **text** | 免 Key 翻译端点 |
| `translate` | `translate.google` | network | **text** | 免 Key 翻译端点 |
| `translate` | `translate.mymemory` | network | **text** | 免 Key 翻译端点 |
| `translate` | `translate.llm` | **paid** | **text** | OpenAI 兼容 LLM 翻译 |
| `write` | `write.subtitles` | free | none | 输出双语/目标语 `.srt` + `.ass` |
| `render` | `render.ffmpeg-libass` | free | none | FFmpeg + libass 硬字幕压制 |

---

## 3. 常见 `issues[]` 与 `error` 问题码

| code | 级别 | 含义与处理 |
|---|---|---|
| `SOURCE_UNUSABLE` / `SOURCE_NOT_FOUND` | blocker | 链接失效/受限或文件不存在；受限链接配 cookies，死链不重试 |
| `PROBE_FAILED` | blocker | `ffprobe` 无法解析本地文件 |
| `TRACKS_UNVERIFIED` | warning | 平台列出了字幕轨但未抓取（requested, not guaranteed） |
| `QUALITY_DEGRADED` | warning | 可用最高画质低于目标画质（如未登录 B 站仅 480p），已自动降级 |
| `TRANSLATION_SKIPPED` | warning | 平台列出了**人工**目标语轨，计划跳过翻译（自动翻译轨不跳过） |
| `TARGET_TRACK_MISSING` | runtime error | 计划跳过了翻译但运行期目标轨未拉到；按 `alternatives` 或 `--force-translate` 重新 `plan` |
| `PLATFORM_TRACK_MISSING` | blocker | 源无源语言字幕轨；改用 `--subtitle-file` 或 ASR |
| `SUPPLIED_SUBTITLE_MISSING` | blocker | 未提供 `--subtitle-file` |
| `CAPABILITY_UNAVAILABLE` | blocker | 显式指定的能力不可用（如缺 Key 或缺 libass） |
| `CAPABILITY_WRONG_STAGE` | blocker | 能力不属于该阶段 |
| `CAPABILITY_WRONG_WORKFLOW` | blocker | 无字幕轨的源上请求平台/供字幕 |
| `CAPABILITY_WRONG_SOURCE` | blocker | 纯字幕输入上请求需要音频的转录能力 |
| `PAID_CAPABILITY_NOT_ALLOWED` | blocker | 默认策略拒绝付费；经用户同意后显式命名或加 `--allow-paid` |
| `NO_CAPABILITY` | blocker | 该阶段无可用能力 |
| `NO_VIDEO_FOR_RELEASE` | blocker | 音频文件请求 `release`；改 `--task subtitle` 或 `transcribe` |
| `TASK_REQUIRES_MEDIA` | blocker | 纯字幕输入请求 `transcribe` 或 `audio` |
| `TASK_NOT_SUPPORTED` | blocker | `convert` 尚未接入执行器 |
| `PLAN_INFEASIBLE` | runtime error | 执行了 `feasible=false` 的计划 |
| `PLAN_CONFIG_CHANGED` | runtime error | 计划生成后非密钥配置变更；重新 `plan` |
