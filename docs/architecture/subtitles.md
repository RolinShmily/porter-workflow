# 字幕

## 来源路径与工作流分流

`transcript` 阶段的能力是对等的，由**源事实**和显式参数在**计划解析阶段**决定，
执行期不会在路径之间静默回退：

1. **显式供字幕（`transcript.supplied`）**：请求给出 `--subtitle-file` 时，直接把该
   文件作为源字幕轨，进入 `url.subtitled` 或 `local.subtitled` 工作流，避免猜测同目录
   `.srt` 到底是源字幕还是译文。
2. **平台原生字幕轨（`transcript.platform-track`）**：探测到 URL 列出源语言字幕轨
   （如 YouTube、Bilibili、TikTok）时，解析器选择 `url.subtitled`。优势是精确的拼写、
   标点和专有名词，且零识别成本。
   **列出的轨是请求而不是保证**：抓取可能因限流（HTTP 429）失败；失败时执行器**不会**
   静默回退到 ASR，而是返回失败详情与 `suggested_workflow: "url.asr"`，由调用方或
   Agent 显式重新 `plan`。
3. **语音识别（`transcript.whisper-local` / `transcript.whisper-api`）**：无原生轨
   （如 X/Twitter、Instagram、无字幕本地视频/音频）或显式指定 ASR 时，进入 `url.asr` /
   `local.asr` / `audio.asr` 工作流。

`porter.core.asr.platform_subs` 只负责读取 `acquire` 抓下来的字幕文件；
它不是 `AsrBackend`，但在工作流能力目录中与 ASR 能力处于同一对等层。

## 对等转录能力（无运行期回退）

| 能力 ID | 成本 / 外发 | 前提 |
| --- | --- | --- |
| `transcript.platform-track` | `free` / `none` | 源 URL 列出平台字幕轨 |
| `transcript.supplied` | `free` / `none` | 显式传入 `--subtitle-file` |
| `transcript.whisper-local` | `free` / `none` | 安装 `[asr-local]`；首次需下载约 484 MB 模型，之后离线可用 |
| `transcript.whisper-api` | `paid` / `audio` | 安装 `[llm]` 且配置 Whisper/LLM Key；默认排除，需显式命名或 `--allow-paid` |

规则：

- 免费优先：默认只在免费能力中选择；环境里存在 API Key 只是配置，不是同意付费。
- 单能力执行：计划选定一个能力，执行器只构造该单一能力；缓存复用会核对 `origin_prefix`。
- 失败即止：返回空结果或抛错视为该能力失败，附带 `alternatives` 与 `suggested_workflow`
  供重新 `plan`；`JobCancelled` 立即向上抛。

## 对等翻译能力（无运行期回退）

| 能力 ID | 成本 / 外发 | 前提 |
| --- | --- | --- |
| `translate.bing` | `network` / `text` | 免 Key，逆向端点，未验证 |
| `translate.google` | `network` / `text` | 免 Key，逆向端点，未验证 |
| `translate.mymemory` | `network` / `text` | 免 Key，有文档的公开 API |
| `translate.llm` | `paid` / `text` | 安装 `[llm]` 且配置 LLM Key；默认排除，需显式命名或 `--allow-paid` |

`--translate`（旧别名 `--translator`）在计划阶段严格锁定该能力；若能力限流或失败，
执行器只在**同一能力内**做必要重试，不会静默切到其他翻译器，而是返回 `alternatives`
由调用方重新 `plan`。

### CJK 自检

免 Key 后端可能返回 HTTP 200，却把输入原样带回（反爬、语言对不支持、额度耗尽）。
如果不检查，每条 cue 都有非空 `target_text`，看起来翻译成功，BURN 也成功，
最终得到一条双语视频里两条相同的英文轨，作业还报告 DONE。

因此翻译结果必须通过 `has_chinese_translation()`：目标语言是中文却没有
CJK 字符时，直接判定本次翻译能力失败，拒绝把原文回显当成译文写出，并给出其他
可选翻译能力供重新 `plan`。

## 句子级翻译与碎片重构

ASR 和平台自动字幕常常给出滚动碎片（YouTube 自动字幕每 2–3 个词一条）。
逐条翻译会得到语序颠倒的碎词。翻译阶段因此做三件事：

1. **合并短碎片**（`merge_short_fragments`），把相邻过短的 cue 合成可翻译的句。
2. **重构句子**：按语法、标点和静音间隙切分，得到 `TranscriptSentence`。
3. **按中文意群重新断句**：单行 ≤ 20 字，时间戳按字数比例平滑分配。

翻译作用在**句子**上，而不是 cue 上；译完再切回带时间戳的 cue。因此输出 cue
数量可以与输入不同，cue 列表是**被替换**而不是被标注。

**副作用（重要）**：对已经是句级切分的字幕，合并仍可能发生。实测 3 条输入
可能变成 2 条输出，把两个不相关的短句并成一条；合并后的 cue 时间跨度正确覆盖
原 cue，所以行为正确但边界变了。CLI 与 MCP 都会报告
`input_cue_count` / `cue_count` / `cues_merged`，不会静默改变。

新工作流执行器固定使用 `PassthroughRefiner`，不会在计划之外发起隐藏的 LLM 精修调用。

## 已知脆弱点

1. **`bing` 的分隔符会被重写。** 批量请求用 `\n[[|]]\n` 分隔；段数对不上时
   在**同一能力内**降级为逐句请求，而不是整条能力失败。
2. **`google` / `mymemory` 会限流（HTTP 429）**，尤其在反复探测之后。
   这是环境性的，等一会儿或重新 `plan` 切换其他翻译能力。
3. **平台字幕抓取会失败**，尤其在限流（HTTP 429）时；执行器会明确失败并提示
   `suggested_workflow: "url.asr"`，由调用方决定是否改走 ASR。
