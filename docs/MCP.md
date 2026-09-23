# MCP 契约（`porter-mcp`）

`porter_mcp` 是 `porter` 引擎之上的一层薄适配器。它不实现业务逻辑，只负责三件引擎刻意不做的事：

1. **协议封装** —— stdio 传输、tool schema、progress notification 的 MCP 形状。
2. **stdout 卫生** —— 在 stdio 传输里 stdout 就是 JSON-RPC 通道，任何杂输出都会破坏协议。
3. **长任务作业化** —— 1080p 压制要几十分钟，远超 MCP tool call 的超时，所以长工作被拆成 start/status/result/cancel 而不是一次阻塞调用。

本文描述**代码实际实现的契约**。`docs/REFACTOR_PLAN.md` §8 是最初的设计意图；两者不一致时以本文（即代码）为准，差异集中在文末「与 §8 的差异」。

面向 agent 的使用说明在 `skills/porter-skill/references/MCP.md`，是工具对照表与客户端配置；本文面向贡献者，讲契约、边界与设计理由。

---

## 1. 运行与接入

### 入口

控制台脚本在 `pyproject.toml` 里注册为 `porter-mcp = "porter_mcp.server:main"`。运行前需要 `[mcp]` extra（它带来 `fastmcp`）：

```bash
# 免安装运行（推荐给 agent 主机）
uvx --from "porter-workflow[mcp]" porter-mcp

# 已装进环境
pip install "porter-workflow[mcp]"
porter-mcp
```

`porter_mcp/__init__.py` 刻意不导入任何重依赖，因此 `import porter_mcp` 在没有 `[mcp]` extra 时也能工作；`fastmcp` 只在 `server.create_server()` 里惰性导入。缺少 extra 时会抛出带安装指引的 `ImportError`，而不是一个难懂的 `ModuleNotFoundError`。

### stdio 客户端配置

```jsonc
{
  "mcpServers": {
    "porter": {
      "command": "uvx",
      "args": ["--from", "porter-workflow[mcp]", "porter-mcp"]
    }
  }
}
```

若已用 `skills/porter-skill/scripts/bootstrap.sh` 建了 `.venv`，可以直接指向它，避免 `uvx` 首次解析依赖需要联网：

```jsonc
{
  "mcpServers": {
    "porter": {
      "command": "/path/to/porter-workflow/.venv/bin/porter-mcp",
      "args": []
    }
  }
}
```

服务器注册 13 个 `porter_*` 工具、3 个静态资源、1 个资源模板和 1 个提示。`server.py` 的 `main()` 在构建服务器**之前**先调用 `porter.logging.configure()`，把日志定向到 stderr——晚一步都可能有一条日志写进 stdout。

---

## 2. 工具清单（以代码为准）

| Tool | 参数（含默认值） | 返回要点 |
|---|---|---|
| `porter_version` | 无 | 引擎版本、Python 版本与实现名 |
| `porter_inspect` | `source: str` | `InspectionResult` + `ok` + `is_valid` + `summary` |
| `porter_plan` | `source: str` | 解析后的计划：`phases`、`subtitles.route`、`translation.needed`、`feasible`、`blocking_issues`、`output_dir` |
| `porter_job_start` | `source`、`output_dir=None`、`burn=None`、`target_lang=None`、`translator=None`、`asr_engine=None`、`llm_model=None`、`only_phase=None`、`force=False`、`audio_denoise=True` | `{job_id, state, source}` |
| `porter_job_status` | `job_id: str` | `state`、`terminal`、`phase`、`percent`、`message`、`elapsed_seconds`、`cancel_requested` |
| `porter_job_result` | `job_id: str` | `artifacts`、`task_dir`、终态与错误 |
| `porter_job_cancel` | `job_id: str` | `{cancelled: bool}`（协作式） |
| `porter_job_list` | 无 | 本进程 + 共享注册表里所有作业，倒序 |
| `porter_translate` | `srt_path`、`target_lang="zh-Hans"`、`backend=None`、`output_dir=None`、`max_cues=0` | 译文 cues + 落盘路径 + `cues_merged` |
| `porter_burn` | `video`、`ass`、`output=None` | 成片路径 + 实际使用的 `encoder` |
| `porter_transcribe` | `source`、`engine=None`、`max_cues=0` | cues + SRT/台词本路径；**拒绝 URL** |
| `porter_doctor` | 无 | 结构化能力报告 + 失败项的修复步骤 |
| `porter_config` | `action="list"`、`section=None` | 解析后的配置（**密钥掩码**） |

