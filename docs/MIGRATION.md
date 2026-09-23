# v0.1 → v0.2 迁移指南

本指南面向已经在用 v0.1（`main` 分支，发行名 `porter-skill`）的用户，说明升级到 v0.2（`refactor/porter-workflow`，发行名 `porter-workflow`）后**哪些东西变了、哪些会坏、怎么改**。

v0.2 是一次结构性重构：一个 `porter` 引擎 + 三个前端（CLI / MCP / Agent Skill）。绝大多数心智模型和工作流步骤不变，但有**一条前置条件的变化会让现有用法直接失败**，先看它。

---

## 0. 影响面速览

| 类别 | 变化 | 你需要做什么 |
|---|---|---|
| **转录** | 免 Key 的语音识别路径**已全部失效** | 配 `OPENAI_API_KEY`，或装 VideoCaptioner CLI；否则转录阶段必定失败 |
| **YouTube 下载** | yt-dlp 需要外部 JS 运行时 | 安装 Deno（推荐）或 Node ≥ 20 |
| **发行名 / 入口** | 仓库与发行名改为 `porter-workflow`，入口改为 `porter` / `porter-mcp` | 更新脚本里的命令 |
| **配置位置** | 用户级配置移到 `platformdirs` 目录，新增 `PORTER_CONFIG` | 用 `porter config path` 确认生效文件 |
| **CLI 形态** | 子命令化（`inspect` / `plan` / `jobs` / `doctor` / `config`） | 旧的 `--config-show` 等旗标改写法 |
| **库导入** | `porter_skill` → `porter`（前端拆为 `porter_cli` / `porter_mcp`） | 更新 import |

不需要改的：翻译逻辑（Bing / Google / MyMemory 免 Key 仍然可用）、四阶段语义、产物目录结构（`raw/` + `cooked/`）、大部分配置键。

---

## 1. 最重要：转录不再有免 Key 路径

v0.1 的 `SKILL.md` 宣称「**纯 Python 零 Key 闭环**」，并列出一条免 Key 的 ASR 回退链。**这句话现在是错的。** 所有免 Key 的语音识别端点都已停用——这是实测结论，不是推测，证据记录在 `docs/REFACTOR_PLAN.md` §13.21（测量时间 2026-09-22，10 分钟真实视频）。

| ASR 后端 | v0.1 的说法 | v0.2 实测 |
|---|---|---|
| Whisper API | 需 Key | 需 `OPENAI_API_KEY`（或兼容端点） |
| VideoCaptioner CLI | 可选增强 | 需单独安装 `videocaptioner`，仍可用 |
| Bcut（必剪） | 免 Key 免费 | 主机有响应，但**返回零条 utterance** |
| Google Web（`[stt]`） | 免 Key 兜底 | **每次请求都返回 14 字节 `{"result":[]}`** |

Google Web 的后端还会在截断的 chunked 响应上抛 `http.client.IncompleteRead`；v0.2 已把它映射为 `AsrBackendError`（§13.22），但这只改变报错方式，不改变「它不再转录」的事实。

### 实际后果

没有 Key、且视频**没有平台原生字幕轨**时，作业会在 `transcribe` 阶段以：

```
every speech-to-text backend failed
```

结束。视频会被下载和标准化（`raw/` 产物保留），但拿不到字幕。

### 两种可行做法

```bash
# 方案 A：配置 LLM / Whisper Key（顺便大幅改善翻译质量）
export OPENAI_API_KEY=sk-...
porter "<URL>" --burn skip

# 方案 B：安装 VideoCaptioner，用它的本地引擎
pip install videocaptioner
porter "<URL>" --asr-engine bijian --burn skip
```

### 不受影响的部分

**翻译不需要 Key。** Bing、Google、MyMemory 三个免 Key 后端在 v0.2 中逐个实测可用（§13.21），仍会作为回退链。**平台原生字幕轨也不需要 Key**——YouTube / Bilibili 等有原生轨时根本不跑 ASR。

### 新工具帮你提前发现

v0.2 新增 `porter plan`，会在开工前直接告诉你这条视频走原生轨还是走 ASR、需不需要翻译、以及 `feasible` 是否为 `false`。**先跑 plan 再开作业**，比跑二十分钟后撞上转录失败划算。

