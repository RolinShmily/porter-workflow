# scripts/

开发与发布辅助脚本。**这里不是 Python 打包配置**，也不是运行时模块。

| 文件 | 用途 |
| --- | --- |
| `launcher.py` | PyInstaller 冻结成 `porter` / `porter.exe` 启动器的源码，由 `.github/workflows/release-exe.yml` 使用 |

## 为什么引擎不冻结

`launcher.py` 只负责确保 `~/.porter/venv` 里有一份可用的
`porter-workflow[all]`，然后 `exec` 真正的 CLI。它**不包含引擎代码**，只用标准库。

原因是 yt-dlp：视频站点变化频繁，yt-dlp 每周发修复。把引擎冻结进二进制，等于把
某一周的 yt-dlp 永久固化；站点一变，发布物就失效，只能重新下载几十 MB 的新版本。
启动器方案让引擎留在普通虚拟环境里，可以原地升级。

## 环境变量

| 变量 | 作用 |
| --- | --- |
| `PORTER_HOME` | 覆盖安装根目录，默认 `~/.porter` |
| `PORTER_LAUNCHER_NO_UPDATE` | 设为 `1` 时跳过 yt-dlp 周期刷新 |
| `PORTER_LAUNCHER_SPEC` | 安装到 venv 的 requirement，默认 `porter-workflow[all]` |

细节见 [docs/architecture/launcher.md](../docs/architecture/launcher.md)，以及
`launcher.py` 顶部的模块 docstring。
