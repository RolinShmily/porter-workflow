# WebUI 操作指南

WebUI 是**人工通道**：适合用户可视化地预检、启动作业、查看日志与成片、修改配置。
Agent 不能点击页面；职责是启动服务、告诉用户去哪个 Tab、怎么看结果，以及在生产
路径上优先使用 CLI/MCP。

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
| 输入链接 / 本地路径 | 支持平台 URL 或 `file:///...` 本地文件 |
| 快速预检 | 调用 `porter inspect`，显示标题、平台、时长、是否有原生字幕 |
| 压制模式 | `dual`（默认）、`zh_only`、`skip` |
| ASR 引擎 | 自动 / 本地 faster-whisper / Whisper API / VideoCaptioner |
| 目标语言 | 默认 `zh-Hans` |
| 开始 / 取消 | 在当前页面启动作业或请求取消 |
| 实时日志 | 阶段、进度、产物事件；失败时显示错误 |
| 成片预览 | 双语与纯中文成片播放器 |
| 物料下载 | `cooked.zip`、`raw.zip`，以及 raw/cooked 文件列表 |

适合“用户想看着它跑”的场景。长任务仍在后台线程执行，页面只轮询状态。

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
- 当前内置管线通常不会主动触发 HITL；这是给人工介入和扩展流程预留的机制。没有
  待操作作业时提交会被拒绝，并提示当前状态。

### 3. 配置文件与首选项

| 操作 | 说明 |
|---|---|
| 目标文件 | 显示实际写入的配置路径：优先写项目级 JSON 配置；若项目只有 `porter.toml`，则回退到用户级 JSON |
| 可视化表单 | LLM Key/Base/模型、ASR 引擎与本地模型、降噪、Whisper API、字幕样式、cookies |
| 保存表单 | 合并进目标 JSON 文件；文件权限尽量设为 `0600` |
| 原始 JSON | 直接编辑完整配置；可格式化、保存 |
| 重新加载 | 从目标文件重新读取所有字段，包含 `cookies_file` |

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
| 运行诊断 | 调用 `porter doctor` 的能力探测，显示 Component / Status / Details / Guidance |
| 摘要 | 区分 ✅ Ready、⚠️ Degraded、❌ Blocked |

诊断结果与 CLI `porter doctor` 同源；页面上的修复建议是只读文本，实际安装仍需用户
在终端执行。

---

## Agent 如何配合

1. **启动前**：用 CLI/MCP 跑 `doctor` 和 `plan`，把结论告诉用户；不要让用户在一个
   必然失败的配置上点击开始。
2. **启动服务**：由用户决定是否运行；如果 agent 启动，放后台，不要阻塞当前会话。
3. **操作引导**：告诉用户“工作台 Tab 填 URL → 预检 → 选择压制模式 → 开始”，以及
   失败时去“任务总览”看 `Action` 列。
4. **产物交付**：告诉用户成片在 `cooked/`，或直接在下载区取 `cooked.zip`。
5. **自动化优先**：需要脚本化、批处理、长时间无人值守时，用 CLI 或 MCP；WebUI 是
   人工可视化层，不是自动化接口。

---

## 常见问题

| 现象 | 处理 |
|---|---|
| 页面打不开 | 确认 `porter-web` 进程仍在运行；检查 `--host` / `--port` 和防火墙 |
| 文件下载 403 / 路径不允许 | 输出目录必须在当前工作目录或 `output_dir` 内；重启服务后再试 |
| 配置保存到错误位置 | 看“目标文件”字段；项目配置优先于用户配置 |
| 提交 HITL 没反应 | 作业不在 `AWAITING_USER`；先刷新看板确认状态 |
| 预览没有视频 | 作业可能用了 `--burn skip`，或 BURN 阶段失败；去任务总览看成片文件是否存在 |
