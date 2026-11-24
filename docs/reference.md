# Python API 与 CLI 参数参考

本页记录 **Python 公开 API（`import porter`）** 与 **WebUI 启动参数**。工作流能力目录、问题码、配置键默认值与受限宿主说明均由 Skill 参考页单一维护，避免两处重复：

- 16 项阶段能力、工作流与问题/错误码：[`skills/porter-workflow/references/PLAN.md`](../skills/porter-workflow/references/PLAN.md)
- 配置优先级、全部配置键默认值与环境变量：[`skills/porter-workflow/references/CONFIG.md`](../skills/porter-workflow/references/CONFIG.md)
- 受限宿主、Windows PowerShell 与脚本覆盖变量：[`skills/porter-workflow/references/HOSTS.md`](../skills/porter-workflow/references/HOSTS.md)

---

## 1. 高层门面 API（推荐入口）

`from porter import ...` 是稳定的公开库接口（通过 PEP 562 懒加载，仅访问时才导入对应子模块；完整导出列表见 `porter.__all__`）。

### `plan_source(source, ...)`

探测源、合并当前生效配置并生成可序列化的 `WorkflowPlan`。

```python
from porter import plan_source

plan = plan_source(
    "https://www.youtube.com/watch?v=...",
    task="release",           # "probe" | "audio" | "transcribe" | "subtitle" | "release"
    quality="1080p",          # "best" | "2160p" | "1440p" | "1080p" | "720p" | "480p" (支持 4k/fhd/hd 等别名)
    burn="dual",              # "dual" | "zh_only" | "bilingual_only" | "skip"
    target_lang="zh-Hans",    # 目标语言
    output_dir=None,          # 省略时读取配置中的 output_dir（默认 ./porter_output）
    transcript=None,          # 显式锁定转录能力，如 "whisper-local" / "whisper-api"
    translate=None,           # 显式锁定翻译能力，如 "bing" / "google" / "mymemory" / "llm"
    render=None,              # 显式锁定渲染能力
    subtitle_file=None,       # 本地供字幕路径（.srt / .vtt）
    cookies_file=None,        # Netscape cookies.txt 路径（省略时继承配置）
    cookies_browser=None,     # 浏览器名，如 "firefox" / "chrome"（省略时继承配置）
    llm_model=None,           # 覆盖 LLM 模型名
    audio_denoise=None,       # 是否降噪（省略时继承配置，默认 True）
    allow_paid=False,         # 是否允许自动选择付费能力
    force_translate=False,    # 忽略平台人工目标语轨，强制翻译
    force=False,              # 忽略已有缓存，重跑各阶段
    config=None,              # 传入已解析的 PorterConfig，省略时自动 resolve(None)
)
```

常用属性与方法：
- `plan.feasible`：是否可执行；为 `False` 时查看 `plan.blockers`（每项含 `code`、`message`、`fix`、`alternatives`）。
- `plan.issues`：包含 blocker 与 warning（如 `QUALITY_DEGRADED`、`TRACKS_UNVERIFIED`、`TRANSLATION_SKIPPED`）。
- `plan.to_dict()` / `WorkflowPlan.model_validate_json(...)`：序列化与反序列化。

### `run_plan(plan, ...)`

按给定 `WorkflowPlan` 执行任务，并自动接入跨进程 `JobRegistry` 与协作式取消：

```python
from porter import run_plan

outcome = run_plan(
    plan,
    config=None,      # 省略时自动 resolve(None)
    store=None,       # 自定义 JobStore（省略时使用默认共享 JobRegistry）
    events=None,      # EventSink 回调：def on_event(event: Event) -> None
    job_id=None,      # 自定义作业 ID
)

wf = outcome.workflow
if wf.error is None:
    print(wf.task_dir, wf.artifacts)
else:
    print(wf.error.code, wf.error.message, wf.error.alternatives)
```

预期失败不会抛异常，而是作为结构化数据返回在 `outcome.workflow.error` 中。

### `run_doctor(config=None)`

检查当前环境的 FFmpeg/`libass`、硬件编码器、JS 运行时（Deno/Node）、字体与 ASR/翻译路由：

```python
from porter import run_doctor

report = run_doctor()
print(report.ok, [f.detail for f in report.blockers])
```

---

## 2. 进阶：自定义事件、取消、配置读写与底层接口

### 实时事件与跨线程取消

```python
from porter import Event, JobOptions, RunContext, execute_plan

def on_event(event: Event) -> None:
    print(type(event).__name__, event.model_dump(mode="json"))

ctx = RunContext(job_id="custom-1", options=JobOptions(), events=on_event)
# 任意线程可安全触发取消（阶段间抛出 JobCancelled）：
# ctx.request_cancel()
outcome = execute_plan(plan, ctx)
```

### 配置读写与跨进程作业查询

- `resolve(path=None) -> PorterConfig`：按优先级解析生效配置；`cfg.masked()` 返回密钥脱敏字典。
- `writable_config_path() -> Path` / `save_key(key, value)`：写入 JSON 配置文件（不覆写手写 TOML）。
- `JobRegistry().read(reap=True)` / `JobRegistry().request_cancel(job_id)`：跨进程查看或取消作业。
- `probe_source`、`capability_catalog`、`resolve_plan`、`Pipeline`：供需要拆步探测或自定义端口注入的场景使用。
- `PorterError` 及其子类（`ConfigError`、`MediaError`、`AsrError`、`TranslationError`、`RenderError`、`ExtractionError`、`JobCancelled` 等）：结构化异常基类，均带 `.code`、`.message`、`.details`。

---

## 3. 最小配置文件示例（`porter.json`）

完整键名与默认值见 [`skills/porter-workflow/references/CONFIG.md`](../skills/porter-workflow/references/CONFIG.md)：

```json
{
  "output_dir": "./porter_output",
  "video_quality": "1080p",
  "cookies_browser": "firefox",
  "asr": {
    "whisper_local_model": "small",
    "audio_denoise": true
  },
  "llm": {
    "api_base": "https://api.deepseek.com/v1",
    "model": "deepseek-chat"
  }
}
```

密钥建议通过环境变量（`OPENAI_API_KEY` / `WHISPER_API_KEY`）传入，避免写入仓库配置文件。

---

## 4. WebUI 命令行参数（`porter`）

安装 `porter-workflow[web]` 后可用：

```bash
porter [--host 127.0.0.1] [--port 7860] [--share] [--version]
```

| 参数 | 默认值 | 说明 |
| --- | --- | --- |
| `--host` | `127.0.0.1` | 绑定监听地址 |
| `--port` | `7860` | 监听端口 |
| `--share` | `false` | 创建 Gradio 临时公网分享链接 |
| `--version` | — | 打印版本号并退出 |
