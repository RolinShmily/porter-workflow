# 通过 MCP 使用 porter

本文件供**已配置 porter MCP server** 的环境阅读。全新安装的技能目录里没有 MCP server（`npx skills add` 只安装技能文件），所以默认路径是 CLI——见 `SKILL.md`。

---

## 为什么优先用 MCP

| | CLI | MCP |
|---|---|---|
| 全新安装可用 | ✅ 只需 Python + uvx | ❌ 需用户改客户端配置 |
| 结果结构 | `--json` 解析 | **原生结构化**，含 `ok` / `is_valid` 语义区分 |
| 长任务 | 后台 + `porter jobs status` | `porter_job_start` 立即返回 + `porter_job_status` 轮询 |
| shell 转义 / 编码风险 | **有**（非 ASCII 路径、ASCII locale 都踩过） | 无 |
| 架构文档 / 配置 | 读文件 | 资源直接拉取 |
| **宿主模型翻译（零 Key）** | **不可能** | ✅ **唯一途径** |

最后一行是能力差异而非偏好：**MCP sampling（§8.3）用宿主的模型翻译字幕，因此不需要任何 LLM API Key 就能拿到 LLM 级翻译质量。** CLI 是个独立进程，拿不到宿主模型。这是 MCP 形态唯一能提供、而 CLI 形态原理上做不到的东西。

---

## 工具对照表

| 任务 | CLI | MCP 工具 |
|---|---|---|
| 预检链接 | `porter inspect <url>` | `porter_inspect` |
| 规划作业 | `porter plan <source>` | `porter_plan` |
| 启动作业 | `porter run <src> &` | `porter_job_start` |
| 查询状态 | `porter jobs status <id>` | `porter_job_status` |
| 取产物 | 读输出目录 | `porter_job_result` |
| 取消作业 | `porter jobs cancel <id>` | `porter_job_cancel` |
| 列出作业 | `porter jobs list` | `porter_job_list` |
| 环境自检 | `porter doctor` | `porter_doctor` |
| 查看配置 | `porter config list/get` | `porter_config` |
| 翻译现成 SRT | （跑完整管线） | `porter_translate` |
| 压制现成字幕 | （跑完整管线） | `porter_burn` |
| 转录本地文件 | `porter run <file> --only-phase transcribe` | `porter_transcribe` |
| 版本 | `porter --version` | `porter_version` |

### 只有 MCP 才有的三个工具

`porter_translate` / `porter_burn` / `porter_transcribe` 是**产物级**工具：吃文件、吐文件。它们让 agent 能翻译一份已有的 SRT、或把改过的字幕重新压一遍，而不必从 URL 重跑整条管线。

**限制（诚实说明）**：三者都被设计成"接受的输入必须很快能做完"，因为 MCP 工具调用有超时。

- `porter_translate` 只吃**本地** `.srt`
- `porter_burn` 只吃**本地**视频 + `.ass`
- `porter_transcribe` 只吃**本地**媒体文件；**传 URL 会被拒绝**并提示改用 `porter_job_start(only_phase="transcribe")`

拒绝并给出下一步，好过接受然后超时。

---

## 资源与提示

| 原语 | 用途 |
|---|---|
| `porter://docs/architecture` | 架构文档（阶段、平台、后端、工作流），表内容由引擎实时派生 |
| `porter://config` | 解析后的配置，**密钥已打码** |
| `porter://doctor/guides` | 能力缺失的修复步骤 |
| `porter://jobs/{job_id}/log` | 运行中作业的日志 |
| 提示 `localize-video` | 完整的 inspect → plan → 确认 → 启动 → 轮询 → 质检 闭环，可带 `source` 参数 |

`porter://docs/architecture` 里的平台表与后端表是**从引擎读出来的**（平台来自注册表，后端来自实际装配的管线），所以新加一个后端会自动出现。它报告"端点是否曾被实测"，**不做实时探测**——实时可用性请用 `porter_doctor` 或 `porter_plan`。

---

## 配置 MCP server

```bash
# 先确认能独立运行（会打印服务器信息后等待 stdin）
uvx --from "porter-workflow[mcp]" porter-mcp
```

然后加入客户端配置。**stdio** 形式：

```jsonc
// 通用 MCP 客户端配置（键名随客户端而异，此处以 pi / Claude Desktop 风格为例）
{
  "mcpServers": {
    "porter": {
      "command": "uvx",
      "args": ["--from", "porter-workflow[mcp]", "porter-mcp"]
    }
  }
}
```

若已用 `bootstrap.sh` 建了虚拟环境，也可直接指向它（避免 `uvx` 首次解析依赖需要联网）：

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

配置后重启客户端，应能看到 13 个 `porter_*` 工具。

---

## 安全约束（设计如此，不是遗漏）

1. **`porter_config` 只读。** 只有 `get` / `list`，**没有写入动作**。通过 MCP 写 API Key 会把密钥写进对话记录以及随后的遥测。密钥请用 CLI 或环境变量配置。
2. **没有 cookie 参数。** `porter_inspect` 不接受 `--cookies` / `--cookies-from-browser`（CLI 有）。cookie 值同样会进入对话记录。需要认证的链接请先用 CLI 把 cookie 写进配置，MCP 侧会从解析后的配置读取。
3. **stdout 纯净。** 引擎内零 `print()`（ruff `T20` 强制）。MCP 的 stdout 是 JSON-RPC 通道，任何杂输出都会破坏协议；所有日志走 stderr。
4. **并发上限。** 重型作业（下载 / 压制）串行（上限 1），轻量探测（inspect / plan / translate）并发上限 4——避免 agent 扇出五十个链接开五十个 socket，或并发触发三个压制把机器打死。
5. **坏链接是一次成功的调用。** `porter_inspect` 对 404 返回 `ok: true, is_valid: false`。`ok` 描述**调用**，`is_valid` 描述**链接**；把 404 报成 `ok: false` 会让 agent 去重试一条死链。

---

## 与技能的关系

技能提供**领域知识**（四阶段语义、回退链、质检步骤、ASR 前置条件），MCP 工具自带**接口说明**（名称、描述、JSON schema）。两者不重复：技能正文不需要复述 MCP 工具签名——那样只会多出一份需要同步维护的副本。

所以：**CLI 讲工作流，MCP 讲升级**。无论走哪条路，工作流的步骤与判断标准完全相同。
