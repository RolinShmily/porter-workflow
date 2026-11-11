# 作业

长任务不能塞进一次 MCP 工具调用：1080p 压制要几十分钟，而 MCP 客户端早就会超时。
因此引擎把长工作建模为**作业**，前端用 start/status/result/cancel 暴露它。

作业分两层：

| 层 | 位置 | 回答的问题 | 生命周期 |
| --- | --- | --- | --- |
| 实时层 | `core.jobs.store.Job` / `JobStore` | 这个进程正在跑什么、现在到哪了 | 进程内，线程安全 |
| 持久层 | `core.jobs.records.JobRecord` / `JobRegistry` | 跑过什么、另一个终端或重连的客户端能看到什么 | 共享 JSON 文件 |

`JobStore` 把状态发布到 `JobRegistry`；注册表是投影，不是第二事实源。

## `JobStore`

- 持有 `Job` 对象：请求、状态、时间戳、结果、最近事件环形缓冲、取消标志、
  所属进程标识。
- `attach()` 把 `RunContext` 的事件 sink 接到 job 上，同时启动一个**取消看门狗**
  线程，每秒轮询共享注册表。
- `finish()` 把最终 `JobResult` 写回 job，并强制发布到注册表。
- 限制并发作业数（默认 64），超出时淘汰已完成的旧作业。

## `JobRegistry`

- 一个共享 JSON 文件，路径由 `platformdirs` 决定；测试和受限宿主可用
  `PORTER_CACHE_DIR` 覆盖。
- 写入使用文件锁 + 原子替换，避免并发进程互相覆盖。
- 每条记录带 `pid` 和 `pid_start`：通过 `/proc`（Linux）或 Win32 API 判断
  “这个 PID 是不是当初那个进程”，避免 PID 复用导致误判。
- 读取时可 `reap`：所有者已死且未完成的记录标为失败/取消，而不是永远
  `RUNNING`。

## 状态机

```
PENDING → RUNNING → DONE
                  ├→ FAILED
                  ├→ CANCELLED
                  └→ AWAITING_USER → RUNNING
```

`AWAITING_USER` 是 HITL（Human-in-the-Loop）状态：作业需要人工确认才能继续。
`JobRecord` 里保存 `interaction_required`、`interaction_data` 和
`interaction_response`；WebUI 的任务面板和 MCP 的 job 工具读取这些字段。

## 取消

取消请求写在注册表的 `cancel_requested` 字段，而不是发信号：

- 信号需要知道 PID，而 PID 可能已被复用；MCP 服务器拥有的作业和终端拥有的
  作业也会因此行为不一致。
- 事件钩子不可行：长下载期间根本不产生事件，而用户最想取消的正是这段时间。

看门狗线程每秒轮询注册表，看到请求就置位 `job.cancel`（`threading.Event`），
管线在阶段之间和单位工作之间检查并抛出 `JobCancelled`。MCP 的优雅关闭
（`core.mcp.shutdown`）走同一条路径：对所有本进程作业置位，然后短暂等待线程
收尾。

## 并发上限（MCP）

`porter.mcp.limits` 把并发限制做成独立工件：

| 信号量 | 上限 | 保护什么 |
| --- | --- | --- |
| `HEAVY` | 1 | 下载与压制；一次编码已吃满机器，排队优于互相抢 |
| `LIGHT` | 4 | 网络探测、计划、本地能力检查；避免对同一平台开几十个连接 |

两者都是 `threading.Semaphore`，因为被保护的工作是同步阻塞的（yt-dlp、ffmpeg），
FastMCP 在线程中运行同步工具体。用 `asyncio.Semaphore` 会在工具阻塞时由不同
任务释放，问题只在负载下出现。

## 前端如何用

- CLI：`porter run` 创建 job，`porter jobs list/status/cancel/clear` 读注册表；
- MCP：`porter_job_start` 立即返回 id，`porter_job_status` 轮询，
  `porter_job_result` 取产物，`porter_job_cancel` 请求停止；
- WebUI：任务面板读注册表，展示跨进程作业，并支持 HITL 交互。