### 逐项说明

**`porter_version`** —— 刻意平凡：给协议检查器一个不触网、不写盘、必定成功的调用，用来确认服务器接线正确。

**`porter_inspect`** —— 只探测链接，不下载媒体。参数名用 `source` 而非计划里写的 `url`，与 `porter_job_start` 保持一致：学过其中一个工具的 agent 不该为同一输入再学一个名字。传本地路径会得到明确的解释（「本地文件不需要预检」），而不是误导性的 "unsupported platform"。见 §6 的 `ok` / `is_valid` 语义。

**`porter_plan`** —— 回答「这条作业**会做什么、能不能成**」，与 `porter_inspect` 回答的「这条链接**是什么**」是两个问题。它自己执行一次探测（不接受调用方传入的事实，否则可能基于过期或编造的数据给出计划），并在计划里携带探测结果。不接受 `options`——它描述默认运行；要改参数就传给 `porter_job_start`。

**作业工具** —— `porter_job_start` 立即返回 `job_id`，工作在当前进程的守护线程上继续。`JobStore` 是进程级、长生命周期对象，这正是轮询可行的原因：作业活得比创建它的 tool call 久。它同时把状态投影进共享注册表文件，所以终端里的 `porter jobs` 看得到同一批作业，从那里发出的取消也能停止本服务器的工作。`porter_job_status` 先查本进程再回落到注册表，因此 CLI 启动的作业同样可查。`porter_job_cancel` 是**协作式**的：置一个标志，作业在下一个检查点停下，状态可能短暂仍是 `running`。

**阶段工具（`porter_translate` / `porter_burn` / `porter_transcribe`）** —— 它们操作**产物**而非整条管线：一份 SRT 进、一份 SRT 出；视频 + ASS 进、成片出。这使 agent 能翻译手头已有的字幕、或把改过的字幕重新压一遍，而不必从 URL 重跑。三者接受阻塞调用的前提是**输入的工作能很快做完**（见 §6）。它们不从零推导任何东西：翻译链来自 `Pipeline.default(ctx)`，编码器判定来自压制阶段用的同一个 `EncoderSelector`，所以阶段工具与完整运行不可能对「选了哪个后端/编码器」产生分歧。

**`porter_doctor`** —— 返回 `report`（纯事实，逐项 capability 带 `severity` 与 `remediation_key`）加 `guidance`（只含**失败项**的修复步骤，键就是那个 `remediation_key`）。失败的修复步骤内联返回而非只放资源里：agent 发现阻塞时需要同一步拿到修法，再发一次请求是它可能不会做的一步。完整指南表另外作为 `porter://doctor/guides` 资源暴露。

**`porter_config`** —— 见 §5。

---

## 3. 资源与提示

| 原语 | URI / 名称 | 内容 |
|---|---|---|
| Resource | `porter://docs/architecture` | 架构文档：四阶段、平台表、后端表、工作流 |
| Resource | `porter://config` | 解析后的配置 JSON，密钥掩码 |
| Resource | `porter://doctor/guides` | 全部能力修复指南，Markdown |
| Resource template | `porter://jobs/{job_id}/log` | 某个作业的近期事件流 |
| Prompt | `localize-video`（可选参数 `source`） | inspect → plan → 确认 → start → 轮询 → 质检 的完整闭环 |

### 派生 vs 手写

`docs.py` 的模块 docstring 把规则写清楚了：**散文只能手写**（「为什么管线这样设计」没有东西可供派生），但**本模块里的每一张表都在调用时从引擎读出**：

- 阶段列表来自 `Phase` 枚举；
- 平台表来自 `registry().handlers()` 的 `PlatformSpec`；
- 后端表来自 `Pipeline.default(ctx)` 实际装配出的链的 `.backends`，`endpoint_verified` 由各 backend 自己声明。

所以**加一个后端不可能让文档过期**。这不是理论担忧：bilibili 的 `prefer_existing_chinese` 修复自动反映进了平台表。测试里有两条「派生保证」——平台表必须与 `registry()` 完全一致，后端表必须与实际装配的链完全一致——并且做过反向验证（手写漏更新的旧表会让测试失败）。

架构文档**故意不含实时可用性**：它报告存在哪些后端、线格式是否曾被实测，但不做探测。探测是 `porter_doctor` 与 `porter_plan` 的职责；一个「客户端为了渲染文档而拉取」的资源若偷偷开网络连接，就是在只读操作里塞副作用。

