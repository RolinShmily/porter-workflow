# 发布启动器

发布物里的 `porter` / `porter.exe` **不是**引擎，而是
[`scripts/launcher.py`](../../scripts/launcher.py) 被 PyInstaller 冻结成的启动器。
它只用标准库，负责三件事：

1. 确保 `~/.porter/venv` 存在，并在其中安装 `porter-workflow[all]`；
2. 周期性刷新 yt-dlp；
3. `exec` 真正的 `porter` CLI。

## 为什么不冻结引擎

`yt-dlp` 是决定性的原因：

- 它每周发布站点修复。冻结进二进制后，用户拿到的就是某一周的副本；YouTube 一
  变，发布物失效，修复方式是重新下载几十 MB 的新可执行文件。
- yt-dlp 的 `remote_components` 会在运行时下载 JavaScript challenge solver；
  这些动态路径正是 PyInstaller 冻结文件系统假设容易破坏的东西。

所以二进制只有启动器 + 标准库（几百 KB），引擎留在普通虚拟环境里，可以原地
升级。CLI、MCP server 和技能脚本最终都使用同一个 venv，行为一致。

## 磁盘布局

```
$PORTER_HOME/                 # 默认 ~/.porter
└── venv/                     # 首次运行创建
    └── .ytdlp-refresh        # 上次刷新 yt-dlp 的时间戳
```

## 环境变量

| 变量 | 作用 |
| --- | --- |
| `PORTER_HOME` | 安装根目录，默认 `~/.porter`；容器与测试常用 |
| `PORTER_LAUNCHER_NO_UPDATE` | 设为 `1` 跳过 yt-dlp 周期刷新 |
| `PORTER_LAUNCHER_SPEC` | 安装的 requirement，默认 `porter-workflow[all]`；本地 wheel 发布可指向路径 |
| `PORTER_LAUNCHER_IN_PROGRESS` | 由启动器设置，防止 `uv` 探测解释器时递归重入 |

## 重入防护

启动器历史上有一个严重 bug：`_create_venv` 把 `sys.executable` 传给
`uv venv --python`。当文件作为脚本运行时它是 Python；被 PyInstaller 冻结后，
它是启动器二进制本身。`uv` 会**执行** `--python` 路径来探测版本，于是探测重新
进入启动器，启动器再次调用 uv，形成 970 层递归，219 秒后失败。

现在：

- 冻结模式下绝不把自身作为解释器；
- `PORTER_LAUNCHER_IN_PROGRESS` 标记在子进程环境中传递；
- 检测到重入立即失败并给出可读错误，而不是递归。

`tests/unit/test_launcher.py` 固定了这三点。

## 发布工作流

`.github/workflows/release-exe.yml` 在 Linux、macOS、Windows 上：

1. 用 Python 3.12 编译 `scripts/launcher.py`，确保没有 3.13+ 语法（用户可能用
   3.11 直接运行 `.py` 版本）；
2. `pyinstaller --onefile --name porter ... scripts/launcher.py`；
3. 冒烟测试 `--version`；
4. 把产物附到 GitHub Release。

PyPI 发布是另一条工作流，见 [../development/release.md](../development/release.md)。