`porter doctor` 现在也会报告 JS 运行时、ffmpeg/libass、字体与编码器能力。

---

## 2. 名称与安装

### 改名

| 项 | v0.1 | v0.2 |
|---|---|---|
| GitHub 仓库 | `porter-skill` | `porter-workflow` |
| Python 发行名 | `porter-skill` | `porter-workflow` |
| 可安装的 Skill 名 | `porter-skill` | **仍是 `porter-skill`** |
| CLI 入口 | `python -m porter_skill` / `scripts/run_porter.py` | `porter`（控制台脚本） |
| MCP 入口 | 无 | `porter-mcp` |
| 引擎包 | `porter_skill` | `porter` |

仓库改名后旧地址会 302 重定向，但**脚本里应改成新名字**。Skill 名保持在 Agent Skills 生态中不变，是为了不破坏已安装路径：

```bash
# 仍是这个命令
npx skills add RolinShmily/porter-workflow
```

### 安装方式

```bash
# 免安装运行（推荐）
uvx porter-workflow "<URL>"

# 全后端
uvx --from "porter-workflow[all]" porter "<URL>"

# 持久安装
pipx install "porter-workflow[all]"
```

可选 extras（与 v0.1 类似，但语义更明确）：`[llm]`（OpenAI/Whisper）、`[stt]`（SpeechRecognition，**已实测不可用**）、`[images]`（Pillow）、`[mcp]`（FastMCP）、`[all]`。

> 注意 `[stt]`：v0.1 把它描述为 Google Web STT 兜底；它现在装得上但**不工作**。保留它是为了不改变发行名的依赖语义，不是为了让你依赖它。

---

## 3. 新系统依赖：YouTube 需要外部 JS 运行时

yt-dlp ≥ 2025.11.12 在解析 YouTube 时需要外部 JavaScript 运行时来解 YouTube 的 JS challenge。

| 运行时 | 状态 |
|---|---|
| **Deno** | 推荐（yt-dlp 上游推荐，会被默认启用） |
| Node ≥ 20 | 可用 |
| Bun / QuickJS | 可用 |

```bash
# Deno（推荐）
curl -fsSL https://deno.land/install.sh | sh
deno --version
```

**为什么这是硬依赖：** 没有 JS 运行时时，yt-dlp **不会报错**——它只是返回更少的格式，问题最终表现为一个莫名其妙的 `format not available`。`porter doctor` 会检测 Deno/Node/Bun/QuickJS 并在缺失时给出上述安装步骤。

---

## 4. 配置变化

### 搜索顺序（优先级从高到低）

1. `--config <path>` 显式路径
2. `$PORTER_CONFIG` 环境变量指向的路径
3. 环境变量（`OPENAI_API_KEY`、`PORTER_ASR_ENGINE` 等）
4. **项目级**配置：从当前目录向上查找最近的 `porter.json` / `porter.toml` / `config.json`
5. **用户级**配置：`platformdirs` 的用户配置目录
6. 内置默认值

与 v0.1 的差别在于第 5 项——v0.1 没有用户级配置目录，配置读写围绕 skill / 仓库根目录进行（甚至靠探测 `SKILL.md` 是否存在来选写入目标）。v0.2 把这层部署形态知识从引擎里移除了。

### 旧配置不会丢

- **用户级位置变了**：新版在 `platformdirs` 的 `user_config_dir("porter")` 下（Linux 通常是 `~/.config/porter/config.json`）。用这个命令确认实际生效文件：

  ```bash
  porter config path       # 打印当前生效的配置文件
  porter config list       # 打印解析后的配置（密钥已掩码）
  ```

- **项目级 `config.json` 仍然被识别**：v0.1 常见的「仓库/技能目录下放 `config.json`」照旧有效。
- **旧文件名 `porter_config.json` 仍被兼容**（`config.LEGACY_CONFIG_NAMES`），所以历史遗留的设置不会因为改名而静默失效。

### 环境变量

v0.2 新增 `PORTER_CONFIG`（显式指定配置文件）。已有环境变量继续生效：

