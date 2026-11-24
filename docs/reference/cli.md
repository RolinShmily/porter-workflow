# CLI 参考

入口：

```bash
porter ...                 # 安装后的 console script
python -m porter.cli ...   # 等价的模块入口
```

本页描述命令结构与约定。每个命令的完整参数以 `porter <cmd> --help` 为准。

## 命令树

```
porter <URL>                          # `porter run <URL>` 的简写
porter capabilities [options]          # 当前安装能执行的能力
porter probe <URL|PATH> [options]      # 源事实，不做计划
porter plan <URL|PATH> [options]       # 解析 WorkflowPlan，不执行
porter run [<URL|PATH>] [options]      # 解析并执行；或 `--plan FILE`
porter inspect <URL> [options]         # 旧名，等价于 probe（保留兼容）
porter doctor [options]
porter config {list,get,set,path}
porter jobs {list,status,cancel,clear}
```

裸 URL 简写会跳过前置全局选项及其值，因此
`porter --config c.json <URL>` 仍然等价于 `porter --config c.json run <URL>`。

## 全局选项

| 选项 | 说明 |
| --- | --- |
| `--version` | 打印版本并退出 |
| `--config PATH` | 显式配置文件（JSON/TOML），优先于 `$PORTER_CONFIG` |
| `--log-level LEVEL` | `debug` / `info` / `warning` / `error`；也可用 `PORTER_LOG_LEVEL` |

## `porter capabilities`

列出七个阶段（probe / acquire / normalize / transcript / translate / write /
render）各自的**平级能力**，以及探测结果、成本与数据出口。

```bash
porter capabilities
porter capabilities --stage transcript
porter capabilities --available --json
```

| 选项 | 说明 |
| --- | --- |
| `--stage STAGE` | 只看一个阶段 |
| `--available` | 只列出探测成功的能力；不可用条目默认也显示，用于区分“没装”和“不支持” |
| `--json` | 输出 JSON 目录 |

成本为 `paid` 的能力不会被自动选中；音频/文本出口为 `egress`，用于在启动前
向用户说明数据会离开本机。

## `porter probe`

描述源是什么：`kind`（url / local_video / audio / subtitle）、平台、标题、
时长、分辨率与平台列出的字幕轨。链接只抓元数据，不下载媒体。

```bash
porter probe "<URL>"
porter probe ./local.mp4 --json
```

| 选项 | 说明 |
| --- | --- |
| `--cookies FILE` | Netscape 格式 cookies.txt |
| `--cookies-from-browser BROWSER` | 从浏览器配置读取 cookie |
| `--json` | 输出 `SourceProbe` JSON |

退出码 0 表示源可用；1 表示源本身不可用。**坏链接仍然是一次成功的探测**，
但调用方通常按“能否继续”脚本化，因此不可用源返回非零。平台列出的字幕轨会带
`TRACKS_UNVERIFIED` 警告：轨是请求而非保证。

`porter inspect` 保留为旧名，只接受链接；新脚本请使用 `probe`。

## `porter plan`

解析作业将要执行的内容：`workflow`（`url.subtitled`、`url.asr`、
`local.subtitled`、`local.asr`、`subtitle.only`、`audio.asr`）、每个阶段唯一
选中的能力、成本、数据出口、`feasible` 与 `issues`。**不下载媒体，不运行
ffmpeg。** `--json` 的输出可原样交给 `porter run --plan`。

```bash
porter plan "<URL>" --burn skip --json > plan.json
porter plan ./clip.mp4 --transcript whisper-local
```

| 选项 | 说明 |
| --- | --- |
| `-o, --output-dir DIR` | 任务目录的父目录；默认取配置里的 `output_dir` |
| `--task TASK` | `probe` / `transcribe` / `subtitle` / `release`（默认）/ `convert` / `audio` |
| `--burn MODE` | `dual`（默认）/ `zh_only` / `bilingual_only` / `skip` |
| `--transcript CAPABILITY` | 只选该转录能力：`whisper-local`、`whisper-api`、`platform-track`、`supplied`；无回退 |
| `--translate CAPABILITY` | 只选该翻译能力：`bing`、`google`、`mymemory`、`llm` |
| `--render CAPABILITY` | 只选该渲染能力；当前为 `ffmpeg-libass` |
| `--subtitle-file FILE` | 用现有 `.srt`/`.vtt` 作为源字幕，规划 subtitled 工作流 |
| `--llm-model MODEL` | 显式选择 `translate.llm` 时使用的模型 |
| `--target-lang LANG` | 目标语言，默认 `zh-Hans` |
| `--cookies FILE` | Netscape 格式 cookies.txt |
| `--cookies-from-browser BROWSER` | 从浏览器配置读取 cookie |
| `--no-denoise` | 规划时不启用 ASR 人声增强 |
| `--allow-paid` | 允许在没有免费替代时选择付费能力；显式命名的付费能力始终允许 |
| `--force-translate` | 即使源自带目标语言轨也执行翻译 |
| `--force` | 执行时忽略缓存阶段 |
| `--only-phase PHASE` | 已废弃，映射到 `--task`：prepare=audio、transcribe=transcribe、translate=subtitle、burn=release |
| `--json` | 输出 `WorkflowPlan` JSON |

