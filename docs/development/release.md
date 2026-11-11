# 发布

## 版本号只有一处

`src/porter/__init__.py` 的 `__version__` 是唯一来源：

```toml
[tool.hatch.version]
path = "src/porter/__init__.py"
```

`pyproject.toml` 通过 `dynamic = ["version"]` 读取它，不要在别处再写一个版本号。
发布工作流会先断言 git tag 与 `__version__` 相等，再上传。

## 发布步骤

1. 在 `CHANGELOG.md` 的 `[Unreleased]` 下整理本次变更；必要时把条目移到新的
   版本标题下。
2. 更新 `src/porter/__init__.py` 的 `__version__`。
3. 跑完整门禁（见 [gate.md](gate.md)）。
4. 提交并打 tag：`git tag vX.Y.Z`，推送 tag。
5. 推送 tag 会触发：
   - `.github/workflows/release-pypi.yml`：再次跑门禁，构建 sdist/wheel，
     通过 PyPI Trusted Publishing 发布 `porter-workflow`；
   - `.github/workflows/release-exe.yml`：在 Linux、macOS、Windows 上把
     `scripts/launcher.py` 冻结成启动器，并附到 GitHub Release。

## 构建物内容

```bash
uv build
```

- **wheel**：只包含 `porter` 一个顶层包，里面有 `core/`、`cli/`、`mcp/`、
  `webui/` 和 `py.typed`。入口点：
  `porter = porter.cli.__main__:main`、
  `porter-mcp = porter.mcp.server:main`、
  `porter-web = porter.webui.app:main`。
- **sdist**：包含 `src/`、`skills/`、`scripts/`、`docs/`、README、LICENSE、
  第三方声明和 CHANGELOG；**不包含 `tests/`**。

CI 的 build 任务会逐个断言这些成员和入口点，而不是只检查顶层目录存在。

## EXE 启动器

发布物里的 `porter`/`porter.exe` 不是冻结的引擎，而是
[`scripts/launcher.py`](../../scripts/launcher.py) 冻结成的启动器：

- 首次运行创建 `~/.porter/venv`，安装 `porter-workflow[all]`；
- 周期性刷新 yt-dlp；
- 之后 `exec` 真正的 CLI。

环境变量：

| 变量 | 作用 |
| --- | --- |
| `PORTER_HOME` | 安装根目录，默认 `~/.porter` |
| `PORTER_LAUNCHER_NO_UPDATE` | 设为 `1` 跳过 yt-dlp 刷新 |
| `PORTER_LAUNCHER_SPEC` | 覆盖安装的 requirement |

为什么不冻结引擎、venv 的目录布局，见
[architecture/launcher.md](../architecture/launcher.md)。

## 本地验证发布物

```bash
uv build
python -m zipfile -l dist/porter_workflow-*.whl | head
python -m tarfile -l dist/porter_workflow-*.tar.gz | head
```

然后在干净环境里安装 wheel，确认入口点与导入路径：

```bash
python -m venv /tmp/porter-release-check
wheel="$(ls dist/porter_workflow-*.whl)"
/tmp/porter-release-check/bin/pip install "${wheel}[mcp,web]"
/tmp/porter-release-check/bin/porter --version
/tmp/porter-release-check/bin/python -c "from porter.mcp.server import create_server; create_server()"
/tmp/porter-release-check/bin/python -c "import porter.webui; print('webui import ok')"
```

`porter-mcp` 是 stdio 服务器，没有 `--help`；确认它可用的方式是构造 server
对象，而不是启动后等待输入。