- LLM：`OPENAI_API_KEY` / `OPENAI_BASE_URL` / `OPENAI_API_BASE` / `OPENAI_MODEL` / `PORTER_LLM_MODEL`
- ASR：`WHISPER_API_KEY` / `WHISPER_API_BASE` / `WHISPER_MODEL` / `PORTER_ASR_ENGINE`
- 其他：`PORTER_OUTPUT_DIR`、`PORTER_LOG_LEVEL`

密钥只允许走 CLI 或环境变量写入——**不能通过 MCP 写**（见 `docs/MCP.md` §5）。

### 配置键

`llm` / `asr` / `style` 的键名基本沿用 v0.1（`style` 字段名与 v0.1 的 `config.json` 一致），新增 `ffmpeg` 段。完整键表见 `docs/CONFIG.md`。

---

## 5. 新能力

### `porter inspect`（预检）

无需下载媒体即可拿到平台、标题、时长、分辨率、画幅与是否有字幕轨。几秒钟。

```bash
porter inspect "<URL>"
```

判读：`is_valid` 才是「链接可用」，`ok` 只是「调用成功」。链接失效时 `is_valid=false`，**不要重试**。

### `porter plan`（先预测，后投入）

在开工前回答「这条视频会走哪条路、能不能走通」：

```bash
porter plan "<URL>"
```

报告 `phases`、`subtitles.route`（`platform` 或 `asr`）、`translation.needed`、`feasible` / `blocking_issues`、`output_dir`，以及 `notes`（必读——平台字幕轨是 "requested, not guaranteed"，未验证后端会被点名）。`feasible=false` 时不要开作业。

### `porter jobs`（跨进程作业注册表）

v0.1 的长任务只能前台等待或靠「把超时设成 1200 秒」硬扛，失败时还拿不到进度。v0.2 引入持久化到磁盘的作业注册表（`platformdirs` 缓存目录下的 `jobs.json`），**跨进程可见**：

```bash
porter jobs list
porter jobs status <job_id>
porter jobs cancel <job_id>
porter jobs clear
```

`porter run` 在后台运行时会把自己的作业注册进去；另一个终端（或重连的 MCP 客户端）能看到同一批记录。跨进程取消通过共享文件 + 看门狗线程实现，不依赖信号。

### 本地文件输入（v0.2 新功能）

v0.1 只接受 URL。v0.2 直接接受本地路径或 `file://`：

```bash
porter run "/path/to/video.mp4"
porter plan "/path/to/video.mp4"
```

同一份文件的不同写法（相对路径、绝对路径、`file://`）会推导出**同一个**任务目录，重跑可复用 master。注意一个刻意的取舍：**视频旁边的同名字幕 `.srt` 不会被接管**（见 §6）。

### MCP server

v0.2 把引擎作为一等 MCP server 暴露（`porter-mcp`），长任务用 `porter_job_start` + `porter_job_status` 轮询，另有 `porter_inspect` / `porter_plan` / `porter_doctor` / `porter_config` 与三个产物级阶段工具。配置方法与完整工具清单见 `docs/MCP.md`。

---

## 6. 行为变化（升级后可能「不习惯」的地方）

### `--only-phase` 是「到此为止」，不是「只跑这个阶段」

每个阶段要消费上一阶段的输出，所以 `--only-phase translate` 仍会先跑 `prepare` 与 `transcribe`，只是**在 translate 完成后停止**，不进入 burn。

### 本地视频的 sidecar `.srt` 不被接管

放在视频旁边的同名 `.srt` 可能是源语言也可能是译文，猜错要么静默跳过 ASR、要么覆盖用户文件。所以 v0.2 两个都不认领。要翻译现成字幕，用 `porter_translate`（MCP）或直接跑管线并显式提供字幕。

### 平台字幕轨是「请求了」，不是「保证拿到」

平台会对字幕抓取限流（HTTP 429）。计划里报告的是**将被请求**的轨，不是**保证得到**的轨；抓取失败会回退到 ASR。这解释了为什么计划可能预测走原生轨、最终却跑了识别——网络请求的结果不是计划能预知的。

### 成片在发布前会被校验

v0.1 把编码结果**无条件改名**为成品，于是截断的编码会被当作完成的成片发布，而且校验只发生在「复用」路径（恰好是不会产出文件的那条）。v0.2 把成片写到目标旁的临时文件，**probe 确认可读后**才改名就位；ffmpeg 失败或产出不可读会抛 `RenderError`。所以「文件存在」不再等于「压制成功」——质检时仍建议用 `ffprobe` 核对时长与 `moov atom`。