`--asr-engine` / `--translator` 是 `--transcript` / `--translate` 的旧别名。它们
**只选中一个**能力，不是“优先尝试”；指定的能力不可用时计划阻塞并给出
`alternatives`，不会换一个继续。`feasible=false` 时不要启动作业。

## `porter run`

```bash
porter run "<URL>" -o ./out
porter run ./local.mp4 --burn zh_only
porter run --plan plan.json
porter run "<URL>" --task subtitle
porter run "<URL>" --json
```

除 `-o/--output-dir` 与 `--json` 外，`run` 接受与 `plan` 相同的规划选项，先解析
再执行。`--plan FILE` 则跳过解析，严格执行保存的计划（源参数可省略，且不能再传
规划选项）。

| 选项 | 说明 |
| --- | --- |
| `source` | 视频 URL、本地媒体路径或字幕文件；使用 `--plan` 时可省略 |
| `--plan FILE` | 执行之前保存的 `plan.json` |
| `-o, --output-dir DIR` | 任务目录的父目录；默认取配置里的 `output_dir` |
| 规划选项 | 同 `plan`：`--task`、`--burn`、`--transcript`、`--translate`、`--render`、`--subtitle-file`、`--llm-model`、`--target-lang`、`--cookies`、`--cookies-from-browser`、`--no-denoise`、`--allow-paid`、`--force-translate`、`--force` |
| `--json` | 把 `WorkflowResult` 写到 stdout，进度与告警留在 stderr |

执行器只做**同一能力内的重试**，不跨能力回退。失败结果包含 `error.code`、
`retryable`、`alternatives` 与 `suggested_workflow`；换能力需要重新 `plan`。

## `porter doctor`

检查 Python、FFmpeg/libass、yt-dlp + JS runtime、凭证与可选依赖。退出码 0 表示
没有缺失的 BLOCKER 能力，1 表示有。

```bash
porter doctor --json
porter doctor --verbose
```

## `porter config`

| 子命令 | 作用 |
| --- | --- |
| `list` | 打印有效配置（密钥打码）与来源 |
| `get KEY` | 打印一个解析后的键，密钥打码 |
| `set KEY=VALUE` | 写入用户级配置；`--file PATH` 可指定 JSON 目标（TOML 需手工编辑） |
| `path` | 打印当前生效的配置文件 |

`set` 默认写用户级配置，而不是最近的项目配置，避免意外修改仓库里的文件。
`import-videocaptioner` 已移除：旧 CLI 不再是后端。

## `porter jobs`

| 子命令 | 作用 |
| --- | --- |
| `list [--all] [--json]` | 列出作业；默认只显示未完成的 |
| `status JOB_ID [--json]` | 阶段、进度、耗时 |
| `cancel JOB_ID` | 请求停止；由所有者进程的看门狗执行 |
| `clear` | 从注册表删除已完成的作业 |

作业注册表是跨进程的：终端 B 能看到终端 A 或 MCP server 启动的作业。

## 退出码

| 码 | 含义 |
| --- | --- |
| `0` | 成功 |
| `1` | 失败（作业失败、链接不可用、doctor 发现 blocker 等） |
| `2` | 用法错误（未知选项、缺少参数） |
| `130` | 用户取消（Ctrl+C / job cancel） |

## `--json` 约定

- stdout **只输出 JSON 文档**；进度、警告、日志一律走 stderr。
- `run`、`probe`、`inspect`、`plan`、`capabilities`、`doctor`、
  `jobs list/status` 支持 `--json`。
- 结构化结果是稳定接口：字段改名需要迁移说明。
- `porter jobs`、`probe` 的 JSON 可能包含路径；脚本应使用 `json.load` 而不是
  按行解析。

## 脚本示例

```bash
porter doctor --json > doctor.json || {
  echo "environment is missing a blocker; see doctor.json" >&2
  exit 1
}

porter plan "$URL" --json > plan.json
python -c 'import json,sys; sys.exit(0 if json.load(open("plan.json"))["feasible"] else 1)'
porter run --plan plan.json
```
