# 架构总览

## 一个引擎，四个前端

| 前端 | 入口 | 面向 |
| --- | --- | --- |
| CLI | `porter` / `python -m porter.cli` | AI Agent（规范入口）、人类与 shell 脚本 |
| MCP | `porter-mcp` / `python -m porter.mcp.server` | AI Agent，stdio JSON-RPC |
| WebUI | `porter-web` / `python -m porter.webui` | 人机协同工作台 |
| Agent Skill | `skills/porter-skill/` | 按 Agent Skills 规范分发的 SOP 与 Python 启动器 |

四者共享同一个 `porter` 引擎与同一套 `WorkflowPlan` 模型。前端只做参数解析、协议适配、
展示和退出码映射；源探测、能力选择、下载、转录、翻译、压制全部在引擎里。这样修一个
bug 不需要改四遍，三个入口的行为也不会漂移。

## 目录映射

```
src/porter/
├── __init__.py        发行包根：稳定公开 API（PEP 562 懒加载），__version__
├── py.typed
├── core/              引擎实现
│   ├── workflow/      七阶段工作流模型、能力注册表、源探测、计划解析与执行
│   ├── pipeline.py    底层四阶段媒体执行编排
│   ├── ports.py       端口协议
│   ├── models/        领域模型（请求、结果、字幕、素材）
│   ├── asr/           语音识别对等后端与单能力执行封装
│   ├── translate/     翻译对等后端与单能力执行封装
│   ├── platforms/     平台识别、下载、原生字幕
│   ├── subtitles/     SRT/ASS、断句、句子重构
│   ├── media/         FFmpeg 封装、标准化、增强、编码、压制
│   ├── jobs/          作业存储与跨进程注册表
│   ├── doctor/        能力探测与修复指南
│   ├── config.py      配置模型与解析
│   ├── context.py     单次运行上下文（事件、取消、logger、config）
│   ├── events.py      结构化事件
│   ├── errors.py      异常层级
│   ├── logging.py     stderr 日志
│   ├── refine/        转录文本精炼
│   ├── mirrors.py     下载镜像选择
│   └── utils/         无依赖工具
├── cli/               CLI 前端
├── mcp/               MCP 前端
└── webui/             Gradio 前端
```

## 依赖方向

```
porter.cli     porter.mcp     porter.webui
      \             |             /
       \            v            /
            porter.core.*
                 |
        models / events / config
                 |
        logging / errors / utils
```

- 前端可以 import 引擎，反之不行。
- 三个前端是兄弟，不是层次；CLI 不能 import MCP，MCP 不能 import WebUI。
- `porter.core` 内部的层序由 import-linter 的 `layers` 契约约束，见
  [layers.md](layers.md)。

## 一次请求的生命周期

以 `porter plan <SOURCE>` → `porter run --plan plan.json`（或一步 `porter run <SOURCE>`，
以及 MCP `porter_plan` → `porter_job_start(plan=...)`、WebUI 预检执行）为例：

1. **解析配置**：`porter.core.config.resolve()` 按优先级合并默认值、文件、
   环境变量和显式路径，结果放进 `PorterConfig`（单次运行只解析一次）。
2. **源探测与计划解析**：`porter.core.workflow.resolve_plan()` 先调用
   `probe_source()` 分类源（URL / 本地视频 / 音频 / 字幕）并读取事实，再按源事实与
   `PlanOverrides` 选择 6 条工作流之一，为每个阶段选定**恰好一个对等能力**，生成
   带内容哈希 `plan_id` 和配置指纹的 `WorkflowPlan`。有阻塞项时 `feasible=false`，
   直接返回结构化 `issues` / `fix` / `alternatives`。
3. **建立上下文**：`RunContext` 持有 `job_id`、选项、事件 sink、取消标志、
   logger 和已解析配置。取消由任意线程置位，阶段之间检查。
4. **执行计划**：`execute_plan(plan, ctx)` 先校验 `feasible` 与配置指纹，再按计划
   中选定的单一能力构造单能力链（无运行期跨能力回退），驱动底层 `Pipeline`（或对
   `subtitle.only` 走字幕直通路径）。
5. **发事件**：引擎通过 `ctx.emit()` 发 `PhaseStarted`、`ProgressUpdated`、
   `ArtifactReady`、`PhaseCompleted` 等结构化事件；CLI 渲染成终端进度，
   MCP 转成工具状态，WebUI 转成流式更新。
6. **写产物**：`raw/` 与 `cooked/` 下的文件在阶段完成时落到磁盘；重跑可以复用
   来源匹配（`origin_prefix` 一致）的已完成阶段，除非 `--force`。
7. **记录作业**：CLI/MCP 的 `JobStore` 把状态发布到共享 `JobRegistry`，
   于是另一个终端或重连的 MCP 客户端也能查询和取消。

## 新代码放在哪里

| 你要加的东西 | 位置 |
| --- | --- |
| 新平台 | `core/platforms/` + `PlatformSpec` |
| 新 ASR 能力 | `core/asr/` 实现 `AsrBackend`，在 `core/workflow/capabilities.py`、`resolver.py`、`executor.py` 注册 |
| 新翻译能力 | `core/translate/` 实现 `TranslationBackend`，在 `core/workflow/capabilities.py`、`resolver.py`、`executor.py` 注册 |
| 新工作流或阶段能力 | `core/workflow/`（先确认 7 阶段模型与底层端口是否够用） |
| 新的可复用媒体操作 | `core/media/` |
| 新的 CLI 命令 | `cli/commands/` + `cli/app.py` |
| 新的 MCP 工具 | `mcp/tools/` + `tools/__init__.py` |
| 新的 WebUI 面板 | `webui/tabs/` 与 `webui/app.py` 的组装 |
| 发布辅助 | `scripts/` |

判断标准：如果三个前端都该拥有这个能力，它属于引擎；如果只有一种协议需要它，
它属于对应前端。