### CLI 输出走 stderr，stdout 留给 `--json`

诊断信息不再混在数据里：机器的输出走 stdout（`--json`），人类的诊断走 stderr。`doctor` 现在也不再创建任何输出目录（只读体检无副作用）。

---

## 7. 库 API 重命名（如果你 import `porter_skill`）

v0.1 是一个大包 `porter_skill`；v0.2 把引擎与前端拆成三个顶层包：

| v0.1 | v0.2 |
|---|---|
| `porter_skill`（引擎 + CLI 混在一起） | `porter`（引擎，纯库，不做参数解析、不写 stdout） |
| `porter_skill.cli` | `porter_cli` |
| 无 | `porter_mcp` |
| `porter_skill.config` | `porter.config` |
| `porter_skill.extractors.*` | `porter.platforms.*` |
| `porter_skill.pipeline.runner` | `porter.pipeline` |
| `porter_skill.subtitle.*` | `porter.subtitles.*` / `porter.translate.*` |
| `porter_skill.synthesizer.burn` | `porter.media.burn` |
| `porter_skill.env_check` | `porter.doctor` |

引擎与前端之间有强制的依赖方向：`porter` 不得 import `porter_cli` / `porter_mcp`（由 import-linter 契约强制执行）。如果你在 v0.1 里依赖了任何「引擎顺手做了 CLI 的事」的行为，需要在 v0.2 里显式调用对应前端。

分层、端口与各模块职责见 `docs/ARCHITECTURE.md`；配置模型与解析见 `docs/CONFIG.md`。

---

## 8. 迁移检查清单

- [ ] 决定转录路线：配 `OPENAI_API_KEY` 或 `WHISPER_API_KEY`，或安装 VideoCaptioner CLI。
- [ ] 安装 JS 运行时（Deno 或 Node ≥ 20），并跑 `porter doctor` 确认。
- [ ] 把脚本里的 `python -m porter_skill ...` 改为 `porter ...`（或 `uvx porter-workflow ...`）。
- [ ] 把 `--config-show` 改为 `porter config list` / `porter config get <key>`；`--config-set` 改为 `porter config set <key>=<value>`。
- [ ] 把 `--doctor` 改为 `porter doctor`；`--inspect` 改为 `porter inspect`。
- [ ] 把 `--skip-burn` / `--only-bilingual` / `--only-zh` 改为 `--burn skip|bilingual_only|zh_only`。
- [ ] 用 `porter config path` 确认新版生效的配置文件位置；必要时把旧配置迁过去。
- [ ] 若脚本 import 了 `porter_skill`，按 §7 的对照表改写。
- [ ] 重新跑一个已知视频，用 `porter plan` 预测、用 §6 的质检方法验收。

---

## 9. 已知缺口（升级前应当知道）

这些是 v0.2 **尚未完成**的部分，不做完整性暗示：

- **免 Key ASR 已死** —— 转录必须有 Key 或 VideoCaptioner CLI（§1）。
- **`--force` 的单阶段从磁盘恢复** —— `force` 对 PREPARE / BURN 的复用判定生效，但「只跑一个阶段」仍需从磁盘恢复中间产物。
- **无字幕视频** —— 未处理。
- **sidecar 字幕** —— 本地视频旁的 `.srt` 不被接管（§6）。
- **MCP sampling** —— 计划中「用宿主模型零 Key 翻译」未实现。
- **MCP 信号处理** —— SIGINT/SIGTERM 的协作取消与临时文件清理未实现（详见 `docs/MCP.md` §4.3）。

---

## 10. 参考

- `README.md` —— 三前端总览、安装、系统依赖、转录前置条件
- `docs/MCP.md` —— MCP 契约、工具清单、安全规则、未实现项
- `docs/ARCHITECTURE.md` —— 四阶段、分层、平台与后端链
- `docs/CONFIG.md` —— 全部配置键、环境变量、搜索顺序
- `docs/REFACTOR_PLAN.md` —— 重构计划与逐项实现记录（§13.x 记录了真实缺陷与实测结论）
- `skills/porter-skill/SKILL.md` —— 面向 agent 的工作流闭环与质检步骤
