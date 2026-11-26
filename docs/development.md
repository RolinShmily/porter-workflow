# 开发指南

详见配套文档：[`architecture.md`](architecture.md)（分层与模块）、[`design.md`](design.md)（工程原则）、[`reference.md`](reference.md)（公开 API 与配置）。

## 1. 环境搭建

需要 **Python 3.11–3.13**、[uv](https://docs.astral.sh/uv/)、**FFmpeg + ffprobe（含 `libass`）**，以及（YouTube 实测）**Deno** 或 **Node ≥ 20**。

```bash
git clone https://github.com/RolinShmily/porter-workflow
cd porter-workflow
uv sync --extra web --extra dev --locked
```

改动 `pyproject.toml` 依赖后必须同步 `uv lock`（CI 强制 `--locked`）。

## 2. 四道门禁

提交前在仓库根目录通过与 CI 一致的四道检查：

```bash
uv run ruff check src tests skills   # 风格、排序、T20（引擎禁止 print）
uv run mypy src                      # strict 类型检查（platform=linux, py3.11）
uv run lint-imports                  # 引擎 / 前端边界与 12 层契约
uv run pytest -m "not network"       # 完整离线测试（含 slow）
```

- **测试分层**：`tests/unit/`（无系统依赖）、`tests/regression/`（行为等价回归）、`tests/integration/`（真实 FFmpeg）；`-m slow` 依赖 FFmpeg，`-m network` 手动按需运行。CI 的 `hermetic` 任务会隐藏 FFmpeg 跑非 `slow` 测试，新增非 `slow` 测试不得假设本机有 FFmpeg。
- **静态架构测试**（`tests/unit/test_architecture.py`）：额外校验 `src/porter/` 文本写入显式 `encoding="utf-8"`、根包 `_LAZY_EXPORTS`/`TYPE_CHECKING`/`__all__` 三者一致。

## 3. 扩展点

| 扩展目标 | 位置与步骤 |
| --- | --- |
| **新平台** | `core/platforms/spec.py` 声明 `PlatformSpec` 并在 `registry.py` 注册，`urls.py` 补归一化；特殊站点参照 `bilibili.py`/`youtube.py` 实现提取器；标准化走 `media/prepare.py`。 |
| **新 ASR 能力** | `core/asr/` 实现 `AsrBackend`，在 `workflow/{capabilities,resolver,executor}.py` 注册并声明 `cost`/`egress`。 |
| **新翻译能力** | `core/translate/` 实现 `TranslationBackend`，接入 `workflow/{capabilities,resolver,executor}.py`；中文译文须过 CJK 自检。 |
| **新 WebUI 面板** | 业务逻辑先沉淀到 `porter.core` 门面，再在 `webui/tabs/` 适配，保持 `webui/i18n.py` 中英键一致。 |

## 4. 许可证规则

### Licence rules

The English heading above is kept stable for GitHub issue template anchors (`docs/development.md#licence-rules`).

- Porter Workflow is **MIT** licensed; contributions are accepted under the same terms.
- **No hard dependency on a copyleft package.** GPL/AGPL tools stay behind a subprocess boundary (e.g., user-installed FFmpeg) and are never imported in-process.
- Do not copy third-party source code, dictionaries, or binaries into the repository without adding a `THIRD_PARTY_NOTICES` file reproducing their upstream copyright and full licence text.

## 5. 发布与版本管理

### 5.1 主线发布（`v*` → PyPI + GitHub Release）

`src/porter/__init__.py` 的 `__version__` 是唯一版本号来源（`pyproject.toml` 动态读取），变更记录在根目录 [`CHANGELOG.md`](../CHANGELOG.md)。

1. 更新 [`CHANGELOG.md`](../CHANGELOG.md) 与 `src/porter/__init__.py` 的 `__version__`。
2. 跑通第 2 节全部四道门禁，确认 `uv build` 产物（wheel 仅含 `porter` 与 `porter = porter.webui.app:main`；sdist 含 `src/`、`skills/`、`docs/`，不含 `tests/`）。
3. 提交并推送 tag：`git tag vX.Y.Z && git push origin main vX.Y.Z`：
   - `.github/workflows/publish.yml` 自动跑门禁并发布 `porter-workflow` 至 PyPI；
   - PyPI 上传成功后自动通过 `workflow_call` 调用 `.github/workflows/release.yml`，以 `porter-workflow vX.Y.Z` 为标题创建 GitHub Release（Release notes 取自根 `CHANGELOG.md` 的对应版本；也可手动 `workflow_dispatch` 补建 Release）。

### 5.2 Agent Skill 不单独版本化

`skills/porter-workflow/` 通过 `npx skills add` 直接从 `main` 分发，随仓库提交演进：不写 `metadata.version`，不维护独立 CHANGELOG，也不打 `skill-v*` tag。技能改动记录在根 [`CHANGELOG.md`](../CHANGELOG.md)。
