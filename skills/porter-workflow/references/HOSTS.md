# 受限宿主与 Windows 使用说明

`npx skills add` 只安装技能文件，不会安装 Python、uv 或 FFmpeg。宿主可能限制沙盒外
写入；**不要为了跑通技能而关闭沙盒或默认提权**。

## 跨平台调用方式

| 平台 | 命令 |
|---|---|
| Linux / macOS | `python3 <SKILL_DIR>/scripts/porter_cli.py <cmd>` |
| Windows PowerShell | `py -3 <SKILL_DIR>\scripts\porter_cli.py <cmd>` |

脚本仅依赖 Python 标准库（3.8+），未安装 `porter>=0.4.0` 时自动通过
`uv run --no-project --python >=3.11,<3.14 --with porter-workflow>=0.4.0` 拉起隔离运行
环境，无需手动建虚拟环境，也能自动处理 Windows 的 `uv.cmd` shim。

## 受限宿主：显式设置 `PORTER_SKILL_STATE_DIR`

向用户确认获准写入的工作区路径，将 `PORTER_SKILL_STATE_DIR` 设为其中的**绝对路径**
（不要放在技能安装目录内），并在同一会话的每次调用中保持一致：

```powershell
$env:PORTER_SKILL_STATE_DIR = 'C:\path\to\workspace\.state'
py -3 '<SKILL_DIR>\scripts\porter_cli.py' --host-check
py -3 '<SKILL_DIR>\scripts\porter_cli.py' doctor
py -3 '<SKILL_DIR>\scripts\porter_cli.py' plan '<URL>' -o 'C:\path\to\workspace\porter_output' --out plan.json
py -3 '<SKILL_DIR>\scripts\porter_cli.py' run --plan plan.json --out result.json
```

```bash
export PORTER_SKILL_STATE_DIR="$PWD/.state"
python3 <SKILL_DIR>/scripts/porter_cli.py --host-check
python3 <SKILL_DIR>/scripts/porter_cli.py doctor
```

设置后，脚本会自动把 `UV_CACHE_DIR`、`UV_TOOL_DIR`、`UV_PYTHON_INSTALL_DIR`、
`XDG_CACHE_HOME` 与 `PORTER_CACHE_DIR` 全部收拢到该目录内。

## 常用环境变量覆盖

| 变量 | 用途 |
|---|---|
| `PORTER_SKILL_STATE_DIR` | 受限宿主下的可写状态根目录（绝对路径） |
| `PORTER_SKILL_SPEC` | 覆盖包版本或指向本地源码目录（如 `.`），默认 `porter-workflow>=0.4.0` |
| `PORTER_SKILL_WEB_SPEC` | 覆盖 `webui` 子命令的包规格，默认 `porter-workflow[web]>=0.4.0` |
| `PORTER_SKILL_FORCE_UV` | 设为 `1` 强制经 `uv run --with` 执行，忽略当前环境已装的 `porter` |
| `PORTER_MIRROR` | `cn` / `off` 控制国内镜像（USTC / ModelScope） |