### `porter://config` 与 `porter_config` 的关系

两者返回同一份数据，且**用的是引擎的同一个 `PorterConfig.masked()`**，不是各写一份掩码。有测试断言两者结果相等——同一件事有两个出口时，「它们是否一致」应当是断言而不是假设。

### `localize-video` 是 prompt，不是资源

计划 §8.2 写作资源 `porter://prompts/localize-video`。实现为名为 `localize-video` 的 **MCP prompt**：prompt 才是客户端会渲染成可复用命令的原语，而「引导 agent 走闭环」正是这个意思；做成资源则需被手工拉取并重读。提示接受可选 `source`，会把链接前置进正文。内容不只是步骤清单，还写进了本轮踩过的坑：平台字幕轨是 "requested, not guaranteed"、`blocking_issues` 为真时应当停下而不是开作业、以及为什么必须轮询。

### 作业日志资源

作业日志资源（`job_log`，URI `porter://jobs/{job_id}/log`）只对**本进程拥有**的作业有事件缓冲；其他进程（例如终端里的 `porter run`）启动的作业只能回落到注册表里那条摘要记录，日志里会说明这一点。事件缓冲进程内、易失，这是设计取舍：持久化每次事件是带锁的 read-modify-write，而轮询方每几秒才读一次。

---

## 4. 三条工程硬线

### 4.1 stdout 纯净

在 stdio 传输里 stdout 承载 JSON-RPC 帧。一行杂输出就会让客户端以解析错误断开连接，而故障点离原因很远，极难排查。

两层防线：

1. **引擎层禁止 `print()`** —— ruff 规则 `T20` 在 `src/porter/` 全库启用。`per-file-ignores` 只放宽 `src/porter_cli/**`（CLI 的 stdout 是给人/`--json` 的）与 `tests/**`。引擎的日志全部走 `porter.logging`（stderr）。
2. **边界守卫** —— `porter_mcp/stdout_guard.py` 的 `protect` 装饰器包住**每一个 tool body**。它把 `sys.stdout` 临时换成 `_GuardedStdout`：

   - 生产模式（`strict=False`）：写入被改道到 **stderr** 并记录，tool 仍然成功返回。服务器存活，缺陷留在日志里可见。
   - 测试模式（`strict=True`）：第一次写入就抛 `StdoutViolation`，CI 在**出事那一行**失败，而不是让缺陷出海。

   `protect` 同时支持同步与异步函数。守卫**只包 tool body，不包服务器循环**——FastMCP 必须直接访问真实的 stdout 才能写协议帧。改道的目标必须是 stderr，绝不能是被替换的那个流本身，否则守卫形同虚设。

> 注意与计划 §8.4 的机制差异：计划的代码片段让 `write()` **无条件抛错**，并说「启动时把 `sys.stdout` 换成 guard」。实现更克制：生产期只改道并告警，只有测试才抛；而且守卫范围是 tool body，不是整个 stdout。

### 4.2 并发上限

`porter_mcp/limits.py` 把两条上限收敛到一个模块，而不是每个工具各自建信号量：

```python
HEAVY = threading.Semaphore(1)   # 下载 / 压制：串行
LIGHT = threading.Semaphore(4)   # 探测类：inspect / plan / translate
```

- **HEAVY=1** —— 单次编码已经吃满机器，跑两个只会让两个都慢一倍并让编码器抖动。排队严格优于竞争。排在后面的作业停在 `PENDING`。
- **LIGHT=4** —— 对本机便宜、对远端不便宜。agent 扇出五十个链接不该开五十个 socket，而每次探测还可能重试两次。

**用 `threading.Semaphore` 而非 `asyncio.Semaphore`**：被守护的工作是同步的——yt-dlp 与 ffmpeg 会阻塞调用线程，而 FastMCP 在 worker 线程里运行同步 tool body。`asyncio.Semaphore` 会在 tool body 阻塞的瞬间被**另一个 task** 释放，是一个只在负载下才现形的 bug。

HEAVY 在 `jobs._run_job` 里获取，且 `ctx.check_cancelled()` 在**信号量内部**检查——排队期间被取消的作业轮到自己时不该开始干活。`porter_burn` / `porter_transcribe` 也直接用 HEAVY；`porter_inspect` / `porter_plan` / `porter_translate` 用 LIGHT。

