# 环境搭建

## 前置条件

| 依赖 | 版本 / 说明 |
| --- | --- |
| Python | ≥ 3.11；CI 覆盖 3.11–3.13 |
| [uv](https://docs.astral.sh/uv/) | 环境与依赖管理，仓库只维护 `uv.lock` |
| FFmpeg + ffprobe | 必须带 `libass`；媒体层和测试都会真实调用 |
| Deno 或 Node ≥ 20 | 仅 YouTube 下载需要，用于 yt-dlp 的 JavaScript challenge；测试不需要 |

`porter doctor` 会检查并报告这些能力，并给出各平台的安装命令。

## 安装

```bash
git clone https://github.com/RolinShmily/porter-workflow
cd porter-workflow
uv sync --extra all --extra dev
```

- `--extra all` 安装 LLM、MCP、WebUI、本地 Whisper 和图片处理的可选依赖。
- `--extra dev` 安装 pytest、ruff、mypy 和 import-linter。
- 只改引擎、不碰可选前端时，可以省略 `--extra all`；零配置路径必须在不安装这些
  extras 的情况下仍然可用。CI 的 hermetic 任务会验证这一点。

## 锁定文件

`uv.lock` 是提交进仓库的依赖事实源。CI 使用：

```bash
uv sync --locked
```

如果 `pyproject.toml` 与锁文件不一致，这条命令会失败。因此：

- 添加、删除或升级依赖时，在同一个提交里运行 `uv lock`。
- 不要手工编辑 `uv.lock`。
- 不要在没有说明的情况下新增依赖；许可证规则见 [gate.md](gate.md#licence-rules)。

## 使用特定 Python 版本

如果本机默认解释器不等于 CI 版本，可以让 uv 只把项目环境放在临时目录，
避免覆盖仓库里已有的 `.venv`：

```bash
UV_PROJECT_ENVIRONMENT=/tmp/porter-dev-venv uv sync --python 3.13 --extra all --extra dev --locked
UV_PROJECT_ENVIRONMENT=/tmp/porter-dev-venv uv run pytest -m "not network"
```

## 运行

```bash
uv run porter --version
uv run porter doctor
uv run porter capabilities
uv run porter probe "<URL>"
uv run porter plan "<URL>" --json > plan.json
uv run porter run --plan plan.json -o ./porter_output
uv run porter-web                       # 需要 [web]
uv run porter-mcp                       # 需要 [mcp]，stdio 传输
```

## 常见问题

| 现象 | 原因 / 处理 |
| --- | --- |
| `porter doctor` 报 FFmpeg 缺 libass | 系统 FFmpeg 构建未启用 libass；换用带 libass 的发行版包或重新构建 |
| YouTube 下载要求 JS runtime | 安装 Deno（推荐）或 Node ≥ 20，再跑 `porter doctor` |
| 编码时报 `Unknown encoder 'libx264'` | FFmpeg 构建不含 libx264；这是本机媒体环境问题，不是 porter 的依赖问题 |
| 本地 Whisper 首次运行很慢 | `[asr-local]` 需要先下载模型；之后可离线复用 |
