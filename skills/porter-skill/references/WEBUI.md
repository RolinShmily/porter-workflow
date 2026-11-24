# WebUI 操作指南

WebUI 是**人工通道**：适合用户可视化地预检、生成计划、执行、查看日志与成片、修改
配置。Agent 不能点击页面；职责是启动服务、告诉用户去哪个 Tab、怎么看结果，以及在
自动化路径上使用 CLI。

WebUI 与 CLI/MCP 使用同一份 `WorkflowPlan`：工作台先解析计划并展示 JSON，执行时把
这份计划交给 `execute_plan`。没有第二套回退链，也没有旧的 ASR 引擎排列。

---

## 启动

```bash
# 仅本机访问（默认）
uvx --from "porter-workflow[web]" porter-web

# 指定监听地址与端口
uvx --from "porter-workflow[web]" porter-web --host 127.0.0.1 --port 7860

# 生成公开链接（会暴露到互联网，先征得用户同意）
uvx --from "porter-workflow[web]" porter-web --share
```

首次运行 `uvx` 需要联网解析依赖。`--share` 会把工作台暴露给外部网络，默认不要用。

服务启动后，浏览器打开终端打印的地址。允许访问的文件路径限于当前工作目录和配置的
`output_dir`；不要为了方便把整个文件系统加进去。

---

## 四个 Tab 能做什么

### 1. 视频本地化工作台

| 操作 | 说明 |
|---|---|
| 输入链接 / 本地路径 | 平台 URL、`file:///...`、本地视频、音频或字幕文件 |
| 预检并生成计划 | 调用 `probe_source` + `resolve_plan`，显示源事实与 `WorkflowPlan` |
| 任务范围 | `release` / `subtitle` / `transcribe` / `audio` / `probe`；`convert` 尚未接入执行器 |
| 转录能力 | 自动、平台字幕轨、本地 Whisper（免费）、Whisper API（付费）；**单选，无回退** |
| 翻译能力 | 自动、Bing / Google / MyMemory（免 Key）、LLM（付费）；**单选，无回退** |
| 供字幕文件 | 可选 `.srt` / `.vtt`，规划为 `subtitle.supplied` |
| 允许付费 | 未勾选时 `paid` 能力不会被自动选中；显式选择仍会在计划中报告 |
| 压制模式 | `dual`（默认）、`zh_only`、`skip` |
| 计划 JSON | 完整 `WorkflowPlan`，可复制给 CLI `run --plan` 或 MCP `job_start(plan=...)` |
| 开始 / 取消 | 执行当前计划，或在当前页面请求取消 |
| 实时日志 | 阶段、进度、产物事件；失败时显示结构化错误 |
| 成片预览 | 从 `WorkflowResult.artifacts` 取 `video_bilingual` / `video_zh` |
| 物料下载 | `cooked.zip`、`raw.zip`，以及 raw/cooked 文件列表 |

规划选项一改，旧计划即失效；点“执行”会重新解析，而不是偷偷沿用旧选择。计划
`feasible=false` 时不会启动，页面会列出 blocker 与 `fix`。

### 2. 任务总览与人机协作

| 操作 | 说明 |
|---|---|
| 活跃任务卡片 | 当前 RUNNING / AWAITING_USER 作业的 ID、阶段、状态、消息、百分比 |
| 刷新看板 | 重新读取共享作业注册表 |
| 任务表格 | 跨进程的历史作业：ID、状态、阶段、进度、待操作、来源 |
| 点击行 | 选中该作业，填入下方两个下拉框 |
| HITL 表单 | 填 action 名称和 JSON payload，提交给 `AWAITING_USER` 的作业 |
| 历史物料 | 按 job id 读取 `raw.zip` / `cooked.zip` 与文件列表 |

HITL（Human-in-the-Loop）说明：

- 作业进入 `AWAITING_USER` 时，活跃卡片会显示 `interaction_required`。
- 在“人工协作”表单里填 action（例如 `confirm`）和 payload（例如 `{}`），提交后
  写入作业记录的 `interaction_response`，流水线恢复。
- 当前内置工作流通常不会主动触发 HITL；这是给人工介入和扩展流程预留的机制。

### 3. 配置文件与首选项

| 操作 | 说明 |
|---|---|
| 目标文件 | 显示实际写入的配置路径：优先写项目级 JSON 配置；若项目只有 `porter.toml`，则回退到用户级 JSON |
| 可视化表单 | LLM Key/Base/模型、首选转录能力、本地模型、降噪、Whisper API、字幕样式、cookies |
| 保存表单 | 合并进目标 JSON 文件；文件权限尽量设为 `0600` |
| 原始 JSON | 直接编辑完整配置；可格式化、保存 |
| 重新加载 | 从目标文件重新读取所有字段，包含 `cookies_file` |

`asr.engine` 是**偏好**：它只在 ASR 真正运行时影响选择，不强制走 ASR，也不是付费
授权。真正的付费同意仍由计划显式命名或 `allow_paid` 决定。

注意：

- 表单里的 Key 是密码框，但“原始 JSON”会显示明文。**优先用环境变量**
  （`OPENAI_API_KEY` / `WHISPER_API_KEY`），不要把 Key 保存进仓库。
- “保存”优先写项目级 JSON 配置（如果检测到）；这也意味着可能在用户仓库里留下文件。
- 这个页面只写 JSON。项目里的 `porter.toml` 不会被改写；检测到它时页面会提示
  并回退到用户级 JSON 配置。要改 TOML 请手工编辑。
- 修改后建议重新加载并对照 `porter config list` 的 `Source:`。

### 4. 系统体检与环境自检

| 操作 | 说明 |
|---|---|
| 运行诊断 | 调用 `doctor` 的能力探测，显示 Component / Status / Details / Guidance |
| 摘要 | 区分 ✅ Ready、⚠️ Degraded、❌ Blocked |

诊断结果与 CLI `porter doctor` 同源；页面上的修复建议是只读文本，实际安装仍需用户
在终端执行。

---

## Agent 如何配合

1. **启动前**：用 CLI 跑 `doctor`、`capabilities`、`probe` 与 `plan`，把结论告诉
   用户；不要让用户在一个必然失败的配置上点击执行。
2. **启动服务**：由用户决定是否运行；如果 agent 启动，放后台，不要阻塞当前会话。
3. **操作引导**：告诉用户“工作台填 URL → 预检并生成计划 → 检查 blocker 与计划 JSON
   → 执行”，失败时去“任务总览”看 `Action` 列。
4. **产物交付**：告诉用户成片在 `cooked/`，或直接在下载区取 `cooked.zip`。
5. **自动化优先**：需要脚本化、批处理、长时间无人值守或保存计划复现时，用 CLI
   `plan --json` → `run --plan`。WebUI 是人工可视化层，不是自动化接口。

---

## 常见问题

| 现象 | 处理 |
|---|---|
| 页面打不开 | 确认 `porter-web` 进程仍在运行；检查 `--host` / `--port` 和防火墙 |
| 计划不可行 | 看 blocker 的 `code` / `message` / `fix`；安装能力或改规划选项后重新生成 |
| 文件下载 403 / 路径不允许 | 输出目录必须在当前工作目录或 `output_dir` 内；重启服务后再试 |
| 配置保存到错误位置 | 看“目标文件”字段；项目配置优先于用户配置 |
| 提交 HITL 没反应 | 作业不在 `AWAITING_USER`；先刷新看板确认状态 |
| 预览没有视频 | 计划任务可能是 `subtitle` / `transcribe`，或压制模式为 `skip`；查看计划 JSON 的 `stages` |
