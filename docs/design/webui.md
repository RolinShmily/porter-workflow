# WebUI 设计基线

`porter.webui` 是 Gradio 实现的人机协同（Human-in-the-Loop）工作台。它是
**第四个前端**，不是第二个引擎：所有下载、转录、翻译、压制都调用
`porter.core`。

## 四个 Tab

| Tab | 职责 |
| --- | --- |
| 工作台（Workbench） | URL 预检、参数调整、启动作业、实时进度、成片预览、素材下载 |
| 任务（Jobs & HITL） | 跨进程作业列表、当前 Agent 任务、人工交互回应、历史产物 |
| 设置（Settings） | 可视化配置表单、原始 JSON 视图、保存/重载 |
| 体检（Doctor） | 能力探测表、修复指引、重新检测 |

### 工作台

- 输入 URL 或本地路径后点“预检并生成计划”：调用 `probe_source` + `resolve_plan`，
  显示标题、平台、时长、字幕轨，以及完整 `WorkflowPlan`。
- 规划选项：任务范围、转录能力、翻译能力、目标语言、供字幕文件、付费许可、
  压制模式。每个能力是**单选**，留空表示自动选择；没有回退链选择器。
- 规划选项变化会使已展示的计划失效；执行时重新解析，或拒绝不可行计划并列出
  blocker 与 `fix`。
- 执行阶段把这份计划原样交给 `execute_plan`；右侧实时显示阶段、百分比、日志。
  完成后的成片从 `WorkflowResult.artifacts` 取 `video_bilingual` / `video_zh`。
- 素材区提供 `raw.zip` 与 `cooked.zip`，以及单个文件下载。

### 任务（HITL）

- 顶部只在实际有运行中作业时显示“当前 Agent 任务”，避免空面板占位。
- 表格列出作业 ID、状态、阶段、进度、动作和来源。
- 右侧是 HITL 交互：选择作业、填写 action 名称与 JSON payload、提交回应。
  这对应引擎的 `AWAITING_USER` 状态与 `AwaitingUserAction` 事件。
- 作业来自共享 `JobRegistry`，因此在另一个终端或 MCP server 中启动的任务
  也会出现在这里。

### 设置

- 左侧是分组表单（LLM、ASR、FFmpeg、样式等），右侧是同一份配置的原始 JSON。
- 保存写回当前目标配置文件；重载会重新读取全部字段。`on_reload_all()` 必须
  与保存字段保持一一对应——v0.3.1 修过一个 `cookies_file` 重载遗漏的问题。
- 密钥字段是 password 输入，显示时打码。

### 体检

- 左侧是摘要与“重新检测”，右侧是组件/状态/详情/指引表格。
- 事实来自 `porter.core.doctor.probes`，文案来自 `doctor.guides`；WebUI 不
  自己编修复步骤。

## 设计不变量

1. **一个引擎，第四个前端。** 不复制视频处理或翻译逻辑。
2. **同一份计划。** 工作台展示并执行 `WorkflowPlan`；它不重新推导路线，也没有
   运行期能力回退。计划 JSON 可以交给 CLI `run --plan` 或 MCP `job_start(plan=...)`。
3. **延迟加载。** `import porter.webui` 不要求安装 `[web]`；Gradio 在
   `create_app()` 内部 import。缺少依赖时给出明确的安装提示，而不是 traceback。
4. **与 Agent 解耦。** 富媒体展示（视频播放器、完整字幕列表）留在 WebUI；
   回传给 Agent 会话的只有简洁状态。
5. **不阻塞事件循环。** 长作业在后台线程/作业层执行，Gradio 只轮询状态。
6. **受限路径。** `launch()` 的 `allowed_paths` 限于当前工作目录和配置的
   `output_dir`；不要为了方便放开整个文件系统。
7. **双语。** 只支持 `zh` 与 `en`，两套字典必须键一致；`test_web.py` 有
   parity 测试。
8. **可测试。** `create_app(store=..., registry=...)` 接受注入的作业层，
   `tests/unit/test_web.py` 用假 Gradio 模块验证四个 Tab 的组装与计划辅助函数。

## 模块划分

`app.py` 只保留 Gradio 依赖检查、`create_app()` 和 console script 入口；四个 Tab
分别在 `tabs/workbench.py`、`tabs/jobs.py`、`tabs/settings.py`、`tabs/doctor.py`，
配置读写、素材归档、作业表格式化和体检表格式化分别在 `config_io.py`、
`materials.py`、`jobs_view.py`、`doctor_view.py`。`app.py` 继续 re-export 这些
辅助函数，保持既有调用方不变。行为不变的回归网是 `tests/unit/test_web.py`。