### 4.3 信号处理（规划要求，尚未实现）

计划 §8.4 的第三条硬线是：捕获 `SIGINT` / `SIGTERM` → 取消所有 running job → 清理临时文件 → 退出。

**当前 `src/porter_mcp/` 里没有任何 `signal` / `atexit` / `KeyboardInterrupt` 处理。** 实际行为是：

- 收到 `SIGINT` 时 `server.run(...)` 抛 `KeyboardInterrupt`，进程退出；作业线程是 `daemon=True`，被进程退出**强杀**，没有协作取消，也没有临时文件清理钩子。
- `media/burn.py` 在目标旁写 `.tmp_<name>` 再 `probe` 通过后才改名，被强杀时可能留下 `.tmp_*` 残留。
- 部分兜底来自**注册表**：每条记录带 owner PID 与进程启动标记，读取时 `reap=True` 会把「owner 已死」的未完成记录纠正为陈旧失败（`jobs/records.py:_owner_is_alive` 用 `os.kill(pid, 0)` 加启动时间比对，能识别 PID 回收）。所以一个被强杀的服务器留下的 `running` 作业下次读取时会被清掉——但这是事后纠正，不是优雅退出。

这条差异如实记录，避免文档暗示完整性。

---

## 5. 安全规则

### 5.1 `porter_config` 只读

只允许两个动作，都是读：`list`（段落名、`source`、用户级/项目级配置文件路径、`output_dir`）与 `get`（掩码后的值，可用 `section` 收窄到 `llm` / `asr` / `ffmpeg` / `style` 之一）。

**没有写动作。** 通过 MCP 写 API Key 会把密钥写进对话记录以及随后的遥测。写密钥只允许走 CLI（`porter config set llm.api_key=...`），这也正是 CLI 能从环境变量读密钥的原因。

有测试钉住 `porter_config` 的 `input_schema.properties == {"action", "section"}`：一旦出现 `value` / `api_key` 参数，就是 §8.5 被静默破坏——参数会开始把密钥收进对话记录。

### 5.2 任何工具都没有 cookie 参数

CLI 有 `--cookies` / `--cookies-from-browser`；MCP 侧一个都没有。cookie 值同样会进入对话记录与遥测。需要认证的链接仍然能工作：`porter_inspect` 与 `porter_job_start` 从解析后的配置里读 `cookies_file` / `cookies_browser`，所以**通过 CLI 配置一次**的 cookie 会在 MCP 侧生效——只是不能**从 tool call 里**认证。

### 5.3 掩码只有一个实现

`porter_config` 与 `porter://config` 都调用引擎的 `PorterConfig.masked()` / `porter.config.mask_secret`。工具层**不**自己写掩码：第二套掩码逻辑就是第二个出错的地方，而出错的那一套就是泄漏的那一套。

---

## 6. 值得记住的语义决定

### `ok` 描述调用，`is_valid` 描述链接

`porter_inspect` 对 404 返回 `ok: true, is_valid: false`。把 404 报成 `ok: false` 等于告诉 agent「工具坏了」，而它最可能的反应——重试——恰恰是错的。两个字段都返回，因为它们回答不同问题；把两者混为一谈要花掉 agent 的重试预算。真正的故障（缺依赖、作业被取消）仍然以 `ok: false` 返回。

### 阶段工具只接受「很快能做完」的输入

MCP tool call 超时是一两分钟，而 1080p 压制要几十分钟。所以规则是：**凡是被接受的输入，其工作都能快速结束**。

| 工具 | 接受的输入 | 为什么可以阻塞 |
|---|---|---|
| `porter_translate` | 本地 `.srt` | 无下载、无识别，只有网络往返 |
| `porter_burn` | 本地视频 + 本地 `.ass` | 只有 ffmpeg |
| `porter_transcribe` | **本地**媒体文件 | 无下载 |

`porter_transcribe` 对 URL **明确拒绝**，并指向 `porter_job_start(only_phase="transcribe")`。计划 §8.1 把它的输入写成 `audio|url`；`url` 那一半移交给 job API，因为「先抓 URL」就是一次下载，而下载正是 job API 存在的意义。**拒绝并给出可执行的下一步，好过接受然后超时。**

一个诚实的边界：`porter_translate` 对**已经是句级切分的 SRT** 仍会合并相邻短句（合并是句子级翻译为 ASR 滚动碎片设计的）。它不掩盖这一点：返回 `input_cue_count` / `cue_count` / `cues_merged` 与一条说明。

