# 计划与结果契约（WorkflowPlan / WorkflowResult）

本页是 [`SKILL.md`](../SKILL.md) 第 1–3 节的技术展开。日常使用只需 `plan --json` 和
`run --plan`；需要判断“为什么计划选了它”“失败后该换什么”时读这里。

---

## 1. 不变量

1. **一个阶段一个能力。** `stages[].capability` 是唯一实现，不是候选列表。
2. **计划完全解析。** 是否包含某阶段在 plan 时决定，运行期没有条件分支。
3. **跨能力替换必须重新 plan。** 运行期只在同一能力内部做有限重试。
4. **付费需要显式同意。** `cost=paid` 的能力默认被排除；显式命名或 `--allow-paid`
   才会选中。
5. **配置指纹防漂移。** 计划保存非密钥配置的哈希；配置变化后执行被
   `PLAN_CONFIG_CHANGED` 拒绝。
6. **计划可复现。** `plan_id` 是排除 `plan_id` / `created_at` 后的内容哈希。相同
   源、参数、探测事实与配置得到相同 id。

---

## 2. `WorkflowPlan` 字段

```jsonc
{
  "schema_version": 1,
  "plan_id": "sha256:...",
  "porter_version": "0.3.2",
  "created_at": "2026-11-23T...",
  "workflow": "url.asr",
  "task": "release",
  "source": "https://...",
  "runtime": {
    "output_dir": "./porter_output",
    "target_lang": "zh-Hans",
    "force": false,
    "audio_denoise": true
  },
  "config_fingerprint": "sha256:...",
  "source_probe": { "...": "见 §3" },
  "stages": [ { "...": "见 §4" } ],
  "feasible": true,
  "issues": []
}
```

| 字段 | 含义 |
|---|---|
| `workflow` | `url.subtitled` / `url.asr` / `local.subtitled` / `local.asr` / `subtitle.only` / `audio.asr` |
| `task` | `probe` / `transcribe` / `subtitle` / `release` / `convert` / `audio` |
| `runtime` | 执行设置，不是能力选择；`run --plan` 只允许覆盖 `output_dir` 与 `force` |
| `source_probe` | 探测事实：`kind`、`platform`、`title`、`duration_seconds`、`width/height`、`has_video`、`has_audio`、`tracks`、`warnings` |
| `stages` | 已解析阶段；每项一个能力 |
| `issues` | `blocker` 或 `warning`；`feasible` 等价于没有 blocker |

### 2.1 `issues[]`

```jsonc
{
  "code": "NO_CAPABILITY",
  "severity": "blocker",
  "stage": "transcript",
  "capability": "transcript.whisper-local",
  "message": "no transcript capability is available on this machine",
  "fix": "install porter-workflow[asr-local], configure an API key ...",
  "alternatives": ["transcript.whisper-api"],
  "suggested_workflow": null
}
```

`severity=warning` 只提示，不阻止执行。`fix` 是给 Agent 的下一步，不是装饰。

### 2.2 `tracks[]`

```jsonc
{
  "lang": "en",
  "role": "source",          // source / target / other
  "auto_generated": true,
  "verified": false,          // 只有真正抓取并解析后才为 true
  "path": null
}
```

`verified=false` 表示平台**列出**了轨道，抓取仍可能失败。计划据此选择
`url.subtitled`，但运行期失败会作为能力失败报告，不会自动回退 ASR。

---

## 3. `source_probe`

| `kind` | 含义 | 可用工作流 |
|---|---|---|
| `url` | 平台链接 | `url.subtitled` / `url.asr` |
| `local_video` | 本地视频 | `local.subtitled` / `local.asr` |
| `subtitle` | `.srt` / `.vtt` | `subtitle.only` |
| `audio` | 音频文件（后缀或实测无视频流） | `audio.asr` |

URL 的 `has_audio` 在探测阶段为 `false`：元数据不能证明音频流存在，执行阶段
PREPARE 报告真实结果。这是刻意的，不伪造事实。

---

## 4. `stages[]`

```jsonc
{
  "id": "transcript",
  "stage": "transcript",
  "capability": "transcript.whisper-local",
  "params": {},
  "needs": ["master_audio"],
  "produces": ["source_transcript"],
  "optional": false
}
```

阶段顺序：`probe` → `acquire` → `normalize` → `transcript` → `translate` → `write`
→ `render`。`subtitle.only` 没有 acquire/normalize/render；`audio.asr` 没有 render；
`--task` 会在对应位置截断。

### 4.1 能力目录（18 项）

