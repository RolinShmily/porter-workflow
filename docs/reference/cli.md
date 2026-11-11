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
porter run <URL|PATH> [options]
porter inspect <URL> [options]
porter plan <URL|PATH> [options]
porter doctor [options]
porter config {list,get,set,path,import-videocaptioner}
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

## `porter run`

```bash
porter run "<URL>" -o ./out
porter run ./local.mp4 --burn zh_only
porter run "<URL>" --only-phase translate
porter run "<URL>" --json
```

| 选项 | 说明 |
| --- | --- |
| `source` | 视频 URL，或本地文件路径（也接受 `file://`） |
| `-o, --output-dir DIR` | 任务目录的父目录；默认取配置里的 `output_dir` |
| `--burn MODE` | `dual`（默认）/ `zh_only` / `bilingual_only` / `skip` |
| `--asr-engine ENGINE` | 优先尝试该 ASR 后端，其余仍作为回退 |
| `--translator BACKEND` | 优先尝试该翻译后端，其余仍作为回退 |
| `--llm-model MODEL` | 本次运行覆盖 LLM 模型 |
| `--target-lang LANG` | 目标语言，默认 `zh-Hans` |
| `--cookies FILE` | Netscape 格式 cookies.txt |
| `--cookies-from-browser BROWSER` | 从浏览器配置读取 cookie |
| `--subtitle-file FILE` | 用现有 `.srt`/`.vtt` 作为源字幕 |
| `--no-denoise` | 跳过 ASR 人声增强（默认由 `asr.audio_denoise` 决定） |
| `--only-phase PHASE` | 做完该阶段就停；之前的阶段仍会执行 |
| `--force` | 忽略缓存，重做所有阶段 |
| `--json` | 把 `JobResult` 写到 stdout，进度与告警留在 stderr |

`--only-phase` 的准确语义见 [architecture/pipeline.md](../architecture/pipeline.md)。

## `porter inspect`

只探测链接，不下载媒体：平台、标题、时长、分辨率、可用字幕轨。用于在提交长任务
之前确认链接可用。

```bash
porter inspect "<URL>" --json
```

退出码 0 表示链接可用；1 表示当前构建无法处理。**坏链接仍然是一次成功的探测**，
但调用方通常按“能否继续”脚本化，因此不可用链接返回非零。

## `porter plan`

预测作业会做什么、是否可行：使用哪个提取器、原生字幕还是 ASR、哪个翻译后端、
哪些阶段会运行。计划由管线自身的组装逻辑派生，不是另一份推断。

```bash
porter plan "<URL>" --burn skip --json
```

`feasible=false` 时不要启动作业。计划不提供时间预估：没有本机实测的编码速率，
任何数字都是编造的。

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
| `import-videocaptioner` | 从 VideoCaptioner 的 `config.toml` 导入（尚未实现） |

`set` 默认写用户级配置，而不是最近的项目配置，避免意外修改仓库里的文件。

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
- `run`、`inspect`、`plan`、`doctor`、`jobs list/status` 支持 `--json`。
- 结构化结果是稳定接口：字段改名需要迁移说明。
- `porter jobs` 与 `porter inspect` 的 JSON 可能包含路径；脚本应使用
  `json.load` 而不是按行解析。

## 退出码与脚本示例

```bash
porter doctor --json > doctor.json || {
  echo "environment is missing a blocker; see doctor.json" >&2
  exit 1
}

porter plan "$URL" --json | python -c 'import json,sys; sys.exit(0 if json.load(sys.stdin)["feasible"] else 1)'
```