### 计划不编造耗时

§8.1 要「预估耗时」。可信的数字需要**本机 × 本分辨率 × 本编码器**的实测编码速率，唯一知道它的是试编码。所以 `porter_plan` 报告驱动成本的实测输入（时长、分辨率、跑哪些阶段），并说明其余由什么决定——一个编造的「大约 12 分钟」会被当真，然后在第一个不寻常的视频上出错。

### `endpoint_verified` 是声明，不是实测

后端表与计划里的 `endpoint_verified` 由各 backend 自己声明（不是一张集中维护的表，那会是第二个真相来源）。「未验证」不等于「坏了」：当 ASR 路线**只**依赖未验证端点时，计划加一条 note 而不是 blocking。`porter://docs/architecture` 报告「是否曾被实测」，不做实时探测。

---

## 7. 明确未实现的部分

以「已记录的缺口」取代「暗示的完整性」：

- **§8.3 sampling 未实现。** 计划提出当用户未配置 LLM Key 时，MCP 可发起 `sampling/createMessage` 用**宿主模型**完成翻译与语义纠错——即「零 Key 拿 LLM 级翻译质量」。代码里没有任何 sampling 调用。

  > 附带问题：面向 agent 的 `skills/porter-skill/references/MCP.md` 与 `SKILL.md` 仍把 sampling 当作**现能力**推销（「唯一能拿到零 Key 的 LLM 级翻译」）。这与服务器实际能力不符，需要一并修正或明确标注为未发布特性。

- **`porter_run`（阻塞式运行）未实现。** §8.1 末尾草拟了 `porter_run(..., max_wait_seconds)`；`jobs.py` 的 docstring 明确说明它「无论怎么写都不可能工作」，因为 tool call 超时远早于编码结束。可靠路径只有 job API。

- **信号处理未实现。** 见 §4.3。

- **作业不推送 progress notification。** `porter_mcp/progress.py` 的 `ProgressBridge`（把事件流映射成单调百分比）已实现，但 `porter_job_start` 传入的是 `null_sink`——作业由客户端轮询，而不是流式通知。原因写在 `jobs.py`：MCP progress token 只存在于一次调用的生命周期内，而作业比调用活得久。

---

## 8. 与 §8 的差异（计划意图 vs 代码）

| 项 | 计划 §8 的写法 | 代码实现 |
|---|---|---|
| 阻塞式运行 | `porter_run(..., max_wait_seconds)` | **未实现**；docstring 说明其不可行 |
| Sampling | §8.3 用宿主模型翻译 | **未实现** |
| 信号处理 | §8.4 硬线：捕获 SIGINT/SIGTERM，取消作业并清理 | **未实现** |
| 信号量类型 | `asyncio.Semaphore` | `threading.Semaphore`（同步工作阻塞 worker 线程） |
| stdout guard | `write()` 无条件抛错，替换整个 `sys.stdout` | tool body 内改道到 stderr；仅测试抛错 |
| `porter_inspect` 参数 | `url` | `source`（与 `porter_job_start` 统一） |
| `porter_plan` 参数 | `url, options?` | 只有 `source`；不接受 `options` |
| `porter_transcribe` 输入 | `audio\|url` | **只接受本地文件**，URL 被拒绝并指向 job API；另有 `max_cues` |
| `porter_burn` 参数 | `video, ass, style?` | `video, ass, output`（无 `style`） |
| `porter_config` 参数 | `action: get\|list` | 增加 `section`（用于收窄 `get`） |
| `porter_version` | 未列出 | 已实现（协议自检用） |
| 资源 | 3 个：architecture / config / jobs log | 3 个静态资源 + 1 模板：额外有 `porter://doctor/guides` |
| `localize-video` | 资源 `porter://prompts/localize-video` | 名为 `localize-video` 的 **MCP prompt** |
| 密钥掩码来源 | 复用 `cli.py:_mask_secret` | 引擎 `PorterConfig.masked()`（`cli.py` 已不存在） |

---

## 9. 参考

- `docs/REFACTOR_PLAN.md` §8（契约意图）、§13.33 / §13.35 / §13.38 / §13.41 / §13.42（实现记录与发现的缺陷）
- `docs/ARCHITECTURE.md`（四阶段、分层、后端链）
- `docs/CONFIG.md`（配置键与解析顺序）
- `skills/porter-skill/references/MCP.md`（面向 agent 的工具对照表）
