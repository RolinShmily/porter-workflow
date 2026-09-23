# porter 架构

本文说明 porter 的内部结构，供需要判断"为什么这么设计"或"哪里可能出问题"时阅读。日常使用只需 `SKILL.md`。

---

## 1. 四个阶段

```
prepare → transcribe → translate → burn
```

| 阶段 | 做什么 | 产物 |
|---|---|---|
| `prepare` | 获取媒体、标准化为母版、提取音轨、探测画幅 | `raw/video.mp4`、`raw/audio.wav`、`raw/audio_enhanced.wav`、`raw/metadata.json` |
| `transcribe` | 取平台原生字幕轨，或跑语音识别 | `raw/subtitle.srt`、`raw/transcript.json/.txt` |
| `translate` | 句子级翻译、碎片重构、生成双语/中文轨 | `cooked/subtitle.srt`、`cooked/subtitle_zh.srt`、两个 `.ass` |
| `burn` | 用 libass 烧录硬字幕 | `cooked/video_bilingual.mp4`、`cooked/video_zh.mp4` |

**每个阶段消费上一阶段的内存结果，同时把自己的产物写到磁盘。** 所以失败时已完成的阶段不会白跑——这一点直接决定了 `--only-phase` 的语义。

### `--only-phase` 是"到此为止"，不是"只跑这个"

因为每个阶段需要上一阶段的输出，`--only-phase translate` 仍然会执行 `prepare` 和 `transcribe`。它表达的是"做完这个阶段就停"，用于调试或只想要中间产物。

---

## 2. 字幕来源：两条路径

`transcribe` 阶段有两条路，由平台规格决定：

1. **平台原生字幕轨**（`platforms/*.py` 里 `subtitles.remote=True` 的平台）
   - YouTube、Bilibili、TikTok
   - 优势：**精确的拼写、标点、专有名词，且零识别成本**
   - 若平台还提供中文轨（`prefer_existing_chinese=True`），翻译阶段整段跳过
   - **但这是"请求"而非"保证"**：抓取可能失败（平台限流，实测会遇到 HTTP 429），失败后回退到语音识别

2. **语音识别**（`subtitles.remote=False` 的平台，或没有原生轨时）
   - X / Twitter、Instagram 永远走这条（它们的规格里 `remote=False`，即不抓字幕）
   - 按回退链依次尝试后端

> `plan` 命令报告的 `subtitles.route` 就是这条判断的结论，它复用管线自身的 `plan_subtitles()`，不是重新推断。

---

## 3. 后端回退链

链按顺序尝试，第一个成功的胜出。

### ASR（语音识别）

| 后端 | 需要 | 端点是否经实测 |
|---|---|---|
| `whisper-api` | Whisper API Key（或 `llm.api_key`） | ✅ |
| `bcut` | 无 | ❌ 逆向，**当前返回空结果** |
| `google-web` | 无 | ❌ 逆向，**当前返回空结果** |
| `videocaptioner` | 已安装 VideoCaptioner CLI | ✅ |

**"端点是否经实测"不等于"当前可用"。** 上表后两列的 `❌` 表示线格式是逆向出来的、随时可能失效——而 `bcut` / `google-web` 已经实测失效。所以**没有 Key 时，若视频也没有原生字幕轨，转录必然失败**。

### 翻译

| 后端 | 需要 | 端点是否经实测 |
|---|---|---|
| `llm` | LLM API Key | ✅ |
| `bing` | 无 | ❌ 逆向 |
| `google` | 无 | ❌ 逆向 |
| `mymemory` | 无 | ✅ |
| `videocaptioner-llm` | VideoCaptioner CLI | ✅ |
| `videocaptioner` | VideoCaptioner CLI | ✅ |

翻译**不需要任何 Key**：`bing` → `google` → `mymemory` 这条免 Key 路径可用（实测 `google` 与 `mymemory` 正常；`bing` 可用但行为不稳定，见下）。

### 两处已知的脆弱点

1. **`bing` 的分隔符会被重写。** Bing 现在走 LLM 翻译，会重排空白：批量请求用 `\n[[|]]\n` 做分隔符，若段数对不上就**降级为逐句请求**而不是整条后端失败。这是刻意的——单个后端的格式猜测不该毁掉整条链。
2. **`google` / `mymemory` 会限流（HTTP 429）**，尤其在被反复探测之后。这是环境性的，等一会儿或换后端。

---

## 4. 句子级翻译与碎片重构

ASR 和平台字幕常常给出**滚动碎片**（YouTube 自动字幕每 2–3 个词一条）。直接逐条翻译会得到语序颠倒的碎词。

所以翻译阶段做三件事：

1. **合并短碎片**（`merge_short_fragments`）——把过短的相邻 cue 合成可翻译的完整句
2. **重构句子**——按语法、标点与静音间隙切分
3. **按中文意群重新断句**——单行 ≤20 字，时间戳按字数比例平滑分配

**副作用（重要）**：对**已经是句级切分**的字幕文件，合并仍会发生——实测 3 条输入可能变成 2 条输出，把两个不相关的短句并成一条（合并后的 cue 时间跨度正确覆盖原 cue，所以**行为正确但边界变了**）。CLI 与 MCP 都会如实报告 `input_cue_count` / `cue_count` / `cues_merged`，不会静默改变。

---

## 5. 媒体层

所有 ffmpeg/ffprobe 调用收在一个入口（`media/ffmpeg.py`），统一应用：

- `-nostdin`（否则 ffmpeg 会偷走 stdin）
- stderr 只保留尾部若干行（否则错误信息被淹没）
- 显式 utf-8 + `errors="replace"`（避免非 ASCII 路径在 ASCII locale 下炸掉）

**成片音频直通复制**（`-c:a copy`）：`raw/audio_enhanced.wav` 只供 ASR 使用，从不进入成片。这样识别准确率与成片音质兼得。

### 硬件编码分级

启动时用**试编码一帧**的方式判断硬件编码器是否真的可用（而不是只看设备节点存在）：

```
h264_nvenc / hevc_nvenc / h264_qsv / h264_amf → libx264
```

判定结果缓存在进程内的 `EncoderSelector`，所以一次进程内多次压制只探测一次。

### 压制输出是原子的

先写到目标旁的临时文件，**探测确认可读后**才改名到位。v0.1 是无条件改名，导致截断的编码被当成成品发布——这正是质检步骤要跑 `ffprobe` 的原因。

---

## 6. 作业注册表

作业记录写在一个共享的 JSON 注册表文件里（原子写 + 文件锁 + 用 `/proc` 的进程启动时间判断 PID 是否陈旧）。

**这解决了"长任务无法轮询"的问题**：`porter run` 在终端 A 启动的作业，终端 B 的 `porter jobs status` 看得到，MCP server 也看得到。取消同理——由一个每作业的看门狗线程轮询注册表实现，因为静默的 yt-dlp 下载期间事件通道根本不产生事件。

---

## 7. 配置与分层

引擎（`src/porter/`）不知道部署形态。CLI（`src/porter_cli/`）与 MCP（`src/porter_mcp/`）是两个对等前端，都只依赖引擎的公开接口。

分层由 `import-linter` 强制（2 条契约），依赖方向单向：

```
porter_mcp / porter_cli
        ↓
     porter.plan
        ↓
   porter.pipeline
        ↓
  asr / translate / platforms / media / doctor
        ↓
   models / errors / events / config
```

引擎内**零 `print()`**（由 ruff 的 `T20` 规则强制）：MCP 的 stdout 是 JSON-RPC 通道，任何一行杂输出都会破坏协议。所有日志走 stderr。