| 阶段 | 能力 id | 成本 | 出口 | 说明 |
|---|---|---|---|---|
| probe | `probe.ytdlp` | network | none | 读平台元数据 |
| probe | `probe.ffprobe` | free | none | 读本地媒体事实 |
| probe | `probe.subtitle` | free | none | 解析供字幕 |
| acquire | `acquire.ytdlp-video` | network | none | 下载视频+音频 |
| acquire | `acquire.ytdlp-audio` | network | none | 只下载音频（当前执行器未使用） |
| acquire | `acquire.ytdlp-subs` | network | none | 只取平台字幕（当前执行器未使用） |
| acquire | `acquire.local` | free | none | 接入本地文件 |
| normalize | `normalize.ffmpeg` | free | none | 标准化母版与音轨 |
| transcript | `transcript.platform-track` | free | none | 平台原生字幕，零识别 |
| transcript | `transcript.supplied` | free | none | 调用方提供的字幕 |
| transcript | `transcript.whisper-local` | free | none | 本地 faster-whisper |
| transcript | `transcript.whisper-api` | **paid** | **audio** | OpenAI 兼容 Whisper |
| translate | `translate.bing` | network | **text** | 免 Key 端点 |
| translate | `translate.google` | network | **text** | 免 Key 端点 |
| translate | `translate.mymemory` | network | **text** | 免 Key 端点 |
| translate | `translate.llm` | **paid** | **text** | OpenAI 兼容 chat |
| write | `write.subtitles` | free | none | 写 SRT/ASS |
| render | `render.ffmpeg-libass` | free | none | 烧硬字幕 |

“network” 是免费但需要联网；“paid” 是会产生费用的能力。`egress=audio` 表示音频
上传到第三方；`egress=text` 表示字幕文本离开本机。

---

## 5. `WorkflowResult`

```jsonc
{
  "job_id": "run-...",
  "state": "done",             // done / failed / cancelled
  "plan_id": "sha256:...",
  "workflow": "url.asr",
  "task": "release",
  "task_dir": "/.../porter_output/abc123_Title",
  "stages": [
    {
      "id": "transcript",
      "stage": "transcript",
      "capability": "transcript.whisper-local",
      "status": "done",        // done / reused / skipped / failed
      "params": {},
      "inputs": {},
      "outputs": {"source_transcript": "/.../cooked/subtitle.srt"},
      "error": null,
      "duration_seconds": 12.4
    }
  ],
  "artifacts": {
    "video": "/.../raw/video.mp4",
    "subtitle_zh_srt": "/.../cooked/subtitle_zh.srt",
    "video_zh": "/.../cooked/video_zh.mp4"
  },
  "error": null,
  "duration_seconds": 95.2
}
```

`artifacts` 是各阶段 `outputs` 与媒体产物的合并；同一路径去重。`state=done` 只表示
执行完成，**不表示产物可用**——仍要跑 [`SKILL.md`](../SKILL.md) §5.3 的质检。

### 5.1 失败时的 `error`

```jsonc
{
  "code": "CAPABILITY_UNAVAILABLE",
  "message": "transcript.whisper-api is not available on this machine (requires openai, api key)",
  "retryable": false,
  "alternatives": ["transcript.whisper-local"],
  "suggested_workflow": null,
  "details": {}
}
```

| 字段 | Agent 的动作 |
|---|---|
| `retryable=true` | 可以原计划重试（同能力内部问题） |
| `alternatives` | 选一个可用能力，显式重新 `plan` |
| `suggested_workflow` | 改走另一条工作流，重新 `plan` |
| `details` | 环境/参数细节，用于报告或修配置 |

---

## 6. 常见问题码

| code | 触发 |
|---|---|
| `SOURCE_UNUSABLE` / `SOURCE_NOT_FOUND` | 链接不可用或文件不存在 |
| `PROBE_FAILED` | ffprobe 无法读取本地文件 |
| `TRACKS_UNVERIFIED` | 平台列出了轨道，但未抓取（warning） |
| `PLATFORM_TRACK_MISSING` | 计划选择平台字幕，但源没有列出源语言轨 |
| `SUPPLIED_SUBTITLE_MISSING` | 计划选择供字幕，但没有指定文件 |
| `CAPABILITY_UNAVAILABLE` | 显式请求的能力不可用 |
| `CAPABILITY_WRONG_STAGE` | 请求的能力属于别的阶段 |
| `CAPABILITY_WRONG_WORKFLOW` | ASR 工作流里请求平台/供字幕 |
| `CAPABILITY_WRONG_SOURCE` | 纯字幕输入里请求需要音频的能力 |
| `PAID_CAPABILITY_NOT_ALLOWED` | 付费能力未被显式允许 |
| `NO_CAPABILITY` | 某阶段没有任何可用能力 |
| `TASK_REQUIRES_MEDIA` | 对字幕文件请求转录/音频任务 |
| `NO_VIDEO_FOR_RELEASE` | 音频输入请求压制 |
| `TASK_NOT_SUPPORTED` | `convert` 尚未接入执行器 |
| `TRANSLATION_SKIPPED` | 平台已有目标语轨，翻译被跳过（warning） |
| `PLAN_INFEASIBLE` | 执行了 `feasible=false` 的计划 |
| `PLAN_CONFIG_CHANGED` | 计划生成后非密钥配置改变 |

---

## 7. 完整示例：保存并执行

```bash
python3 <SKILL_DIR>/scripts/porter.py plan "<URL>" --burn zh_only --json > plan.json
python3 <SKILL_DIR>/scripts/porter.py run --plan plan.json --json > result.json
```

`plan.json` 可以提交、审阅和重放。`run --plan` 忽略 `--transcript` / `--burn` /
`--target-lang` / `--cookies` 等规划参数并打印警告——要改这些，重新生成计划。
