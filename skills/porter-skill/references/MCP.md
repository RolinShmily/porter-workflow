# 通过 MCP 使用 porter

本文件供**已配置 porter MCP server** 的环境阅读。全新安装的技能目录里没有 MCP
server（`npx skills add` 只安装技能文件），默认路径是 CLI——见
[`SKILL.md`](../SKILL.md)。环境检查见 [`SETUP.md`](SETUP.md)。

MCP 与 CLI 使用同一个计划模型：`porter_plan` 返回 `WorkflowPlan`，
`porter_job_start(plan=...)` 把它原样交给 `execute_plan`，
`porter_job_result` 返回 `WorkflowResult`。没有第二套预测模型，也没有运行期回退链。

---

## 标准调用顺序

1. `porter_version` / `porter_doctor`：确认入口与环境；`blockers` 非空先修，
   `guidance` 里有可直接执行的步骤。
2. `porter_capabilities`：看每个阶段有哪些**对等**能力、是否可用、成本与数据出口。
3. `porter_probe`：确认源是什么（url / local_video / audio / subtitle）以及平台
   列出的字幕轨；不可用源是 `ok: true` 加 warning，不是工具故障。
4. `porter_plan`：得到可执行计划；读 `feasible`、`issues`、`stages[].capability`。
5. **向用户报告 workflow、阶段、成本与 `egress`，取得同意**。不可行就停，按
   `fix` / `alternatives` 重新规划。
6. `porter_job_start(plan=<porter_plan 的输出>)`：立即返回 `job_id`。
7. `porter_job_status` 轮询到 `done` / `failed` / `cancelled`。
8. `porter_job_result` 取 `stages`、`artifacts` 与结构化 `error`；按
   [`SKILL.md`](../SKILL.md) §5.3 质检后再交付。

不要在同一轮里既传 `plan` 又传 `source` / `transcript` / `burn` 等规划参数：
计划已经固定了这些选择，MCP 会拒绝而不是静默取其一。

---

## 关键契约

### `porter_plan`

接受的规划参数与 CLI `plan` 一致（**没有 cookie 参数**）：

`source`、`task`、`output_dir`、`target_lang`、`burn`、`transcript`、`translate`、
`render`、`subtitle_file`、`llm_model`、`force`、`audio_denoise`、`allow_paid`、
`force_translate`。

返回 `WorkflowPlan.to_dict()` 加：

- `ok`：这次**调用**是否成功；
- `blockers`：`issues` 中 `severity=blocker` 的子集。

不可行计划仍是 `ok: true, feasible: false`。显式命名的能力不可用时，计划阻塞并给出
`alternatives`，不会换成另一个能力。

### `porter_job_start`

- `plan`：`porter_plan` 的返回对象（原样传入）。
- 或 `source` + 同一组规划参数：先解析计划；不可行则拒绝启动。
- 源模式的返回也包含完整 `plan`、`plan_id`、`workflow`、`task` 与阶段摘要。
- 作业在后台线程执行；可被 `porter_job_cancel` 或 CLI `porter jobs cancel` 取消。

### `porter_job_result`

本进程执行的作业返回完整 `WorkflowResult`：每个阶段的 `capability`、`status`、
`outputs`，失败时的 `error.code` / `retryable` / `alternatives` /
`suggested_workflow`。其他进程启动的作业只有扁平注册表记录，返回时会明确说明，
不会伪造阶段结果。

---

## 工具对照表

| 任务 | CLI（权威） | MCP |
|---|---|---|
| 环境自检 | `porter doctor` | `porter_doctor` |
| 阶段能力清单 | `porter capabilities` | `porter_capabilities` |
| 预检源 | `porter probe <src>` | `porter_probe` |
| 规划作业 | `porter plan <source>` | `porter_plan` |
| 执行已保存计划 | `porter run --plan plan.json` | `porter_job_start(plan=...)` |
| 启动作业 | `porter run <src>` | `porter_job_start(source=...)` |
| 查询状态 | `porter jobs status <id>` | `porter_job_status` |
| 取产物 | 读 `WorkflowResult.artifacts` | `porter_job_result` |
| 取消作业 | `porter jobs cancel <id>` | `porter_job_cancel` |
| 列出作业 | `porter jobs list` | `porter_job_list` |
| 查看配置 | `porter config list/get` | `porter_config` |
| 翻译现成 SRT | `porter plan <srt> --task subtitle` | `porter_translate` |
| 压制现成字幕 | `porter plan <video> --subtitle-file ...` | `porter_burn` |
| 转录本地文件 | `porter plan <file> --task transcribe` | `porter_transcribe` |
| 版本 | `porter --version` | `porter_version` |

### 产物级工具

`porter_translate` / `porter_burn` / `porter_transcribe` 吃文件、吐文件，让 agent
能翻译已有 SRT 或把改过的字幕重新压一遍，而不必从 URL 重跑整条管线。

- `porter_translate` 只吃**本地** `.srt`；先解析 subtitle-only 计划，再构建只含计划
  所选那一个翻译能力的链。
- `porter_burn` 只吃**本地**视频 + `.ass`，使用与 BURN 阶段相同的编码器判定。
- `porter_transcribe` 只吃**本地**媒体文件；URL 会被拒绝并提示改用
  `porter_plan(task='transcribe')` + `porter_job_start`。

三者都要求输入能在工具调用超时内完成。拒绝并给出下一步，好过接受然后超时。

---

## 资源与提示

| 原语 | 用途 |
|---|---|
| `porter://docs/architecture` | 七阶段、六工作流、能力目录、平台表（静态派生，不探测） |
| `porter://config` | 解析后的配置，**密钥已打码** |
| `porter://doctor/guides` | 能力缺失的修复步骤 |
| `porter://jobs/{job_id}/log` | 运行中作业的日志 |
| 提示 `localize-video` | doctor → capabilities → probe → plan → 确认 → 启动 → 轮询 → 结果 闭环 |

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

若已用 `python3 scripts/bootstrap.py` 建了虚拟环境，也可直接指向它（避免 `uvx`
首次解析依赖需要联网）：

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

配置后重启客户端，应能看到 14 个 `porter_*` 工具。

---

## 安全约束（设计如此，不是遗漏）

1. **`porter_config` 只读。** 只有 `get` / `list`，**没有写入动作**。通过 MCP 写
   API Key 会把密钥写进对话记录以及随后的遥测。密钥请用 CLI 或环境变量配置。
2. **没有 cookie 参数。** `porter_probe` / `porter_plan` / `porter_job_start` 都不
   接受 `--cookies` / `--cookies-from-browser`。cookie 值同样会进入对话记录。需要
   认证的链接请先用 CLI 把 cookie 写进配置，MCP 侧会从解析后的配置读取。
3. **stdout 纯净。** 引擎内零 `print()`（ruff `T20` 强制）。MCP 的 stdout 是
   JSON-RPC 通道，任何杂输出都会破坏协议；所有日志走 stderr。
4. **并发上限。** 重型作业（下载 / 压制）串行（上限 1），轻量探测（probe / plan）
   并发上限 4——避免 agent 扇出五十个链接开五十个 socket，或并发触发三个压制把机器
   打死。
5. **坏源是一次成功的调用。** `porter_probe` / `porter_plan` 对 404 返回
   `ok: true` 加 `SOURCE_UNUSABLE` warning / blocker。`ok` 描述**调用**；把死链报成
   `ok: false` 会让 agent 去重试一条死链。`porter_job_result` 的 `ok` 才描述作业
   结果。
