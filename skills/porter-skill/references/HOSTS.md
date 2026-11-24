# 受限宿主与 Windows

技能文件不是可执行环境。`npx skills add` 只复制技能目录，不安装 Python、uv、FFmpeg 或 MCP 服务。宿主可能另外限制启动子进程、命名管道及工作区外写入；**不要为了运行技能默认关闭沙盒**。

通用依赖安装与验证见 [`SETUP.md`](SETUP.md)；本页只讲受限宿主和 Windows 原生入口。

## 入口与依赖

| 宿主 | 入口 |
|---|---|
| Linux / macOS | `python3 <SKILL_DIR>/scripts/porter.py <command>` |
| Windows PowerShell | `py -3 <SKILL_DIR>\scripts\porter.py <command>` |
| 已配置 porter MCP | `porter_capabilities` / `porter_probe` / `porter_plan` / `porter_job_start(plan=...)`；契约见 [`MCP.md`](MCP.md) |

入口只有一个 Python 文件；两边共享同一套 state dir、`--host-check` 与 uvx 逻辑。
先确认 `python3 --version` / `py -3 --version`；没有 Python 时由 Agent 指导安装。

首次运行会联网安装基础包 `porter-workflow`：已有 yt-dlp、平台字幕、免 Key 翻译与压制能力。免费离线语音识别需要 `[asr-local]`（首次还要下载模型）；付费 Whisper API 与 LLM 翻译需要 `[llm]` 和对应 Key。MCP、WebUI 不属于 CLI 入口的必需项，它们解析并执行同一份 `WorkflowPlan`。需要本地 ASR 时设置 `PORTER_SKILL_SPEC='porter-workflow[asr-local]'`，需要付费能力时再加 `,llm`；安装额外依赖前取得用户同意。修改 spec 后，后续 `plan` / `run` / `jobs status` 等调用也应使用同一个 spec 和环境。

## 工作区状态目录：显式选择，而不是推断

先让用户确认宿主**允许写入**的工作区路径，再在每次调用包装器前设置 `PORTER_SKILL_STATE_DIR` 为其中一个**绝对路径**。不要将它设在安装的技能目录里（升级可能重建该目录）。输出视频也必须写在允许的工作区内。

```powershell
$env:PORTER_SKILL_STATE_DIR = 'C:\path\to\allowed-workspace\.state'
py -3 '<SKILL_DIR>\scripts\porter.py' --host-check
py -3 '<SKILL_DIR>\scripts\porter.py' doctor
py -3 '<SKILL_DIR>\scripts\porter.py' plan '<URL>' --json
py -3 '<SKILL_DIR>\scripts\porter.py' run '<URL>' -o 'C:\path\to\allowed-workspace\porter_output'
```

```bash
export PORTER_SKILL_STATE_DIR="$PWD/.state"  # 仅在当前目录获准写入时使用
python3 <SKILL_DIR>/scripts/porter.py --host-check
python3 <SKILL_DIR>/scripts/porter.py doctor
```

启用时，包装器将 uv 的缓存、工具及托管 Python 路径覆盖到该目录，也设置 `XDG_CACHE_HOME`（yt-dlp）和 `PORTER_CACHE_DIR`（作业注册表）。**已有环境变量会被覆盖**：机器级变量也可能指向沙盒外；未启用时则只检查已配置的路径，不修改它们。每次运行或轮询都要保持同一个状态目录，否则作业列表可能不同。`--host-check` 测试这些目录能否创建与写入，不会运行 uvx，也不能保证宿主允许子进程或网络访问。`doctor` 还检查编码器、字体和输出目录；两者都通过才开始作业。

默认的输出目录是 `./porter_output`。若当前目录不可写，显式指定 `-o` 或 `PORTER_OUTPUT_DIR` 到获准路径；不要因为缓存可写就假设媒体输出也可写。`PORTER_SKILL_STATE_DIR` 内包含作业记录与缓存，不要把状态目录提交到仓库，亦不要对其他用户开放。仓库根目录下的 `.state/` 已被 `.gitignore` 忽略。

## 按错误类别处理

| 现象 | 处理 |
|---|---|
| `npx skills add` 报 `spawn EPERM` | 宿主禁止安装器的子进程模式。可经用户确认后按宿主文档手动复制技能目录；不要循环重试安装器或自动提权。 |
| Git Bash 无法创建 signal pipe / WSL 被拦 | 直接用 Windows 的 `py -3 ...\scripts\porter.py`；包装器不依赖 Bash。 |
| uv、yt-dlp、作业注册表的 `access denied` | 确认状态目录获准写入，设置 `PORTER_SKILL_STATE_DIR` 并运行 `--host-check`；检查 `-o` 输出目录。 |
| `Failed to download ... operation timed out` | 这是网络/体积问题；在用户同意后设置 `UV_HTTP_TIMEOUT`、核对代理/镜像或缩小可选 extras。不要改权限。 |
| 安装成功但语音识别不可行 | 用 `plan` 检查有无平台字幕轨、API Key 或已安装的本地 ASR；`doctor` 通过并不保证某一条视频可行。 |

不要用“完全权限”作为默认修复手段。若宿主禁止所有形式的子进程，即便原生包装器也不能让 CLI 工作：必须由用户批准所需操作，或在已配置且能启动的 MCP 宿主中使用现有服务。这里的目录检测不承诺完整视频管线已通过。
