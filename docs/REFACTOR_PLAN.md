# Porter Workflow 重构操作手册

> 分支：`refactor/porter-workflow`（从 `main` 创建，已清空旧实现）
> 目标版本：`v0.2.0`
> 发行包名：`porter-workflow`（PyPI 可用）
> 状态：本文件为**唯一交付物**，旧代码保留在 `main` 分支作为迁移参照。

---

## 0. 本手册的使用方式

1. 旧实现全部可从 `main` 取回，**不要**在新分支上重新发明任何逻辑：
   ```bash
   git show main:porter_skill/subtitle/formatter.py            # 看单文件
   git restore --source=main -- porter_skill/                   # 整目录取回（仅作参照，勿提交）
   ```
2. 按 **§9 分阶段执行手册** 逐阶段推进，每个阶段有明确的**验收命令**。验收不过就不进入下一阶段。
3. **行为等价性是硬门槛**：§9-P2 之前，`main` 上那 90 个测试必须能在新结构下全绿。结构可以换，行为不能变。
4. 本手册中标注 `【决策】` 的条目已经拍板，标注 `【待定】` 的需要在对应阶段开始前确认。

---

## 1. 重构目标与非目标

### 1.1 目标

| # | 目标 | 可度量的验收标准 |
|---|---|---|
| G1 | 仓库更名 `porter-skill` → `porter-workflow` | GitHub 旧地址 302 重定向生效；README/徽章/badge 无 `porter-skill` 残留（skill 名除外） |
| G2 | 引擎从 CLI 中剥离为独立库层 | `porter_mcp` 与 `porter_cli` 都不含业务逻辑，二者只调用 `porter.*` 公共 API |
| G3 | 消除假抽象与复制粘贴 | 5 个 extractor 从 2565 行降至 ≤ 700 行；`_convert_vtt_to_srt` 等重复符号全局唯一 |
| G4 | 让 MCP 成为一等公民 | `porter-mcp` 在 `npx @modelcontextprotocol/inspector` 下所有 tool 可调；stdout 零污染 |
| G5 | 依赖声明与实际使用一致 | 死依赖清零；零配置路径（纯 Python + FFmpeg）不再硬拉 `openai`/`pillow`/`SpeechRecognition` |
| G6 | 许可证合规 | `videocaptioner`（GPL-3.0）永不进入 `dependencies`，仅 subprocess 隔离调用 |
| G7 | 可发布的 CLI 与可发布的独立 exe | `pypi` 与 `GitHub Release` 两条流水线可独立触发；exe 无需重下即可跟进 yt-dlp 更新 |

### 1.2 非目标

- 不新增平台（YouTube/X/Bilibili/Instagram/TikTok 五个足够）。
- 不重写字幕排版算法（`formatter.py` 的断句/对齐逻辑是资产，只做**物理拆分**，不做**算法重写**）。
- 不引入 GPU 推理、本地大模型、Docker-only 部署。
- 不改用户可见的输出目录契约（`<task_dir>/raw/` 与 `<task_dir>/cooked/` 保持不变）。

---

## 2. 现状问题清单（含证据）

> 这一节是重构的**必要性论证**，不是抱怨。每条都有行号，可复核。
> 复核命令：`git show main:<file> | sed -n '<N>p'`

### 2.1 `BasePlatformExtractor` 是假抽象

`porter_skill/extractors/base.py:134` 的 ABC 只有两个抽象方法：

```python
class BasePlatformExtractor(ABC):
    @abstractmethod
    def can_handle(self, url: str) -> bool: ...
    @abstractmethod
    def extract_raw_materials(self, url, output_base_dir, ffmpeg_path,
                              cookies_file, cookies_browser) -> RawMaterialResult: ...
```

**ABC 未提供任何一行共享实现。** 每个子类把「拉元数据 → 选字幕语言 → 下载视频流 → ffmpeg 转码 → 抽 WAV → 抽增强 WAV → 下封面 → 写 metadata.json → 重试降级」整条链路塞进 `extract_raw_materials`：

| 文件 | `extract_raw_materials` 起止 | 单函数规模 |
|---|---|---|
| `extractors/youtube.py` | 152 → ~485 | ≈ 330 行 |
| `extractors/bilibili.py` | 220 → ~544 | ≈ 320 行 |
| `extractors/tiktok.py` | 199 → ~520 | ≈ 320 行 |
| `extractors/instagram.py` | 120 → ~430 | ≈ 310 行 |
| `extractors/x.py` | 67 → ~330 | ≈ 260 行 |

这不是抽象，是**分类**——把 5 份复制粘贴贴上同一张标签。

### 2.2 跨文件重复符号（实测）

| 符号 | 位置 | 份数 |
|---|---|---|
| `_convert_vtt_to_srt` | `youtube.py:25` / `tiktok.py:27` / `bilibili.py:28` | **3** |
| `_select_source_subtitle_lang` | `youtube.py:94` / `tiktok.py:137` / `bilibili.py:156` | **3** |
| `_select_chinese_subtitle_lang` | `youtube.py:139` / `tiktok.py:173` / `bilibili.py:193` | **3** |
| `_clean_tweet_title` / `_clean_caption_to_title` / `_clean_title` | `x.py:45` / `instagram.py:58` / `tiktok.py:105` / `bilibili.py:139` | **4** |

5 个 extractor 合计 **2565 行**，其中估计 60%+ 是同一套流程的变体。修一个 bug 要改 3~5 个地方。

### 2.3 注册表靠 import 副作用

`extractors/__init__.py` 导入各平台模块 → 触发 `@register_extractor` 装饰器 → 往模块级可变 `_EXTRACTORS: list` 追加。

问题：
- `porter-mcp` 是**常驻进程**，`import` 只发生一次，注册表是全局单例，测试之间无法隔离；
- 导入顺序成为隐式契约（谁先 import 谁先命中 `can_handle`）；
- 无法在不 import 的情况下枚举平台（`porter doctor --list-platforms` 做不到）。

### 2.4 59 处 `print()`，0 处 `logging` —— 对 MCP 致命

```bash
git show main | grep -c 'print('   # core 模块内（排除 cli.py）：59 处
git show main | grep -c 'import logging'   # 0 处
```

典型：
```
controller.py:339  print("  -> Attempting VideoCaptioner CLI ASR with engine ...")
controller.py:356  print(f"  ✓ VideoCaptioner CLI ASR succeeded ...")
youtube.py:316     print(f"  ✓ Reusing existing valid temporary download ...")
translator.py:573  print("  -> Falling back to ...")
```

**MCP over stdio 的 stdout 就是 JSON-RPC 通道**，任何一行 print 都会导致协议解析失败。

更隐蔽的一处：
```
youtube.py:301   ydl_opts_download = { ..., "quiet": False, ... }
```
yt-dlp 内部实现：
```python
# yt_dlp/YoutubeDL.py, __init__
self._out_files = Namespace(
    out=stdout,
    error=sys.stderr,
    screen=sys.stderr if self.params.get('quiet') else stdout,
)
```
即 **`quiet=False` 时下载进度写入 stdout**。CLI 形态下无害，MCP 形态下必炸。

### 2.5 核心层泄漏了部署形态

`config.py:76-90`：
```python
candidates = [
    Path.cwd() / "config.json",
    skill_root / "config.json",
    Path.home() / ".pi" / "agent" / "skills" / "porter-skill" / "config.json",   # ← 核心知道 skill 装在哪
    Path.home() / ".config" / "porter-skill" / "config.json",
    Path.home() / ".config" / "videocaptioner" / "config.toml",                   # ← 核心读别家项目的 legacy 配置
]
```
`get_default_user_config_path()` 甚至用 `(skill_root / "SKILL.md").is_file()` 来判断该往哪写配置。

这是把「部署布局」和「第三方项目兼容」两件与领域无关的事，硬编码进了配置解析。

### 2.6 管线接口为 CLI 量身定做

```python
# pipeline/runner.py:36
def run_pipeline(url, output_dir, config, skip_burn, only_bilingual, only_zh,
                 on_progress: Callable[[str, int], None] | None = None) -> PipelineResult:
```

| 缺口 | MCP 的需求 |
|---|---|
| 进度只有 `(step_name, step_num)` | 需要结构化事件（阶段/百分比/消息/产物） |
| 一次性阻塞 | 需要阶段级独立调用（`transcribe` / `translate` / `burn`） |
| 无取消机制 | 客户端断开必须能停掉正在跑的 ffmpeg |
| 返回 dataclass | 需要可 JSON 序列化的稳定 schema |
| 无法并发管控 | 需要串行化 CPU 密集任务 |

且 `runner.py:63` 在管线开头无条件 `run_doctor()` 并 `raise RuntimeError`，把「诊断」和「执行」强耦合——用户想跳过 doctor 直接跑都不行。

### 2.7 依赖声明与实际使用不符

| pyproject 声明 | 实际使用 | 结论 |
|---|---|---|
| `fonttools>=4.40.0` | 全项目零 `import` | **死依赖** |
| `langdetect>=1.0.9` | 全项目零 `import` | **死依赖** |
| `pyyaml>=6.0.0` | 全项目零 `import` | **死依赖** |
| `speechrecognition>=3.10.0` | 仅 `controller.py:233` 懒加载（Google STT 兜底） | 应为 extra |
| `openai>=1.0.0` | `controller.py:13`、`translator.py:12`（硬 import） | 应为 extra（零配置路径不用） |
| `pillow>=10.0.0` | 5 个 extractor 硬 import（`PIL.Image`） | 应为 extra |
| `yt-dlp>=2024.7.4` | 代码已用 `remote_components={"ejs":"github"}` | 版本声明过时，见 §6 |

结果：README 宣称「系统底线仅需 Python + FFmpeg」，但 `pip install porter-skill` 会硬拉 `openai` + `pillow` + `SpeechRecognition`。

### 2.8 仓库卫生

根目录混入 5 个 `pi-session-*.html`（合计约 7 MB）、`.memory/`、`porter_output/`（含 mp4/wav）。`.gitignore` 覆盖了但历史未清理。新分支已清空，`.gitignore` 已重写。

---

## 3. 目标架构

### 3.1 分层与依赖方向

```
                    ┌──────────────────────────────────────┐
                    │            porter  (引擎库)          │
                    │  纯 Python · 零 print · 零 argparse  │
                    │  零全局可变状态 · 零部署形态知识      │
                    └───────────────┬──────────────────────┘
                                    │  只被依赖，不依赖前端
        ┌───────────────────────────┼───────────────────────────┐
        │                           │                           │
┌───────▼────────┐        ┌─────────▼─────────┐       ┌─────────▼─────────┐
│  porter_cli    │        │    porter_mcp     │       │  skills/          │
│  console script│        │   console script  │       │  porter-skill/    │
│  `porter`      │        │   `porter-mcp`    │       │  （纯资产）        │
│  人类可读渲染   │        │  JSON schema      │       │  SKILL.md+脚本     │
└────────────────┘        └───────────────────┘       └───────────────────┘
```

**铁律**：`porter` 包**不得** import `porter_cli` / `porter_mcp`；反向允许。这条要用 import-linter 或自定义测试守住（见 §10.4）。

### 3.2 目标目录树

```
porter-workflow/
├── LICENSE                                  # 保留（MIT，未改动）
├── README.md  README_zh.md                  # 重写：更名 + 新架构
├── pyproject.toml                           # 【单发行包】见 §7.1
├── uv.lock
├── .gitignore
│
├── src/
│   ├── porter/                              # ── 引擎（唯一有业务逻辑的地方）
│   │   ├── __init__.py                      #    __version__ + 公共 API 再导出
│   │   ├── py.typed
│   │   ├── errors.py                        #    异常层次
│   │   ├── logging.py                       #    get_logger()，handler 走 stderr
│   │   ├── config.py                        #    配置解析（无部署形态知识）
│   │   ├── events.py                        #    PipelineEvent 联合类型 + EventSink
│   │   ├── context.py                       #    RunContext: events/cancel/checkpoint/logger
│   │   ├── ports.py                         #    Protocol 定义（Downloader/Transcriber/...）
│   │   ├── pipeline.py                      #    Pipeline 编排器（只依赖 ports）
│   │   ├── jobs.py                          #    JobStore / JobState（供 MCP 与 CLI 共用）
│   │   │
│   │   ├── models/
│   │   │   ├── __init__.py
│   │   │   ├── metadata.py                  #    VideoMetadata
│   │   │   ├── materials.py                 #    RawMaterials / TaskLayout
│   │   │   ├── subtitle.py                  #    SubtitleItem / TranscriptSentence / SubtitleSet
│   │   │   └── request.py                   #    JobRequest / JobResult / JobOptions
│   │   │
│   │   ├── platforms/
│   │   │   ├── __init__.py
│   │   │   ├── spec.py                      #    PlatformSpec / PlatformQuirks / SubtitleParser
│   │   │   ├── base.py                      #    YtDlpExtractor：唯一一份共享流程
│   │   │   ├── registry.py                  #    显式注册（无 import 副作用）
│   │   │   ├── inspector.py                 #    轻量预检探针
│   │   │   ├── youtube.py                   #    ≈50 行：只声明差异
│   │   │   ├── bilibili.py
│   │   │   ├── x.py
│   │   │   ├── instagram.py
│   │   │   └── tiktok.py
│   │   │
│   │   ├── subtitles/
│   │   │   ├── __init__.py
│   │   │   ├── srt.py                       #    SRT/VTT/Bilibili-JSON 解析与序列化
│   │   │   ├── phrasing.py                  #    断句 / 意群切分 / 中英对齐
│   │   │   ├── ass.py                       #    ASS 样式与渲染（含样式自适应）
│   │   │   └── transcript.py                #    台词本重建与持久化
│   │   │
│   │   ├── asr/
│   │   │   ├── __init__.py
│   │   │   ├── base.py                      #    Transcriber Protocol + AsrResult
│   │   │   ├── chain.py                     #    回退链编排
│   │   │   ├── whisper_api.py
│   │   │   ├── bcut.py                      #    必剪（免 Key）
│   │   │   ├── google_web.py                #    Google Web STT + VAD 切片
│   │   │   └── videocaptioner.py            #    ★ 外部 CLI 适配器（GPL 隔离，可选）
│   │   │
│   │   ├── translate/
│   │   │   ├── __init__.py
│   │   │   ├── base.py                      #    Translator Protocol
│   │   │   ├── chain.py
│   │   │   ├── llm.py                       #    OpenAI 兼容（DeepSeek/GPT）
│   │   │   ├── bing.py
│   │   │   ├── google.py
│   │   │   ├── mymemory.py
│   │   │   └── videocaptioner.py            #    ★ 外部 CLI 适配器（GPL 隔离，可选）
│   │   │
│   │   ├── media/
│   │   │   ├── __init__.py
│   │   │   ├── ffmpeg.py                    #    ★ 唯一 subprocess 封装点（run_ffmpeg）
│   │   │   ├── probe.py                     #    ffprobe 封装 + is_valid_video_file
│   │   │   ├── enhance.py                   #    ASR 三合一人声增强链
│   │   │   ├── encode.py                    #    HardwareProfile + 自适应编码参数
│   │   │   └── burn.py                      #    libass 硬字幕压制
│   │   │
│   │   └── doctor/
│   │       ├── __init__.py
│   │       ├── probes.py                    #    CapabilityReport（结构化，无文案）
│   │       └── guides.py                    #    跨平台安装指引（纯文本，presentation）
│   │
│   ├── porter_cli/                          # ── CLI 前端
│   │   ├── __init__.py
│   │   ├── __main__.py                      #    def main() -> int
│   │   ├── app.py                           #    命令树
│   │   ├── render.py                        #    Event → 终端（rich 或纯文本）
│   │   └── commands/
│   │       ├── __init__.py
│   │       ├── run.py  inspect.py  doctor.py  config.py  jobs.py
│   │
│   └── porter_mcp/                          # ── MCP 前端
│       ├── __init__.py
│       ├── server.py                        #    FastMCP("porter") + def main()
│       ├── stdout_guard.py                  #    stdout 哨兵（见 §8.4）
│       ├── progress.py                      #    Event → MCP progress notification
│       └── tools/
│           ├── __init__.py
│           ├── inspect.py  plan.py  jobs.py  stages.py  doctor.py
│
├── skills/
│   └── porter-skill/                        # ── Agent Skill 资产（无 Python 包）
│       ├── SKILL.md                         #    frontmatter: name=porter-skill
│       ├── README.md
│       ├── references/
│       │   ├── ARCHITECTURE.md
│       │   └── CONFIG.md
│       ├── scripts/
│       │   ├── porter.sh                    #    入口：uvx 转调
│       │   ├── inspect.sh
│       │   └── bootstrap.sh                 #    venv 自举（exe/离线场景）
│       └── assets/
│           └── config.example.json
│
├── tests/
│   ├── unit/                                #    纯函数，无网络无 ffmpeg
│   ├── integration/                         #    需要 ffmpeg / mock 网络
│   ├── regression/                          #    ← main 上 90 个测试的等价版本
│   └── conftest.py
│
├── docs/
│   ├── REFACTOR_PLAN.md                     #    本文件
│   ├── ARCHITECTURE.md
│   ├── CONFIG.md
│   ├── MCP.md
│   └── MIGRATION.md                         #    v0.1 → v0.2 用户迁移指南
│
└── .github/workflows/
    ├── test.yml
    ├── release-pypi.yml
    └── release-exe.yml
```

---

## 4. 逐文件迁移映射表

> 旧路径均相对 `main` 分支根目录。**`git show main:<旧路径>` 可取回原文。**

### 4.1 根级文件

| 旧路径 | 新路径 | 动作 |
|---|---|---|
| `LICENSE` | `LICENSE` | 保留不动（MIT） |
| `README.md` / `README_zh.md` | 同名 | 重写：更名、新架构、新安装方式、依赖矩阵 |
| `SKILL.md` | `skills/porter-skill/SKILL.md` | 迁移 + 精简 + 新增 `compatibility`/`license` frontmatter |
| `config.example.json` | `skills/porter-skill/assets/config.example.json` | 迁移 + 同步新配置键 |
| `pyproject.toml` | `pyproject.toml` | 完全重写（§7.1） |
| `.gitignore` | `.gitignore` | 已重写 |

### 4.2 `porter_skill/` 顶层

| 旧路径 | 新路径 | 动作 |
|---|---|---|
| `__init__.py` (3 行) | `src/porter/__init__.py` | 迁移，补全公共 API 再导出 |
| `__main__.py` (8 行) | **删除** | CLI 入口由 `porter_cli/__main__.py` 承担 |
| `cli.py` (232 行) | `src/porter_cli/{app,render}.py` + `commands/*.py` | **拆分**：`print_config_summary`→`commands/config.py`；`_mask_secret`→`porter_cli/render.py`；参数定义→`app.py` |
| `config.py` (250 行) | `src/porter/config.py` | **改写**：删除 `~/.pi/...`、`SKILL.md` 探测、videocaptioner legacy toml（见 §5.5） |
| `env_check.py` (487 行) | 拆分见下 | —— |

`env_check.py` 拆分明细（按 `git show main:porter_skill/env_check.py` 的符号）：

| 旧符号（行号） | 新位置 | 备注 |
|---|---|---|
| `CheckResult` (17) | `doctor/probes.py` | 改造为 `Capability`（去掉 `guide` 文案字段） |
| `HardwareProfile` (28) | `media/encode.py` | 与编码强相关，不属于 doctor |
| `detect_hardware_profile` (43) | `media/encode.py` | 迁移 |
| `DoctorReport` (183) | `doctor/probes.py` | 改为 `CapabilityReport` |
| `get_windows_ffmpeg_guide` (196) | `doctor/guides.py` | 纯文案 |
| `get_windows_python_guide` | `doctor/guides.py` | 纯文案 |
| `get_linux_ffmpeg_guide` / `get_linux_python_guide` | `doctor/guides.py` | 纯文案 |
| `check_python` (~250) | `doctor/probes.py` | 返回结构化 Capability |
| `check_ffmpeg` (253) | `doctor/probes.py` + `media/probe.py` | **增强**：必须验证 libass |
| `check_packages` (322) | `doctor/probes.py` | **改写**：新增 Deno / yt-dlp-ejs 探测 |
| `check_llm` (390) | `doctor/probes.py` | 迁移 |
| `check_hardware` (423) | `doctor/probes.py` | 转调 `media/encode.py` |
| `run_doctor` (438) | `doctor/probes.py` | 迁移 |
| `print_doctor_report` (450) | `src/porter_cli/render.py` | **离开引擎**（presentation） |

### 4.3 `porter_skill/extractors/`

| 旧路径 | 新路径 | 动作 |
|---|---|---|
| `base.py` (160 行) | 拆分见下 | —— |
| `__init__.py` (41 行) | `src/porter/platforms/registry.py` | **改写**为显式注册表 |
| `inspector.py` (386 行) | `src/porter/platforms/inspector.py` | 迁移，`InspectionResult` 移入 `models/` |
| `youtube.py` (555 行) | `src/porter/platforms/youtube.py` | **削至 ≈50 行声明**；`_convert_vtt_to_srt` 上提 |
| `bilibili.py` (595 行) | `src/porter/platforms/bilibili.py` | 同上；保留 CC JSON 解析 + WBI/412 |
| `x.py` (381 行) | `src/porter/platforms/x.py` | 同上；保留 tweet 标题清洗 |
| `instagram.py` (462 行) | `src/porter/platforms/instagram.py` | 同上；保留 carousel 过滤 + embed 降级 |
| `tiktok.py` (572 行) | `src/porter/platforms/tiktok.py` | 同上 |

`base.py` 拆分明细：

| 旧符号（行号） | 新位置 |
|---|---|
| `VideoMetadata` (11) | `models/metadata.py` |
| `RawMaterialResult` (38) | `models/materials.py` → 更名 `RawMaterials` |
| `sanitize_filename` (54) | `porter/utils/text.py`（新增，通用工具） |
| `enhance_audio_for_asr` (87) | `media/enhance.py` → 更名 `enhance_audio_for_asr(...)` |
| `BasePlatformExtractor` (134) | 拆为 `platforms/spec.py`（声明）+ `platforms/base.py`（模板方法） |
| `_EXTRACTORS` / `register_extractor` / `get_extractor` (150-160) | `platforms/registry.py` |

### 4.4 `porter_skill/pipeline/`

| 旧路径 | 新路径 | 动作 |
|---|---|---|
| `runner.py` (144 行) | `src/porter/pipeline.py` | **重写为可取消的事件驱动管线**（见 §5.4） |
| `PipelineResult` (18) | `models/request.py` → `JobResult` | 改造为可序列化 |
| `__init__.py` (5 行) | **删除** | 由 `porter/__init__.py` 统一再导出 |

### 4.5 `porter_skill/subtitle/` —— 最大的一块

| 旧路径 | 新路径 | 动作 |
|---|---|---|
| `controller.py` (723 行) | 拆分见下 | —— |
| `formatter.py` (1184 行) | 按符号拆分见下 | **纯物理拆分，不改算法** |
| `translator.py` (579 行) | 拆分见下 | —— |
| `__init__.py` (75 行) | **删除** | 由 `porter/__init__.py` 统一再导出 |

`controller.py` 拆分（符号行号见 `git show main`）：

| 旧符号 | 新位置 |
|---|---|
| `SubtitleResult` (48) | `models/subtitle.py` → `SubtitleSet` |
| `transcribe_with_whisper_api` (62) | `asr/whisper_api.py` |
| `transcribe_with_bcut` (96) | `asr/bcut.py` |
| `transcribe_with_google_stt` (223) | `asr/google_web.py` |
| `run_asr_transcription` (329) | `asr/chain.py`（含 videocaptioner 回退分支） |
| `compute_adaptive_subtitle_style` (410) | `subtitles/ass.py` |
| `has_chinese_translation` (490) | `subtitles/phrasing.py` |
| `generate_subtitles` (499) | `pipeline.py`（作为 `SubtitleStage`） |

`formatter.py` 拆分（1184 行，按职责三层）：

| 新模块 | 承接符号 |
|---|---|
| `subtitles/srt.py` | `SubtitleItem`(387)、`srt_time_to_ms`(436)、`ms_to_srt_time`(451)、`parse_srt`(481)、`generate_bilingual_srt`(1033)、`generate_zh_srt`(1050) |
| `subtitles/phrasing.py` | `_TokenPart`(255)、`restore_english_punctuation_heuristic`(262)、`TranscriptSentence`(414)、`reconstruct_sentences_from_fragments`(574)、`clean_chinese_subtitle_punctuation`(666)、`split_chinese_text_by_phrase`(680)、`split_english_text_to_n_parts`(743)、`split_chinese_sentence_into_cues`(833)、`merge_short_fragments`(922)、`normalize_subtitle_items`(980)、`align_bilingual_items`(1003)、`is_cjk`(476) |
| `subtitles/ass.py` | `ms_to_ass_time`(463)、`generate_bilingual_ass`(1060)、`generate_zh_ass`(1144) |
| `subtitles/transcript.py` | `save_transcript_json`(900)、`save_transcript_txt`(908) |
| `utils/text.py` | `restore_english_punctuation_heuristic` 若被 translate 复用则上提 |

`translator.py` 拆分：

| 新模块 | 承接符号 |
|---|---|
| `translate/base.py` | 新增 `Translator` Protocol |
| `translate/chain.py` | `translate_with_*` 系列的门面与回退编排 |
| `translate/llm.py` | `translate_sentences_with_direct_llm`(430)、`translate_with_direct_llm`(509) |
| `translate/bing.py` | `translate_sentences_with_bing_http`(39)、`translate_with_bing_http`(150) |
| `translate/google.py` | `translate_sentences_with_google_http`(180)、`translate_with_google_http`(285) |
| `translate/mymemory.py` | `translate_sentences_with_mymemory_http`(315)、`translate_with_mymemory_http`(362) |
| `translate/videocaptioner.py` | `_get_videocaptioner_bin`(28)、`translate_with_videocaptioner_free`(392)、`translate_with_videocaptioner_cli`(539) |

### 4.6 `porter_skill/synthesizer/`

| 旧路径 | 新路径 | 动作 |
|---|---|---|
| `burn.py` (296 行) | `src/porter/media/burn.py` | 迁移；`subprocess.run` 全部改走 `media/ffmpeg.py::run_ffmpeg` |
| `utils.py` (26 行) | `src/porter/media/ffmpeg.py` | `escape_ffmpeg_filter_path` 并入 |
| `__init__.py` (19 行) | **删除** | 统一再导出 |
| `DualReleaseResult` (13 行) | `models/request.py` → `BurnResult` | 改造为可序列化 |

### 4.7 `references/` 与 `scripts/`

| 旧路径 | 新路径 | 动作 |
|---|---|---|
| `references/ARCHITECTURE.md` | `docs/ARCHITECTURE.md` + `skills/porter-skill/references/ARCHITECTURE.md` | 更新为 v0.2 架构 |
| `references/CONFIG_GUIDE.md` | `docs/CONFIG.md` + `skills/porter-skill/references/CONFIG.md` | 更新配置键 |
| `scripts/run_porter.py` | `skills/porter-skill/scripts/porter.sh` | **重写为 shell**：`exec uvx --from "porter-workflow[all]" porter "$@"`；保留 venv 自举为 `bootstrap.sh` |
| `scripts/inspect_link.py` | `skills/porter-skill/scripts/inspect.sh` | 同上，`porter inspect "$@"` |
| `scripts/setup_env.sh` | `skills/porter-skill/scripts/bootstrap.sh` | 迁移，供离线/exe 场景 |

### 4.8 `tests/`（90 个测试，是重构的安全网）

| 旧路径 | 新路径 | 动作 |
|---|---|---|
| `test_subtitle.py` (775) | `tests/regression/test_subtitle.py` | 迁移；import 路径改 `porter.subtitles.*` |
| `test_bilibili_extractor.py` (347) | `tests/regression/test_bilibili_extractor.py` | 迁移；适配新的 `PlatformSpec` 断言方式 |
| `test_tiktok_extractor.py` (299) | `tests/regression/test_tiktok_extractor.py` | 同上 |
| `test_instagram_extractor.py` (243) | `tests/regression/test_instagram_extractor.py` | 同上 |
| `test_extractors.py` (185) | `tests/regression/test_extractors.py` | 同上 |
| `test_pipeline.py` (170) | `tests/regression/test_pipeline.py` | 迁移到新的 `Pipeline` API |
| `test_synthesizer.py` (173) | `tests/regression/test_synthesizer.py` | 迁移 |
| `test_config.py` (80) | `tests/unit/test_config.py` | **改写**：断言新的配置搜索顺序 |
| `test_env_check.py` (107) | `tests/unit/test_doctor.py` | **改写**：断言 `CapabilityReport` 结构 |
| `test_inspector.py` (92) | `tests/unit/test_inspector.py` | 迁移 |
| `test_x_extractor.py` (126) | `tests/regression/test_x_extractor.py` | 迁移 |

> **迁移原则**：`tests/regression/` 里的断言**只允许改 import 路径和模块位置，不允许改断言内容**。如果某条断言必须改，说明行为变了，需要在 PR 描述里单独说明理由。

---

## 5. 核心接口契约

> 这些签名是 CLI / MCP / skill 三方的共同契约。**先冻结本节，再写实现。**

### 5.1 领域模型（`models/`）

全部使用 `pydantic.BaseModel`（已有依赖）或 `dataclass` + `asdict`，必须可 `model_dump_json()`。

```python
# models/request.py
class JobOptions(BaseModel):
    output_dir: Path
    burn: BurnMode = BurnMode.DUAL        # BOTH | ZH_ONLY | BILINGUAL_ONLY | SKIP
    asr_engine: str | None = None          # None = 走回退链
    translator: str | None = None          # llm | bing | google | mymemory | None=自动
    llm_model: str | None = None
    target_lang: str = "zh-Hans"
    cookies_file: Path | None = None
    cookies_browser: str | None = None
    audio_denoise: bool = True
    force_stage: Stage | None = None       # 只跑单个阶段

class JobRequest(BaseModel):
    url: str | None = None
    local_video: Path | None = None        # 支持本地文件（MCP 场景需要）
    options: JobOptions

class JobResult(BaseModel):
    job_id: str
    state: JobState                        # pending|running|done|failed|cancelled
    task_dir: Path | None
    raw: RawMaterials | None
    subtitles: SubtitleSet | None
    burn: BurnResult | None
    error: ErrorInfo | None
    duration_seconds: float
```

```python
# models/materials.py
class TaskLayout(BaseModel):
    """单一职责：路径推导。取代散落在 5 个 extractor 里的 mkdir 逻辑。"""
    task_dir: Path
    @property
    def raw_dir(self) -> Path: ...
    @property
    def cooked_dir(self) -> Path: ...
    @property
    def tmp_dir(self) -> Path: ...
    @classmethod
    def build(cls, root: Path, video_id: str, title: str) -> "TaskLayout": ...

class RawMaterials(BaseModel):
    """与现 RawMaterialResult 字段对齐，保证输出契约不变。"""
    layout: TaskLayout
    video: Path
    audio: Path
    audio_enhanced: Path | None = None
    cover: Path | None = None
    subtitle_src: Path | None = None
    metadata: Path | None = None
    info: VideoMetadata | None = None
```

### 5.2 `PlatformSpec`：把 5 份巨石变成声明

```python
# platforms/spec.py
SubtitleParser = Callable[[str], str]          # 原始字幕文本 → SRT 文本

@dataclass(frozen=True)
class PlatformQuirks:
    player_clients: tuple[str, ...] = ()       # YouTube / Bilibili
    remote_components: dict | None = None      # {"ejs": "github"}
    requires_cookies_hint: str | None = None   # 泄露时的用户指引
    extra_ydl_opts: dict = field(default_factory=dict)

@dataclass(frozen=True)
class PlatformSpec:
    name: str
    url_patterns: tuple[re.Pattern[str], ...]
    expand_url: Callable[[str], str]           # 短链展开 + 去 tracking 参数
    format_spec: str = "bestvideo[height<=1080][ext=mp4]+bestaudio[ext=m4a]/bestvideo[height<=1080]+bestaudio/best"
    subtitle_priority: tuple[str, ...] = ("en", "zh-Hans", "zh")
    subtitle_parser: SubtitleParser = parse_vtt
    clean_title: Callable[[Mapping[str, Any]], str] = default_title
    quirks: PlatformQuirks = PlatformQuirks()

    def matches(self, url: str) -> bool:
        return any(p.search(url) for p in self.url_patterns)
```

### 5.3 `platforms/base.py`：唯一一份共享流程

```python
class YtDlpExtractor:
    """模板方法。子类只提供 spec，不覆写 extract()。"""

    spec: ClassVar[PlatformSpec]

    def __init__(self, ctx: RunContext) -> None:
        self.ctx = ctx
        self.log = ctx.logger.getChild(f"platform.{self.spec.name}")

    def extract(self, url: str) -> RawMaterials:
        url  = self.spec.expand_url(url)
        info = self._probe(url)                                  # 1. 元数据
        layout = TaskLayout.build(self.ctx.output_root, info["id"], self.spec.clean_title(info))
        layout.ensure_dirs()

        # 字幕与媒体流解耦：单轨 429 不丢弃已成功的源字幕
        subtitle = self._fetch_subtitle(info, layout)            # 2. 字幕（可失败）
        video    = self._fetch_media(info, layout)               # 3. 媒体
        audio    = self._make_audio(video, layout)               # 4. WAV
        enhanced = self._enhance_audio(audio, layout) if self.ctx.opts.audio_denoise else None
        cover    = self._fetch_cover(info, layout)               # 5. 封面
        self._write_metadata(info, layout)                       # 6. metadata.json

        return RawMaterials(layout=layout, video=video, audio=audio,
                            audio_enhanced=enhanced, cover=cover,
                            subtitle_src=subtitle, metadata=layout.raw_dir / "metadata.json",
                            info=self._to_metadata(info))

    # ---- 以下钩子默认实现对所有平台通用，子类仅在必要时覆写 ----
    def _ydl_opts(self, *, purpose: Literal["info","subtitle","download"]) -> dict: ...
    def _fetch_media(self, info, layout) -> Path: ...
    def _select_langs(self, info: Mapping) -> list[str]:         # ← 取代 3 份重复实现
        return list(self.spec.subtitle_priority)
```

**Yt-dlp 调用的唯一出口**（解决 §2.4 的 stdout 污染）：

```python
# platforms/base.py
def build_ydl(opts: dict, logger: logging.Logger) -> yt_dlp.YoutubeDL:
    """所有 YoutubeDL 实例必须经此构造。"""
    return yt_dlp.YoutubeDL({
        **opts,
        "quiet": True,          # ← screen 走 stderr
        "noprogress": True,
        "no_warnings": True,
        "logger": _YtDlpLoggerAdapter(logger),   # 把 yt-dlp 输出转进 logging
    })
```

**注册表改写**（消除 import 副作用）：

```python
# platforms/registry.py
_SPECS: dict[str, type[YtDlpExtractor]] = {}

def register(cls: type[YtDlpExtractor]) -> type[YtDlpExtractor]:
    _SPECS[cls.spec.name] = cls
    return cls

def resolve(url: str) -> type[YtDlpExtractor]:
    for cls in _SPECS.values():
        if cls.spec.matches(url):
            return cls
    raise UnsupportedPlatformError(url, supported=tuple(_SPECS))

def load_builtin() -> None:
    """显式调用（在 porter/__init__.py 或 CLI 启动时），不依赖 import 顺序。"""
    from porter.platforms import bilibili, instagram, tiktok, x, youtube  # noqa: F401
```

### 5.4 `Pipeline` / `events` / `RunContext`

```python
# events.py
class Event(BaseModel):
    """全部事件可 JSON 序列化，CLI 与 MCP 共用。"""
    ts: float

class PhaseStarted(Event):    phase: Phase; total_steps: int
class StepCompleted(Event):   phase: Phase; step: int; name: str
class ProgressUpdated(Event): phase: Phase; percent: float; message: str
class ArtifactReady(Event):   kind: ArtifactKind; path: Path
class LogRecord(Event):       level: str; message: str
class PhaseFailed(Event):     phase: Phase; error: ErrorInfo

EventSink = Callable[[Event], None]
```

```python
# context.py
@dataclass
class RunContext:
    opts: JobOptions
    output_root: Path
    logger: logging.Logger
    events: EventSink
    cancel: threading.Event
    checkpoint_dir: Path | None = None

    def emit(self, event: Event) -> None:
        if not self.cancel.is_set():
            self.events(event)

    def check_cancelled(self) -> None:
        if self.cancel.is_set():
            raise JobCancelled(self.job_id)
```

```python
# pipeline.py
class Pipeline:
    """只依赖 ports。构造时可注入替身（测试友好）。"""

    def __init__(self, *, downloader: Downloader, asr: Transcriber,
                 translator: Translator, renderer: Renderer) -> None: ...

    # 阶段级公开 API —— MCP 需要单独调用
    def prepare(self, url: str, ctx: RunContext) -> RawMaterials: ...
    def transcribe(self, raw: RawMaterials, ctx: RunContext) -> SubtitleSet: ...
    def translate(self, subs: SubtitleSet, ctx: RunContext) -> SubtitleSet: ...
    def burn(self, raw: RawMaterials, subs: SubtitleSet, ctx: RunContext) -> BurnResult: ...

    # 全流程编排
    def run(self, req: JobRequest, ctx: RunContext) -> JobResult:
        for phase in self._phases_for(req.options):
            ctx.check_cancelled()
            ctx.emit(PhaseStarted(phase=phase, total_steps=len(...)))
            ...

    @classmethod
    def default(cls, ctx: RunContext) -> "Pipeline":
        """生产装配：从能力探测结果挑选具体实现。"""
```

**关键行为变更（相对 `runner.py`）**：

| 旧行为 | 新行为 | 理由 |
|---|---|---|
| 开头无条件 `run_doctor()` 并在失败时 raise | **按需探测**：只检查当前路径真正需要的能力 | 想跑 `--skip-burn` 的用户不该被 libass 缺失拦住 |
| 无取消 | `ctx.cancel` + 每个 ffmpeg 子进程注册到 context | MCP 客户端断开必须能停 |
| 无 checkpoint 抽象 | `checkpoint_dir` + 各阶段写 `stage.json` | 复用现有「断点续传」能力但结构化 |
| 进度 `(str, int)` | 结构化 `Event` | CLI 与 MCP 都能消费 |

### 5.5 配置解析（去部署形态知识）

```python
# config.py
def resolve(explicit: Path | None = None) -> PorterConfig:
    """
    优先级（高 → 低）：
      1. 显式 --config 路径
      2. 环境变量 PORTER_CONFIG 指向的路径      ← skill/agent 通过它注入自己的配置
      3. 环境变量覆盖（OPENAI_API_KEY / PORTER_ASR_ENGINE / ...）
      4. 项目级 ./porter.json（向上查找到第一个）
      5. 用户级 platformdirs.user_config_dir("porter") / config.json
      6. 内置默认值
    """
```

**明确删除**（这些不属于核心职责）：

| 删除项 | 替代方案 |
|---|---|
| `~/.pi/agent/skills/porter-skill/config.json` 探测 | skill 脚本设置 `PORTER_CONFIG` 环境变量 |
| 依据 `SKILL.md` 是否存在决定写入路径 | 同上；核心只认 `platformdirs` |
| `~/.config/videocaptioner/config.toml` 自动读取 | 提供显式迁移命令 `porter config import-videocaptioner` |

新增依赖 `platformdirs>=4.0`（跨平台用户目录）。写配置时**沿用现有语义**：`porter config set llm.api_key=...` 仍需落在用户级配置。

### 5.6 Doctor：能力探测 vs 文案渲染

**现在的问题**：`CheckResult` 同时携带 `status`、`message`、`details` 和 `guide` 文案；`print_doctor_report()` 在引擎里。MCP 拿不到结构化数据，只能拿到一堆中文文案。

**目标**：

```python
# doctor/probes.py
class Capability(str, Enum):
    PYTHON = "python"
    FFMPEG = "ffmpeg"
    FFMPEG_LIBASS = "ffmpeg_libass"        # ← 新增，之前只验 ffmpeg 存在
    FFPROBE = "ffprobe"
    YTDLP = "ytdlp"
    YTDLP_EJS = "ytdlp_ejs"                # ← 新增
    JS_RUNTIME = "js_runtime"              # ← 新增（deno/node）
    LLM_API = "llm_api"
    ASR_WHISPER = "asr_whisper"
    ASR_BCUT = "asr_bcut"
    ASR_GOOGLE = "asr_google"
    VIDEOCAPTIONER = "videocaptioner"      # 永远 optional
    HARDWARE_ACCEL = "hardware_accel"

class CapabilityFinding(BaseModel):
    capability: Capability
    available: bool
    severity: Severity                     # BLOCKER | DEGRADED | INFO
    version: str | None = None
    detected_at: Path | None = None
    remediation_key: str | None = None     # 指向 guides.py 的 key，而非文案

class CapabilityReport(BaseModel):
    findings: list[CapabilityFinding]

    @property
    def blockers(self) -> list[CapabilityFinding]: ...
    def requires(self, *caps: Capability) -> None:
        """管线按需断言。缺什么才报什么。"""
```

`guides.py` 保留现有四段 Windows/Linux 指引（它们写得很完整），只是改为按 `remediation_key` 查找。

**新增探测命令**：
```bash
ffmpeg -hide_banner -filters | grep -q ' subtitles '      # libass via subtitles filter
python -c "import yt_dlp_ejs"                             # ejs
deno --version || node --version                          # js runtime
```

---

## 6. 依赖策略

### 6.1 判定矩阵

| 判据 | → PyPI 库（import） | → subprocess CLI |
|---|---|---|
| **许可证** | MIT / BSD / Apache / PSF / Unlicense | **GPL / AGPL**（进程边界 = 许可证边界） |
| **失败域** | `try/except` 可收敛 | 崩溃不退化为我方进程中毒 |
| **版本耦合** | API 稳定 | 迭代极快 / 用户想用系统版本 |
| **可选性** | 核心必需 | 少数场景 / 体积巨大 |
| **Python 约束** | 不限制我方 | 有限制（如 `python<3.13`）→ 必须隔离 |

### 6.2 逐项结论

#### ① `yt-dlp` → **PyPI 库**，但版本声明必须升级

- **许可证 Unlicense**（公有领域），无 copyleft 风险。
- CLI 就是 `YoutubeDL` 的薄封装，官方提供 `devscripts/cli_to_api.py` 做参数转换 → 用库是正统做法。
- `cookiefile` / `cookiesfrombrowser` / `format` / `subtitles` / `progress_hooks` 全部可从 API 获取，**不需要调 CLI**。

**必须修正的声明**：

```diff
- "yt-dlp>=2024.7.4"
+ "yt-dlp[default]>=2025.11.12"
```

理由：代码已在用 `remote_components={"ejs": "github"}`。自 yt-dlp **2025.11.12** 起，YouTube 强制要求外部 JS 运行时：

| 组件 | 获取方式 | 是否 PyPI 可解 |
|---|---|---|
| `yt-dlp-ejs`（JS 挑战求解脚本） | `pip install "yt-dlp[default]"` | ✅ 有，且**版本被 yt-dlp 严格 pin** |
| JS 运行时 | **Deno**（推荐，默认启用）/ Node ≥20（需 `--js-runtimes node`） | ❌ **系统二进制** |

→ 这是**系统级外部依赖，与 ffmpeg 同级**，必须进 `doctor` 与 SKILL.md 的 `compatibility` 字段。
→ 保留 `YtDlpBackend` 适配层，留出切 CLI 的口子。

#### ② `ffmpeg` / `ffprobe` → **系统二进制**（无选择）

- `imageio-ffmpeg` 等 PyPI 包**通常不带 `libass` / `subtitles` 滤镜**，而本项目硬字幕完全依赖 libass → 不可用。
- `doctor` 必须真正验证 libass 滤镜，而不只是二进制存在（现在只验存在）。
- exe 场景可**首启动侧载**静态构建（见 §7.3）。

#### ③ `videocaptioner` → **只能 subprocess，永不声明为依赖** ★

三条独立的死线：

| # | 问题 | 后果 |
|---|---|---|
| 1 | **GPL-3.0**，本项目 MIT | `import videocaptioner` 构成 derivative work → 你被迫整体改为 GPL-3.0 发布 |
| 2 | **`requires-python >=3.10,<3.13`** | 写进 dependencies 即把项目永久锁死在 Python 3.12 |
| 3 | 默认安装**含 GUI（Qt 等）**，体积巨大 | 用户为了 3 个引擎装一整套桌面应用 |

**正确做法**（现有代码已在做，只需正式化）：

```python
# asr/videocaptioner.py
def is_available() -> bool:
    return _resolve_bin() is not None      # shutil.which("videocaptioner")

class VideoCaptionerASR(Transcriber):
    """可选后端。不可用时由 chain 静默剔除，不报错、不警告为用户可见噪音。"""
    name = "videocaptioner"
    def available(self) -> bool: return is_available()
```

`subprocess.run([bin, ...])` 是业界标准的 arms-length 隔离，MIT 得以保留。

#### ④ 其他依赖 → 分层 extras

| 依赖 | 归属 | 说明 |
|---|---|---|
| `requests` | **core** | HTTP（Bing/Google/MyMemory/Bcut/STT） |
| `pydantic` | **core** | 模型与配置 |
| `platformdirs` | **core** | 替代硬编码 `~/.config` |
| `openai` | `[llm]` | LLM 翻译 + Whisper API |
| `json-repair` | `[llm]` | LLM 返回 JSON 容错 |
| `SpeechRecognition` | `[stt]` | Google Web STT 兜底 |
| `pillow` | `[images]` | 封面处理（可选，缺则跳过封面） |
| `fastmcp` | `[mcp]` | MCP server |
| **删除** | — | `fonttools`、`langdetect`、`pyyaml`（零 import） |

> 这一层设计让「系统底线仅需 Python + FFmpeg」**第一次名副其实**：`pip install porter-workflow` 装的是最小闭环，不再硬拉 `openai` / `pillow` / `SpeechRecognition`。

### 6.3 一句话原则

> **Python 生态内、宽松许可、核心必需 → 声明为 PyPI 依赖；跨语言、copyleft、可选、有 Python 版本约束 → 运行时探测 + subprocess。**

---

## 7. 打包与发布

### 7.1 `pyproject.toml`（完整）

```toml
[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[project]
name = "porter-workflow"
version = "0.2.0"
description = "Automated video localization, multi-engine ASR, subtitle translation, and dual-version FFmpeg hardsub pipeline for AI agents, CLI, and MCP hosts."
readme = "README.md"
license = { text = "MIT" }
requires-python = ">=3.10"
authors = [{ name = "RoL1n_SrP" }]
keywords = ["youtube", "video-localization", "subtitles", "asr", "translation",
            "ffmpeg", "hardsub", "agent-skill", "mcp"]

classifiers = [
    "Development Status :: 4 - Beta",
    "Environment :: Console",
    "Intended Audience :: Developers",
    "License :: OSI Approved :: MIT License",
    "Programming Language :: Python :: 3.10",
    "Programming Language :: Python :: 3.11",
    "Programming Language :: Python :: 3.12",
    "Programming Language :: Python :: 3.13",
    "Topic :: Multimedia :: Video",
]

dependencies = [
    "yt-dlp[default]>=2025.11.12",
    "requests>=2.31",
    "pydantic>=2.0",
    "platformdirs>=4.0",
]

[project.optional-dependencies]
llm    = ["openai>=1.0", "json-repair>=0.30"]
stt    = ["SpeechRecognition>=3.10"]
images = ["pillow>=10.0"]
mcp    = ["fastmcp>=2.11"]
all    = ["porter-workflow[llm,stt,images,mcp]"]
dev    = ["pytest>=8.0", "pytest-asyncio>=0.23", "ruff>=0.6", "mypy>=1.10",
          "pytest-cov>=5.0", "import-linter>=2.0"]

[project.scripts]
porter     = "porter_cli.__main__:main"
porter-mcp = "porter_mcp.server:main"

[project.urls]
Homepage      = "https://github.com/RolinShmily/porter-workflow"
Documentation = "https://github.com/RolinShmily/porter-workflow/tree/main/docs"
Changelog     = "https://github.com/RolinShmily/porter-workflow/releases"

[tool.hatch.build.targets.wheel]
packages = ["src/porter", "src/porter_cli", "src/porter_mcp"]

[tool.hatch.build.targets.sdist]
include = ["/src", "/tests", "/docs", "/skills", "/README.md", "/LICENSE"]

# ---------------------------------------------------------------------------
# Lint —— T20 是 MCP 能跑的前提：全库禁止 print()
# ---------------------------------------------------------------------------
[tool.ruff]
line-length = 100
target-version = "py310"
src = ["src", "tests"]

[tool.ruff.lint]
# BLE 是刻意启用的：引擎只在明确的边界（event sink、best-effort 清理）捕获宽泛异常，
# 每处都带 `# noqa: BLE001` 并写明理由。这样异常是“可审计的”而非“隐形的”。
select = ["E", "F", "W", "I", "N", "UP", "B", "BLE", "A", "C4", "SIM", "T20", "RUF"]
ignore = ["E501"]

[tool.ruff.lint.per-file-ignores]
"src/porter_cli/**" = ["T20"]     # CLI 前端允许 print
"tests/**"          = ["T20"]
# 控制流信号与守卫异常，不是“故障”。为满足 N818 改名反而会模糊语义并破坏公开 API。
"src/porter/errors.py"           = ["N818"]
"src/porter_mcp/stdout_guard.py" = ["N818"]

[tool.mypy]
python_version = "3.10"
strict = true
ignore_missing_imports = true
files = ["src"]
# src/ 布局必需：缺这两项时 mypy 会把 tools/meta.py 当成两个模块名，直接拒绝检查。
mypy_path = "src"
explicit_package_bases = true
namespace_packages = true

# ---------------------------------------------------------------------------
# 分层守卫：引擎不得依赖前端
# ---------------------------------------------------------------------------
[tool.importlinter]
root_package = "porter"
# porter_cli / porter_mcp 是 `porter` 的顶层兄弟包，必须按外部包处理才能被契约解析。
include_external_packages = true

[[tool.importlinter.contracts]]
name = "engine must not import frontends"
type = "forbidden"
source_modules = ["porter"]
forbidden_modules = ["porter_cli", "porter_mcp"]

# 注意：“两个前端互不依赖”改用 tests/unit/test_architecture.py 断言，
# 因为 import-linter 只能解析位于 root_package 内部的契约 *source*。

[[tool.importlinter.contracts]]
name = "engine layers"
type = "layers"
# 自上而下。上层可 import 下层；不可 import 上层，也不可 import 同层的兄弟模块 ——
# 所以这个列表必须是严格全序，不能把“平级模块”用 | 归为一层。
layers = [
    "porter.pipeline",
    "porter.asr | porter.translate | porter.platforms",
    "porter.media | porter.subtitles",
    "porter.ports",
    "porter.context",
    "porter.models",
    "porter.config",
    "porter.events",
    "porter.logging | porter.errors | porter.utils",
]

[tool.pytest.ini_options]
testpaths = ["tests"]
asyncio_mode = "auto"
```

### 7.2 PyPI

**实测名称可用性**（`https://pypi.org/pypi/<name>/json`）：

| 名称 | 状态 |
|---|---|
| `porter` | ❌ 被占（"Porter: Simple File Operations in Python"） |
| `porter-cli` | ❌ 被占（portermetrics，v1.0.6） |
| **`porter-workflow`** | ✅ **可用** ← 采用 |
| `porter-core` | ✅ 可用（本方案不使用） |
| `porter-mcp` | ✅ 可用（本方案不使用） |
| `porter-skill` | ✅ 可用（本方案不使用） |

【决策】单发行包 `porter-workflow` + `porter` / `porter-mcp` 两个 console script。
`porter-cli` 仅作为**仓库内目录名**与**概念名**存在，不占用 PyPI。

三种调用路径：

```bash
# CLI
uvx porter-workflow "https://youtu.be/xxx"

# MCP（uvx 指定 extras 的正确语法：--from）
uvx --from "porter-workflow[mcp]" porter-mcp

# pipx
pipx install "porter-workflow[all]"
```

MCP 客户端配置：

```json
{
  "mcpServers": {
    "porter": {
      "command": "uvx",
      "args": ["--from", "porter-workflow[mcp]", "porter-mcp"]
    }
  }
}
```

### 7.3 EXE 发布：管理型启动器，不冻结 yt-dlp

**纯 PyInstaller 冻结方案的硬伤**：

1. `yt-dlp` 是**周级更新**。YouTube 改版后，冻进 exe 的 yt-dlp 当天就废，用户必须重下整个 exe。
2. `--collect-all yt_dlp` 打出的包 60 MB+。
3. 冻结后 `remote_components={"ejs":"github"}` 的动态下载路径可能被 PyInstaller 的路径假设破坏。

**采用方案：exe 只做引导器，不冻结引擎。**

```
porter.exe
  ├─ 定位/创建 ~/.porter/venv
  │    └─ 首次：uv venv + uv pip install "porter-workflow[all]"
  ├─ 版本检查：yt-dlp 是否过旧 → uv pip install -U "yt-dlp[default]"
  └─ os.execv(venv_python, ["porter", *argv])
```

**这正是 `scripts/run_porter.py` 已在做的事**（venv 探测 + `os.execv` 自举），只是正式化为发布形态。

| 优点 | 说明 |
|---|---|
| yt-dlp 永远可更新 | YouTube 改版不会让用户卡死 |
| exe 体积 ≈ 几百 KB | 只含引导逻辑 |
| CLI / MCP / skill 共用同一 venv | 行为完全一致 |
| 离线场景 | `bootstrap.sh` 仍保留，可指向本地 wheel |

**外部二进制侧载**（不打包进 exe）：

```bash
porter doctor --install-binaries          # 下载静态 ffmpeg(libass) + deno 到 ~/.porter/bin/
```
- 下载前校验 SHA256，写入 `~/.porter/bin/manifest.json`；
- 解析优先级：`$PORTER_FFMPEG_PATH` > `~/.porter/bin/ffmpeg` > `PATH` 上的 `ffmpeg`。

### 7.4 Skill 安装路径

`npx skills` 的发现目录白名单**包含 `skills/`**，且支持子路径：

```bash
npx skills add RolinShmily/porter-workflow                       # 自动发现（仓库内仅 1 个 SKILL.md）
npx skills add RolinShmily/porter-workflow --skill porter-skill  # 显式按名
npx skills add RolinShmily/porter-workflow/skills/porter-skill   # 直达子路径
```

**硬约束：全仓库只能有一个 `SKILL.md`。** 否则裸命令会弹多选菜单，破坏一键安装体验。

**skill 名保持 `porter-skill` 不变**（spec 要求 `name` 必须等于父目录名），已安装用户的触发词不失效。仓库叫 `porter-workflow`、技能叫 `porter-skill`，二者不冲突。

`skills/porter-skill/scripts/porter.sh` 全部内容：

```bash
#!/usr/bin/env bash
set -euo pipefail
exec uvx --from "porter-workflow[all]" porter "$@"
```

SKILL.md 新增 frontmatter：

```yaml
---
name: porter-skill
description: ...
license: MIT
compatibility: >
  Requires Python >=3.10 plus FFmpeg (with libass) on PATH. For YouTube,
  an external JavaScript runtime (Deno recommended) is required by yt-dlp.
  Optional: `pip install videocaptioner` for extra local ASR / translation engines.
metadata:
  homepage: https://github.com/RolinShmily/porter-workflow
  version: "0.2"
---
```

---

## 8. MCP 契约

### 8.1 工具集

| Tool | 输入 | 输出 | 用途 |
|---|---|---|---|
| `porter_inspect` | `source: str` | `InspectionResult` | 预检（数秒，不下载媒体），无副作用 |
| `porter_plan` | `url`, `options?` | `Plan` | 返回**解析后的执行计划**：命中哪个 extractor、字幕走原生轨还是 ASR 链、翻译走哪个后端、预估耗时 |
| `porter_job_start` | `JobRequest` | `{job_id}` | **长任务核心**，立即返回 |
| `porter_job_status` | `job_id` | `{state, phase, percent, message, recent_logs}` | 轮询 |
| `porter_job_result` | `job_id` | `JobResult` | 产物清单 |
| `porter_job_cancel` | `job_id` | `{cancelled: bool}` | 触发 cancel token |
| `porter_job_list` | — | `[JobSummary]` | 列出历史/进行中任务 |
| `porter_transcribe` | `audio\|url`, `engine?` | `SubtitleSet` | 阶段级 |
| `porter_translate` | `srt_path`, `target_lang`, `backend?` | `SubtitleSet` | 阶段级 |
| `porter_burn` | `video`, `ass`, `style?` | `BurnResult` | 阶段级 |
| `porter_doctor` | — | `CapabilityReport` | 结构化能力报告 |
| `porter_config` | `action: get\|list` | `dict`（**密钥掩码**） | **只读，禁止写密钥** |

同时提供阻塞式 `porter_run(..., max_wait_seconds)`，但 **job API 才是可靠路径** —— MCP tool call 的默认超时通常 60 s 至数分钟，而压制 1080p 视频需要数十分钟。

### 8.2 资源与提示

- Resources：`porter://docs/architecture`、`porter://config`、`porter://jobs/{id}/log`
- Prompt：`porter://prompts/localize-video` —— 引导 agent 走「inspect → plan → 确认 → job_start → 轮询 → 质检」的闭环

### 8.3 Sampling（MCP 独有增益）

当用户未配置 LLM API Key 时，`porter_mcp` 可发起 `sampling/createMessage`，用**宿主模型**完成字幕翻译与语义纠错。

> 这是 CLI 形态做不到的 —— MCP 形态的 porter 可以**零 API Key 拿到 LLM 级翻译质量**。这也是把 MCP 做成一等公民而非 CLI 包装层的强论据。

### 8.4 工程约束（三条硬线）

1. **stdout 纯净**

```python
# porter_mcp/stdout_guard.py
class _StdoutGuard(io.TextIOBase):
    def write(self, s: str) -> int:
        raise RuntimeError(
            "porter-mcp 检测到向 stdout 写入！这会破坏 JSON-RPC 通道。"
            "请改用 logging（stderr）。"
        )
```
启动时把 `sys.stdout` 换成 guard，仅让 FastMCP 的协议写入器持有真实 fd。开发期立刻暴露污染，而不是生产期静默失败。

2. **并发控制**

```python
HEAVY = asyncio.Semaphore(1)   # burn / download 串行
LIGHT = asyncio.Semaphore(4)   # inspect / doctor 并发
```
避免 agent 并发触发 3 个压制把机器打死。

3. **信号处理**

捕获 `SIGINT` / `SIGTERM` → 取消所有 running job → 清理临时文件 → 退出。现有代码完全没有这个能力。

### 8.5 安全

`porter_config` 只允许 `get` / `list` 且密钥掩码（复用 `cli.py:_mask_secret` 的逻辑）。

**禁止通过 MCP 写入 API Key** —— 密钥会进入对话记录与遥测。写密钥只允许走 CLI：`porter config set llm.api_key=...`。

---

## 9. 分阶段执行手册

> 每个阶段末尾有**验收命令**。不通过不进入下一阶段。
> **AGENTS.md 红线：不得擅自 `git commit`。** 所有变更留在工作区，由用户决定提交。

### P0 —— 仓库整理（✅ 已完成）

```bash
# 已完成：新分支 + 清空旧实现 + 重写 .gitignore + 本手册
# 已完成：仓库改名（GitHub 侧）
gh repo rename porter-workflow        # gh 会同时把本地 origin 改成新地址

# 实测验收（§13.45）
curl -sI https://github.com/RolinShmily/porter-skill    # → HTTP/2 301，location 指向新名
curl -sI https://github.com/RolinShmily/porter-workflow  # → HTTP/2 200
git remote -v                                            # → .../porter-workflow.git
```

**验收结果**：`git remote -v` 指向新地址；旧地址返回 301（GitHub 保留重定向）。

> 注意一个**顺序**坑：提交里的 README / `pyproject.toml` 已经把 URL 写成 `porter-workflow`，所以先改名再 push，推送的那一刻所有链接就是有效的；反之会有一段 404 窗口（虽然改名完成后重定向会把它们救回来）。
> 另：本地目录仍叫 `/home/rol1n/Projects/porter-skill` —— 这只是本地路径，不影响仓库名，且改名会打断 `.venv` 里的 editable 安装路径，因此不动。

### P1 —— 骨架与工具链

```bash
mkdir -p src/porter/{models,platforms,subtitles,asr,translate,media,doctor}
mkdir -p src/porter_cli/commands src/porter_mcp/tools
mkdir -p skills/porter-skill/{references,scripts,assets}
mkdir -p tests/{unit,integration,regression} docs

# 写入 §7.1 的 pyproject.toml
# 写入 src/porter/py.typed（空文件）
# 写入 src/porter/__init__.py（__version__ = "0.2.0"）

uv venv && uv pip install -e ".[dev]"
```

**验收**：
```bash
python -c "import porter; print(porter.__version__)"        # → 0.2.0
ruff check src && mypy src && lint-imports                  # 全过
python -c "import porter_cli, porter_mcp"                   # 可导入（空实现）
```

### P2 —— 引擎外科手术（最大块，风险最高）

按依赖倒序做，每步都跑一次回归测试：

1. **`logging` 替代 `print`**（59 处）
   ```bash
   git show main:porter_skill/subtitle/controller.py > /tmp/ref_controller.py   # 逐段对照
   ```
   启用 `ruff` 的 `T20` 规则，让它把漏网的 print 全部揪出来。
2. **`subtitles/srt.py`**：收敛 3 份 `_convert_vtt_to_srt`、3 份 `_select_*_subtitle_lang`、4 份 `_clean_*_title`。
3. **`media/ffmpeg.py`**：把散落的 `subprocess.run(["ffmpeg", ...])`（`burn.py:201/228`、`base.py:104`、5 个 extractor 的转码/WAV 提取）全部收进 `run_ffmpeg()`。
4. **`platforms/spec.py` + `platforms/base.py` + `registry.py`**：实现模板方法，把 5 个 extractor 削成声明。
5. **`platforms/youtube.py` 的 `build_ydl()`**：`quiet=True` + `noprogress=True` + logging adapter，消除 stdout 污染。

**验收（硬门槛）**：
```bash
pytest tests/regression -q          # main 上 90 个测试的等价版本全绿
ruff check src | grep -c "T20"      # → 0（引擎内零 print）
wc -l src/porter/platforms/*.py     # 5 个平台模块合计 ≤ 700 行
grep -rn "quiet.*False" src/porter/ # → 无输出
```

### P3 —— 解耦

6. **`ports.py`**：定义 `Downloader` / `Transcriber` / `Translator` / `Renderer` Protocol。
7. **`events.py` + `context.py` + `pipeline.py`**：事件驱动、可取消、阶段级 API。
8. **`config.py`**：删除 `~/.pi/...`、`SKILL.md` 探测、videocaptioner legacy toml；引入 `platformdirs`；新增 `PORTER_CONFIG` 环境变量支持。
9. **`doctor/`**：拆 `probes.py`（结构化）/ `guides.py`（文案）；新增 libass / deno / ytdlp-ejs 探测；移除管线开头的无条件 doctor 调用。

**验收**：
```bash
pytest -q                                    # 全绿
lint-imports                                 # 分层守卫通过
pytest tests/unit/test_doctor.py -q          # 新探测项覆盖
# 新增行为测试：
pytest -q -k "cancel or checkpoint"          # 取消与断点续跑
```

### P4 —— 前端

10. **`src/porter_cli/`**：重建命令树；新增 `porter inspect --json` 与 `porter run --json`（供脚本消费）；`--no-doctor` 开关。
11. **`src/porter_mcp/`**：`server.py` + `tools/*` + `stdout_guard.py` + `progress.py`；`jobs.py` 的实现可放在 `src/porter/jobs.py` 供 CLI 复用。

**验收**：
```bash
uv run porter --help
uv run porter doctor
uv run porter inspect "https://youtu.be/dQw4w9WgXcQ"
npx @modelcontextprotocol/inspector uv run porter-mcp     # 手动逐个 tool 点击验证
```
> Inspector 里**必须确认没有任何非 JSON-RPC 输出**。这是 P2 第 5 步的最终检验。

### P5 —— 资产与发布

12. **`skills/porter-skill/`**：迁移 SKILL.md（加 `compatibility` / `license`）、重写三个脚本、迁移 references。

    **SKILL.md 必须写明的前置条件（否则 agent 必然踩坑）**：免 Key 的 ASR 路径**已失效**（§13.21 实测）。SKILL.md 的 description 与正文都要说清：转录需要 `OPENAI_API_KEY` 或 VideoCaptioner CLI，否则任务会在转录阶段以 `every speech-to-text backend failed` 失败。翻译不需要 Key。v0.1 的 SKILL.md 宣称"纯 Python 零 Key 闭环"，那句话现在是**错的**，迁移时不能照抄。
13. **`docs/`**：ARCHITECTURE / CONFIG / MCP / MIGRATION 四份文档。
14. **`.github/workflows/`**：
    - `test.yml`：ruff + mypy + lint-imports + pytest（矩阵 3.10–3.13）
    - `release-pypi.yml`：tag 触发 → build → trusted publishing
    - `release-exe.yml`：矩阵 `windows-latest` / `macos-latest` / `ubuntu-latest` → PyInstaller 引导器 → GitHub Release
15. **`docs/MIGRATION.md`**：v0.1 → v0.2 用户迁移指南（配置路径变化、安装命令变化、`yt-dlp[default]` + Deno 新要求）。

**验收（端到端）**：
```bash
# 用未发布的本地仓库路径安装 skill
npx skills add /home/rol1n/Projects/porter-skill/skills/porter-skill
# 然后在 agent 中执行一次完整搬运，确认：
#   - 字幕含 CJK 且无英文残留（质检）
#   - ffprobe 验证成片 moov atom 完好
```

---

## 10. 测试迁移策略

### 10.1 目录职责

| 目录 | 允许依赖 | 目的 |
|---|---|---|
| `tests/unit/` | 纯函数、无网络、无 ffmpeg | 快速反馈（< 2 s） |
| `tests/regression/` | mock 网络、可选 ffmpeg | **行为等价性守卫** |
| `tests/integration/` | 真实 ffmpeg、真实网络（标记 `@pytest.mark.slow`） | 端到端 |

### 10.2 迁移原则

`tests/regression/` 中：
- ✅ 允许改：`import` 路径、patch target（如 `"porter.platforms.base.build_ydl"`）
- ❌ 不许改：任何断言的内容、期望值、fixture 数据

若某条断言**必须**改，说明行为确实变了 → 必须在 PR 描述中单独列出并给出理由。

### 10.3 新增必测项

| 测试 | 断言 |
|---|---|
| `test_no_stdout_pollution` | 用 `capsys` 跑完整 `Pipeline`（全 mock），断言 `capsys.readouterr().out == ""` |
| `test_cancel_mid_pipeline` | 在 `PhaseStarted` 回调里 set cancel → 断言抛 `JobCancelled` 且临时文件被清理 |
| `test_engine_has_no_print` | AST 扫描 `src/porter/**/*.py`，断言无 `print` 调用（比 ruff 更硬的守卫） |
| `test_subtitle_lang_selection` | 同一组输入，新实现与 `main` 的旧实现输出逐字节相同 |
| `test_vtt_to_srt_equivalence` | 同上（3 份旧实现 → 1 份新实现，比 3 份输出的交集） |
| `test_doctor_reports_libass` | mock `ffmpeg -filters` 输出，断言识别出 libass |
| `test_mcp_tools_schema` | 用 `fastmcp.Client` in-memory 断言每个 tool 的 input schema 稳定 |

### 10.4 分层守卫

```bash
lint-imports      # 由 §7.1 的 [tool.importlinter] 配置驱动
```
配合 `test_engine_has_no_print`，机械式守住 §3.1 的架构铁律。

---

## 11. 风险、回滚与红线

### 11.1 风险登记

| 风险 | 影响 | 缓解 |
|---|---|---|
| P2 拆分 `formatter.py`(1184 行) 引入回归 | 字幕排版错乱 | **只做物理移动，一行算法不改**；用 `test_vtt_to_srt_equivalence` 逐字节比对 |
| `PlatformSpec` 声明式改造漏掉平台特性 | 某平台下载失败 | 5 个平台的现有测试作为护栏；先做 YouTube（最简单），最后做 Bilibili（最复杂） |
| yt-dlp API 非稳定接口 | 未来某版不兼容 | 保留 `build_ydl()` 单一出口 + `YtDlpBackend` 适配层，可切 CLI |
| exe 引导器在无 `uv` 的机器上失败 | Windows 用户装不上 | `bootstrap.sh` 回退到 `python -m venv`；exe 内置 `uv.exe` |
| PyPI 名称/信任发布配置错误 | 发布失败 | 先用 `--index-url https://test.pypi.org/legacy/` 试发 |
| 配置搜索顺序变化 | 老用户配置失效 | `docs/MIGRATION.md` + `porter doctor` 显式打印当前生效的配置来源 |

### 11.2 回滚

```bash
# 整分支作废
git checkout main

# 取回单个文件参考（不提交）
git restore --source=main -- porter_skill/subtitle/formatter.py

# 从零再清空
git checkout main && git branch -D refactor/porter-workflow
```

### 11.3 红线（不做的事）

- ❌ 不 `git commit`（AGENTS.md）；不 `git push --force`；不 `git reset --hard`
- ❌ 不把 `videocaptioner` 写进 `dependencies`（GPL + 版本锁定）
- ❌ 不清空 `.memory/` / `.pi/` / `porter_output/`（gitignored，**不可从 git 恢复**）
- ❌ 不改 `raw/` 与 `cooked/` 的输出目录契约（用户已有产物会失效）
- ❌ 不在 P2 顺带优化字幕算法（结构重构与算法改动必须分成两个 PR）

---

## 12. 附录：命名与合规检查清单

### 12.1 命名对照

| 概念 | 名称 | 位置 |
|---|---|---|
| GitHub 仓库 | `porter-workflow` | RolinShmily/porter-workflow |
| PyPI 发行包 | `porter-workflow` | PyPI |
| 引擎 import 名 | `porter` | `src/porter/` |
| CLI import 名 | `porter_cli` | `src/porter_cli/` |
| MCP import 名 | `porter_mcp` | `src/porter_mcp/` |
| CLI 命令 | `porter` | console script |
| MCP 命令 | `porter-mcp` | console script |
| Agent Skill 名 | `porter-skill` | `skills/porter-skill/`（frontmatter `name` 必须等于目录名） |

### 12.2 合规检查清单

- [ ] `LICENSE` 仍为 MIT，未被覆盖
- [ ] `videocaptioner` 不在任何 `dependencies` / `optional-dependencies` 中
- [ ] `videocaptioner` 仅通过 `shutil.which` + `subprocess` 交互，无 `import videocaptioner`
- [ ] README 的第三方致谢章节保留（VideoCaptioner 等）
- [ ] SKILL.md frontmatter 补齐 `license: MIT` 与 `compatibility`
- [ ] `config.json` / `.env` / `*.cookies` 在 `.gitignore` 中（已重写）
- [ ] 无任何真实 API Key 出现在仓库、测试 fixture、文档示例中
- [ ] exe 发布物不含第三方二进制（ffmpeg/deno 走侧载）

### 12.3 关键验收命令速查

```bash
# 结构
lint-imports                                             # 分层守卫
python -c "import porter; print(porter.__version__)"

# 纪律
ruff check src                                           # 含 T20（禁 print）
grep -rn "print(" src/porter/                            # 必须为空
grep -rn "quiet.*False" src/porter/                      # 必须为空

# 行为
pytest tests/regression -q                               # 90 个等价测试
pytest tests/unit -q
pytest -q -k "cancel or checkpoint"

# 端到端
uv run porter doctor
uv run porter inspect "<URL>"
uv run porter "<URL>" -o ./porter_output
npx @modelcontextprotocol/inspector uv run porter-mcp
ffprobe -v error -show_entries format=duration,probe_score \
        -of default=noprint_wrappers=1:nokey=1 <成片.mp4>
```

---

## 13. 实施进度与偏差记录

> 本节记录实际执行结果，以及**原设计被实践推翻的地方**。手册与代码不一致时，以本节为准。

### 13.1 进度看板

| 阶段 | 状态 | 验收证据 |
|---|---|---|
| **P0 仓库整理** | ✅ 完成（含 GitHub 改名：`porter-skill` → `porter-workflow`，旧地址 301 重定向） | 见 §9-P0、§13.45 |
| **P1 骨架与工具链** | ✅ 完成 | 见 §13.2 |
| **P2 引擎外科手术** | ✅ 完成（`fetch()` + `inspect()` + 回归移植；`test_pipeline`/`test_synthesizer` 阻塞于 P3/P4） | 见 §13.7、§13.9 |
| **P3 解耦** | ✅ 完成（端口/事件/取消、platformdirs 配置、doctor 拆分、两条后端链） | 见 §13.12–§13.22 |
| **P4 前端** | ✅ 完成（CLI 重建 + MCP 13 工具 / 作业注册表 / 资源与提示） | 见 §13.27、§13.33、§13.35、§13.38、§13.41、§13.42 |
| **P5 资产与发布** | ✅ 完成 | 见 §13.43、§13.44、§13.45 |

### 13.2 P1 验收结果

```text
ruff check src tests    → All checks passed!
mypy src                → Success: no issues found in 39 source files
lint-imports            → Contracts: 2 kept, 0 broken.
pytest                  → 106 passed in 1.65s
porter --version        → porter 0.2.0
porter <URL>            → 正确路由到 run（返回 exit 2，即 “P2/P3 未实现”）
import porter           → 0 字节 stdout、0 字节 stderr（导入无副作用）
MCP create_server()     → 1 个 tool，porter_version 调用成功
```

交付物（39 个引擎文件 + 5 个测试文件）：

```
src/porter/            errors, logging, events, context, config, ports,
                       pipeline, jobs, utils/{time,text}, models/{metadata,
                       materials,subtitle,request}, py.typed
src/porter_cli/        app, render, __main__, commands/{run,inspect,doctor,
                       config,jobs}
src/porter_mcp/        server, stdout_guard, progress, tools/{__init__,meta}
tests/unit/            test_architecture, test_cli, test_config,
                       test_mcp_server, test_stdout_guard
```

其中**已可用**（非桩）：`config` 全部子命令、`--version`、`--help`、引擎的全部领域模型与配置层。
**待实现**（返回 exit 2 并明说未实现）：`run` / `inspect` / `doctor` / `jobs`。

### 13.3 原设计被推翻的 6 处（重要）

这 6 条是 P1 实际跑出来的结果，手册 §7.1 已同步修正。

#### ① import-linter 的 `independence` 契约无法用于外部包

原设计用 `type = "independence", modules = ["porter_cli", "porter_mcp"]` 表达“两个前端互不依赖”。
**实测失败**：`Module 'porter_mcp' does not exist.`
原因：import-linter 只能解析位于 `root_package` **内部**的契约 *source*；`porter_cli`/`porter_mcp` 是 `porter` 的顶层兄弟包。
即使 `source_modules = ["porter_cli"]` 也同样失败。

**修正**：该约束改用 `tests/unit/test_architecture.py::test_frontends_do_not_import_each_other` 以 AST 断言实现。
`forbidden` 契约（source 在 `porter` 内部、forbidden 为外部）**可以**正常工作。

#### ② layers 契约中同层模块必须互相独立

原设计把 `porter.models | porter.config | porter.errors | porter.events` 归为一层，
**实测失败**：`porter.config is not allowed to import porter.errors`。
即 `|` 表示“这些模块彼此不得互相 import”，而不是“同一层”。

**修正**：layers 列表必须是**严格全序**。新顺序按真实依赖方向排列（`utils` → `errors`/`logging` → `events` → `config` → `models` → `context` → `ports` → `media`/`subtitles` → `asr`/`translate`/`platforms` → `pipeline`）。
新增 `porter.utils` 子包（`time.py` + `text.py`），位于最底层。

#### ③ src 布局下 mypy 必须显式配置

原设计只有 `files = ["src"]`，**实测报错**：`Source file found twice under different module names: "tools.meta" and "porter_mcp.tools.meta"`，并直接中止检查。

**修正**：补上 `mypy_path = "src"` + `explicit_package_bases = true` + `namespace_packages = true`。

#### ④ `SubtitleItem` / `TranscriptSentence` 必须是 dataclass，不能是 pydantic

原设计 §5.1 说“全部使用 pydantic.BaseModel 或 dataclass”。
**实测发现**：v0.1 测试用**位置参数**构造它们 —— `SubtitleItem(1, 0, 3000, "text", "")`（`tests/test_subtitle.py:98` 等 10 余处）。
pydantic v2 不支持位置参数，改用 pydantic 会迫使 §10.2 “不许改断言”原则被破坏。

**修正**：分而治之 ——
- 热路径数据（`SubtitleItem`、`TranscriptSentence`）保持 **dataclass**，字段名与 v0.1 逐字一致；
- 跨边界信封（`SubtitleSet`、`RawMaterials`、`JobRequest`、`JobResult`、`VideoMetadata`）用 **pydantic**，因为它们需要 JSON schema 供 MCP 使用。

#### ⑤ `ArtifactReady` 事件需要 `phase` 字段

原设计的事件只有 `kind` + `path`。写 `Pipeline` 时发现 CLI 无法把产物归到正确的阶段进度条上。

**修正**：`ArtifactReady` 新增 `phase: Phase`。

#### ⑥ stdout 守卫必须重定向到 **stderr**，而不是“原来的 stdout”

这是 P1 **测试真正抓到的一个 bug**。第一版实现是：

```python
real = sys.stdout                      # ← 错
sys.stdout = _GuardedStdout(real, ...)  # 写入 real，即写回 stdout 本身
```

在 pytest 下 `sys.stdout` 是捕获对象，所以测试表现为“写到了 stdout”，直接失败；
在**生产环境下这就是把 JSON-RPC 通道再次污染**——守卫完全失效。

**修正**：重定向目标固定为 `sys.stderr`；同时在进入守卫区域时清空本线程的记录（否则长驻的 MCP server 会把上一个 tool 的违规记到下一个 tool 头上）。
新增测试：`test_stdout_is_clean_after_the_guarded_region`、`test_log_is_per_thread`。

### 13.4 其他实测发现（不改变设计，但值得记录）

| 发现 | 影响 |
|---|---|
| **fastmcp 4.0.5** 已安装（`pyproject` 只写了 `>=2.11`） | API 与 2.x 有差异：`server.get_tool()` 而非 `get_tools()`；tool 模型字段是 `input_schema` 而非 `inputSchema`。测试已按 4.x 编写。 |
| Python **3.14.6** 是当前开发解释器 | 已避免 PEP 695 泛型语法（`def f[T]()`），因为它需要 3.12+ 而 `requires-python = ">=3.10"`。改用 `TypeVar`。 |
| `argparse._SubParsersAction` 在 `--strict` 下需要类型参数 | 所有 `configure(subparsers)` 签名写成 `argparse._SubParsersAction[argparse.ArgumentParser]`。 |
| 可编辑安装的 `.pth` 只是把 `src/` 加进 `sys.path` | `ruff`/`mypy`/`lint-imports` 都能正常解析，无需额外 `PYTHONPATH`。 |
| `porter config set` 的掩码格式是 `{前3}...{后4}` | 即 `sk-...mnop`，不是 `sk-abc...mnop`。文档与测试已对齐。 |
| `print()` 会拆成 `write("text")` + `write("\n")` 两次调用 | 守卫记录的是**写入片段**而非行；读取时用 `"".join(violations())` 还原。 |

### 13.5 P1 新增（手册原未列出的）文件

| 文件 | 为何需要 |
|---|---|
| `src/porter/utils/{__init__,time,text}.py` | `SubtitleItem` 的 `start_srt`/`start_ass` 属性需要时间格式化；`sanitize_filename` 需要从 5 个 extractor 上提。置于依赖图最底层。 |
| `src/porter/ports.py` | 冻结管线与实现之间的接缝（4 个 Protocol + `AsrBackend`）。 |
| `src/porter/jobs.py` | CLI 与 MCP 共用的 JobStore（含线程安全、事件回放、取消传播、LRU 淘汰）。 |
| `src/porter_cli/render.py` | 事件→终端渲染、退出码常量、`not_implemented()`。**唯一允许 print 的引擎外模块。** |
| `src/porter_mcp/progress.py` | Event → MCP progress 映射，按阶段加权成 0-100。 |
| `README.md` | 同时是 hatchling 的构建元数据，缺它无法 `pip install -e .`。 |
| `skills/porter-skill/README.md` | 记录“为何 skill 在子目录”与两条硬约束；skills.sh 会展示每个 skill 的 README。 |

### 13.6 P2 开工前的环境验证（已完成）

已按你的确认在本机（WSL2）实测，结论**推翻了手册原先的两个假设**：

| 依赖 | 实测 |
|---|---|
| ffmpeg / ffprobe | `/usr/sbin/ffmpeg` n9.0.1，**WSL 原生 Linux 版**，非 Windows 侧。`--enable-libass`、`--enable-libx264`、`--enable-fontconfig`、`--enable-libfribidi`、`--enable-libharfbuzz` 全部就位；`subtitles` 与 `ass` 滤镜均存在 |
| deno | `/usr/sbin/deno` **2.9.6**（yt-dlp 推荐运行时，需 ≥ 2.0.0） |
| node | v24.18.0（备用运行时） |
| 中文字体 | Microsoft YaHei 经 fontconfig 可见（`msyh.ttc`），**无需额外安装** |
| GPU | **RTX 4060 Laptop 8GB**，driver 616.92 |
| CPU | i9-13900H，10 核 |

**Phase 4 全链路已实测通过**：构造含中文与英文的 `.ass`，用 `-vf ass=...` 烧录后抽帧目视检查 —— 中文字形正确、标点正确、描边正确、双语分层正确、**无 tofu**。

硬件对比（1080p30 × 600 帧）：

```
h264_nvenc -preset p4 -cq 19      wall=2.88s   (1.46× 快)
libx264 -preset veryfast -crf 18  wall=4.21s
```

#### ⚠️ 硬件档位探测的设计结论

本机 **`/dev/dri` 与 `/dev/nvidia*` 都不存在**，但 NVENC 完全可用（WSL2 走 `/dev/dxg` + `/usr/lib/wsl/lib/libcuda.so`）。因此：

1. **任何基于设备路径的探测在 WSL2 上都会误判**为 Tier B/C，白白损失 1.46× 性能。
2. **“编码器编译进去了” ≠ “运行时可用”**。发行版 ffmpeg 普遍带 `h264_nvenc`，但无驱动时压制会在**任务末尾**（已完成下载、转录、翻译之后）报 `Cannot load libcuda.so.1` 而失败 —— 这是最贵的失败位置。
3. `v0.1` 的 `detect_hardware_profile()` 确实有这个缺口：NVENC 分支**只查编码器列表，完全不查设备**（QSV 分支至少查了 `/dev/dri`）。本机不触发，换台机器会。

**P3 的修正方案**：档位探测改为 **1 帧试编码**（`ffmpeg -f lavfi -i color=... -frames:v 1 -c:v h264_nvenc -f null -` 看退出码），而不是查列表或查设备路径。这同时自动解决 WSL2 的 `/dev/dxg` 问题，且对未来任何新后端（AV1、Vulkan）都成立。

### 13.7 P2 进度（`fetch()` 管线已完成）

| 交付物 | 内容 |
|---|---|
| `porter/subtitles/srt.py` | 收敛 **3 份字节级相同**的 `_convert_vtt_to_srt` / `_select_source_subtitle_lang` / `_select_chinese_subtitle_lang` |
| `porter/platforms/titles.py` | 收敛 **4 份**标题清洗器（其中 3 份是同一算法，只差前缀与截断长度） |
| `porter/platforms/ydl.py` | 唯一的 `build_ydl()` 构造点 + stdout 安全约束 + 进度钩子 |
| `porter/platforms/spec.py` | `PlatformSpec` / `SubtitleSource` 声明式数据 |
| `porter/platforms/base.py` | `YtDlpExtractor` 模板：`probe()` + **`fetch()` 全管线** |
| `porter/platforms/registry.py` | 显式、幂等的注册表（取代 decorator 副作用） |
| `porter/platforms/{youtube,x,instagram,tiktok,bilibili}.py` | 每个仅一份 spec，无管线代码 |
| `porter/media/{ffmpeg,probe,standardize,enhance}.py` | ffmpeg 边界层（进程安全 + 错误上报 + 转码 + WAV + 增强） |

**规模收敛**：

| | v0.1 | v0.2 |
|---|---|---|
| 5 个平台模块 | 2565 行 | **224 行**（每平台 ~45 行） |
| 5 个 extractor + base | 2725 行 | — |
| 平台层总计 | 2725 行 | 1794 行（含 registry/ydl/spec/titles/media 等公共设施） |

**网络实测**（真实 YouTube URL，非 mock）：

```
extractor: youtube
title: Big Buck Bunny 60fps 4K - Official Blender Foundation Short Film
id: aqz-KE-bpKQ   duration: 635.0
dims: 3840 x 2160   vertical: False
```

`probe()` 全链路（yt-dlp → `YdlLogRouter` → `VideoMetadata`）在真实网络上工作正常。

**待完成**：`test_pipeline.py` / `test_synthesizer.py` 的移植（**阻塞于 P3/P4 功能缺失**，非投入不足），以及 `porter run` 的接线。

### 13.8 P2 新发现的 bug

#### ① `remote_components={"ejs": "github"}` 被静默丢弃（**从 v0.1 继承**）—— 影响未能复现

`yt-dlp` 内部执行 `set(params['remote_components'])`，**传 dict 会被迭代成它的 key**：

```
v0.1 dict form   -> yt-dlp resolved: set()
correct form     -> yt-dlp resolved: {'ejs:github'}

yt-dlp 原话: WARNING Ignoring unsupported remote component(s): ejs.
             Supported remote components: ejs:github, ejs:npm.
```

**bug 本身确凿**：v0.1 写下的值不是它想表达的值，yt-dlp 会警告并丢掉。

**但影响没有复现，我不做过度声明。** 实测对比（真实 URL，两种配置各跑一次）：

| 配置 | formats | 带直链 |
|---|---|---|
| `["ejs:github"]`（修正后） | 53 | 53 |
| `()`（等价于 v0.1 的实际效果） | 53 | 53 |

**完全相同。** 又试了只用 `web` client（最依赖 JS 挑战的组合）：三种配置（正确形式 / 无组件 / dict 形式）**全部同样失败**，说明该视频的失败原因与 EJS 无关（更可能是 PO token / bot 检测）。

**结论**：修正后代码与官方文档一致，属**正确性修复**；但“因此获得了更多格式/更高画质”这一说法**在本样本上不成立，不应写进任何发布说明**。EJS 何时真正起作用，需要在遇到具体失败案例时再测。

#### ② 取消信号被重试处理器吞掉（**我自己在 `fetch()` 里引入的**）

`_download_media` 为了做限流重试而捕获了宽泛的 `Exception`，于是 `JobCancelled` 也被吃掉：用户点了取消，得到的却是 `ExtractionError: could not download any media`，**并且会再发起第二次下载**——正是用户刚刚要求中止的那件事。

修正：`except JobCancelled: raise` 置于宽泛捕获之前（subtitle 下载路径同样处理）。对应测试 `test_cancel_between_phases_is_not_swallowed`。

#### ③ 轮播选择的静默错答（**我自己引入的**）

`_first_video_entry` 在没有任何 entry 带视频流时，会记一条 warning 然后**照样返回第一个 entry**。这是和 `get_video_dimensions` 返回 `1920x1080` 同一类问题：把“不知道”伪装成“一个值”，失败被推迟到标准化阶段，报错信息也不再提及“这是个图片轮播”。

修正：改为**明确抛错**，错误信息带上 entry 数量。对应测试 `test_carousel_without_any_video_raises`。

#### ④ `quiet=True` **不足以**保护 stdout（**从 v0.1 继承**，已修）

`YoutubeDL.__init__` 里有**三个**流：

```python
self._out_files = Namespace(
    out=sys.stderr if logtostderr else sys.stdout,   # ← 进度打印机用这个
    screen=sys.stderr if quiet else stdout,
    error=sys.stderr,
)
```

`quiet=True` 只搬走了 `screen`；`out` 仍是 stdout，而 `downloader/common.py` 的 `MultilinePrinter` 正是写 `out`。**必须同时设 `logtostderr=True`**（外加 `noprogress=True` 与自定义 `logger`）。`v0.1` 的 `youtube.py` 更是直接用了 `quiet: False`。

新增 `test_out_files_never_point_at_stdout` —— 断言**效果**而非标志位。

### 13.9 P2.5 回归测试移植 + 补齐两处功能缺口

移植 v0.1 的 90 个根目录测试时，发现 P2 声称"完成"但实际有 **两处功能完全缺失**，而 CLI 早已把它们写进了 `--help`：

| 缺口 | 证据 | 后果 |
|---|---|---|
| **URL 规范化** | 全仓库 grep `resolve_and_clean_url` / `utm_source` / `canonical` = 0 命中 | 带 `?spm_id_from=...` 的分享链接原样进入 yt-dlp |
| **inspect 实现** | `src/porter_cli/commands/inspect.py` 返回 `not_implemented`，`InspectionResult` 曾计划移入 `models/` 但**并未创建** | `porter inspect` 是个空壳；MCP 的 `porter_inspect` 同样无实现 |

两者现已补齐：`porter/platforms/urls.py`、`porter/models/inspection.py`、`porter/platforms/inspector.py`、`PlatformRegistry.canonicalize()`，并接线 `porter inspect`（报告走 stdout、横幅走 stderr、不可用链接 exit 1）。

**实测**（真实 URL）：

```
$ porter inspect "https://www.youtube.com/watch?v=aqz-KE-bpKQ&t=30s&utm_source=share"
Platform:      YOUTUBE
Canonical URL: https://www.youtube.com/watch?v=aqz-KE-bpKQ&t=30s   ← t 保留，utm_source 剥离
Video ID:      aqz-KE-bpKQ
Title:         Big Buck Bunny 60fps 4K - Official Blender Foundation Short Film
Author:        Blender
Duration:      10:35 (635s)
Resolution:    3840x2160 (Horizontal 16:9)
Subtitles:     None (ASR will be used)
```

#### 转移参数的作用域：v0.1 那张全局表是错的

v0.1 用**一张全局** `STRIP_QUERY_PARAMS`，然后在循环里塞了一个 `if is_youtube and k == "t"` 的特例来撤销它对 YouTube 的伤害。测试本身就证明了这张表不可能是全局的：

| 平台 | `t` 的含义 | 要求 |
|---|---|---|
| YouTube | `watch?v=...&t=15s` 起播时间点 | **必须保留** |
| X | `?s=20&t=abcdef` 分享噪声 | **必须剥离** |

两个要求互相矛盾，除非知道平台。因此拆成：

* `CAMPAIGN_PARAMS` — 任何平台都是噪声（`utm_*`、`fbclid`…），可在不知道平台时安全剥离
* `AMBIGUOUS_PARAMS` — 在 A 站是噪声、在 B 站是功能参数（`t`、`s`、`from`、`ts`、`rt`…），**只**经 `PlatformRegistry.canonicalize()` 按平台集合剥离

关键取舍：`clean_url()` 的 `strip` 参数**改为必填、无默认值**。任何默认值都对某类调用方是错的（用 `TRACKING_PARAMS` 会静默毁掉 YouTube 的 `t`；用 `CAMPAIGN_PARAMS` 会静默留下 X 的 `t`），而静默的错答比要求一个参数更糟。

未知站点的行为：只剥 `CAMPAIGN_PARAMS`，保留 `t`/`from` —— 没有平台可查时无权猜测哪个名字有效。

### 13.10 P2.5 新发现的 4 个 bug

#### ① `_first_video_entry` 把"有 formats 键"当成"有视频"（**我自己在 P2 写的**）

```python
for entry in candidates:
    if entry.get("formats"):   # ← 图片轮播的图片也有 formats 键
        return entry
```

Instagram 图片轮播的每个 entry 都带 `formats: [{"vcodec": "none", "url": "...jpg"}]`，于是**图片被当作视频选中**。这正是我在 §13.8 批评 v0.1 的同一个错误，我原样重犯了——因为 `inspector.py` 里写对了、`base.py` 里写错了，两份谓词各自演化。

**修法不是改这一处**，而是把 `has_video_stream()` 提升为唯一实现放在 `platforms/ydl.py`，两处共用。两份拷贝正是 bug 复现的机制。

顺带确定了三态语义：

| `vcodec` | 判定 | 理由 |
|---|---|---|
| `"none"` | **非**视频 | 明确声明纯音频（YouTube 140/251、图片轮播） |
| 已设置（非 none） | 视频 | — |
| **缺失** | **可能**是视频 | **未知 ≠ 否定** |

最后一行的不对称是刻意的：猜"有视频"而猜错 → 任务启动后在下载阶段失败；猜"无视频"而猜错 → **拒绝一个好链接且用户无法覆盖**。两种错误不等价，所以平局时倒向"试一下"。

#### ② `sanitize_filename` 不剥离 NUL 与 ESC（**从 v0.1 继承**）

v0.1 的字符类是 `[\\/*?:"<>|\r\n\t]`——覆盖了 `\r\n\t`，但控制字符远不止换行符。标题来自远端元数据，属不可信输入：

* **NUL** — Linux 上 `open()` 抛 `ValueError: embedded null byte`，另一些路径则**静默截断**
* **ESC** — 视频标题可以驱动终端，精心构造的标题能在操作员跑 `porter inspect` 时重绘输出

改为匹配整个 C0 + DEL 区间 `[\x00-\x1f\x7f]`，一次覆盖两者，不必逐一枚举滥用方式。

#### ③ `registry()` 依赖导入顺序

```
supported platforms: none registered
```

`get_extractor` / `canonicalize` / `inspect_url` 都走 `registry()`，而它原先只返回 `_REGISTRY`——填充完全依赖 `porter.platforms.__init__` 恰好先被执行。实际能工作**只是因为**导入 `porter.platforms.registry` 会先导入父包，是导入顺序的巧合而非保证。一旦破裂就是"所有 URL 都不支持"且错误信息自相矛盾。

修法：`registry()` 内 `register_builtins()`（本身幂等，故只是一次标志位检查）。

#### ④ `caption_to_title` 不做路径分隔符清洗——**这不是 bug，是我测试写错了**

我把路径安全断言加在了 `caption_to_title` 上，它失败了。但 `titles.py` 的 docstring 明确写着分隔符由 `sanitize_filename` 处理——这是一个**两段式**边界，我对着错误的那一段做了断言。已改为断言真正落到文件系统的组合（`sanitize_filename(caption_to_title(...))`，即 `TaskLayout.build` 的行为），并新增断言 `layout.task_dir.parent == 根目录`（直接验证"逃不出去"本身，而非验证分隔符不存在）。

### 13.11 P2 另外三项设计修正

#### `-nostdin`：实测是纵深防御，不是修复线上 bug

我原先的表述是“ffmpeg 会读 stdin，而 MCP 的 stdin 是 JSON-RPC 通道，因此这是严重风险”。**实测推翻了这一表述**：

```
ffmpeg -nostdin  → 管道中的哨兵字节完好
ffmpeg（无标志）  → 管道中的哨兵字节同样完好
```

原因：ffmpeg 用 `tcgetattr(0)` 判断是否可交互，而管道上该调用失败，所以**在 Linux + 管道的组合下它根本不读 fd 0**。

仍然同时保留 `-nostdin` 与 `stdin=subprocess.DEVNULL`，理由是：该保护是 POSIX 特有的（Windows 无 `tcgetattr`）；MCP 客户端可能给的是 pty；且 `-nostdin` 是 ffmpeg 官方文档对非交互调用方的要求。**但这是纵深防御，不是“修了一个会损坏协议的 bug”** —— 这个区别在写 commit message 时很重要。

`tests/integration/test_media_pipeline.py::TestStdinIsolation` 把这个观测结果钉住：将来某个 ffmpeg 版本真的开始吞管道 stdin，测试会失败。

#### 封面改用 ffmpeg，不再需要 Pillow

v0.1 用 `requests` + Pillow 把缩略图转成 JPEG。Pillow 是可选 `[images]` extra，所以**没装该 extra 时封面会被静默丢弃**。ffmpeg 已是硬依赖且能处理任何 CDN 可能返回的格式，因此改走 ffmpeg，`[images]` extra 在这条路径上不再需要。

#### 主视频显式 `-map`

v0.1 依赖 ffmpeg 默认的 `-map 0`，会复制**所有**流。源文件若有多条音轨或内嵌字幕，要么让主视频膨胀，要么让 `-c copy` 到 MP4 直接失败（MP4 装不下多数字幕编码）。两条路径现在都取 `-map 0:v:0` 加可选 `-map 0:a:0?`，保证主视频恰好一条视频流一条音频流。对应测试 `test_master_has_exactly_one_video_and_one_audio_stream`。

#### ruff 新增 `S`（bandit）

`src` 中只剩 2 处需显式处置（`subprocess` 调用加 `# noqa: S603` 并说明 argv 为内部构造；一处 `assert` 改写为真实返回路径）。`tests/**` 豁免 `S101`/`S603`/`S607`——断言是测试机制，测试构造 fixture 时调 ffmpeg 是正常的。

| — | 0.3 | 增补 §13.6 环境实测、§13.7 P2 识别层进度、§13.8 P2 新发现的 3 个真 bug。 |
| — | 0.4 | §13.7 更新为 `fetch()` 管线完成；§13.8 修正 EJS bug 的**影响**未能复现；新增 §13.9 三项设计修正（含 `-nostdin` 实测降级为纵深防御）。 |
| — | 0.5 | 新增 §13.9 P2.5 回归移植并补齐 URL 规范化与 `inspect` 两处功能缺口；§13.10 四个新 bug（含我自己重犯的轮播 bug）；§13.11 三项设计修正。 |

### 13.12 P3.1 硬件档位：设备路径 → 试编码

v0.1 的 `detect_hardware_profile()` 有两层错误，第二层比第一层更根本。

**第一层**：NVENC 分支只查 `ffmpeg -encoders`（编译进来 **!=** 运行时可用），不查任何设备。因此一个编译进来但加载不了驱动的编码器，会在任务**末尾**（burn 阶段）才爆出 `Cannot load libcuda.so.1`——用户已经等了几十分钟。

**第二层（更根本）**：QSV/VAAPI 分支查设备路径，但**设备路径本身是错的判据**。本机实测：

```
/dev/dri          不存在
/dev/nvidia0      不存在
/dev/dxg          存在        ← WSL2 的 GPU 通道
```

NVENC 在本机**完全可用**（实测 1.46x），但任何基于设备路径的探测都会判定为 Tier B，白白放弃加速。

因此判据改为**试编码一帧**：

```
ffmpeg -f lavfi -i color=c=black:s=256x144:d=0.1:r=1 -frames:v 1 -c:v <enc> ... -f null -
```

看退出码。这一条判据覆盖 WSL2 的 `/dev/dxg`、未来的 AV1/Vulkan 后端，以及任何"编译进来但跑不起来"的组合。实测本机：

| 编码器 | 结果 | 诊断 |
|---|---|---|
| `h264_nvenc` | 成功 | NVIDIA NVENC |
| `h264_qsv` | 失败 | `could not open encoder before EOF`（GPU 缺失/占用） |
| `h264_videotoolbox` | 失败 | `this ffmpeg was built without h264_videotoolbox` |
| `h264_vaapi` | 失败 | `/dev/dri/renderD128 is not present` |

4 个探测共 1.82s，选定 `h264_nvenc` 共 0.72s。

**两个刻意的例外**：

1. **软件编码不做试编码**。它不可能因缺设备失败，档位由核心数决定（>= 8 核 `veryfast` crf 18，否则 `ultrafast` crf 22）。这是 v0.1 唯一做对的部分，保留。
2. **VAAPI 保留设备检查**。判据是"**只有当编码器的接口本身以该路径定义时才检查路径**"：VAAPI 的接口就是 `/dev/dri/renderD128`，路径不存在则试编码注定失败，直接短路可省一次 subprocess。NVENC/QSV/VideoToolbox 的接口不是路径，所以必须实测。

#### 顺带修掉的 3 个 bug

| bug | 说明 |
|---|---|
| `StrEnum` 是 3.11+ | `requires-python = ">=3.10"`，`ruff target-version = py310`，`mypy python_version = "3.10"`。gate 直接抓到，改为 `class X(str, Enum)` |
| VAAPI 声明了 `device` 却没把 `-vaapi_device` 放进 argv | 字段存在但从未使用，VAAPI 永远选不中。改为 `device_flag` + `device` 两个字段（`input_args` 由二者合成），**两个字段无法各自漂移** |
| 诊断信息太笼统 | 真实 QSV stderr 里有用的是 `[enc:h264_qsv] Could not open encoder before EOF`，而 v0.1 从头截 stderr（`proc.stderr[:200]`），报出来的是**版本横幅**。P2 已改为取尾部；此处再补上模式识别 |

### 13.13 P3.2 doctor：事实与文案分离

v0.1 的 `env_check.CheckResult` 同时装了三件事：布尔状态、`is_warning`（"status=False 但别停"）、以及**多段中文安装说明**。这在一个终端里能用，但 MCP 前端需要的是结构化数据，塞不进一墙中文。

拆成两半，且**依赖方向单向**：

* `doctor/probes.py` → `Finding(key, label, ok, severity, remediation_key)`，纯事实
* `doctor/guides.py` → 按 `remediation_key` 索引的文案

找不到文案不是错误（降级为显示 `detail`），所以**加一个 probe 不会弄坏渲染器**。

#### 严重性分级是核心，不是修饰

```
BLOCKER   任务根本出不来结果          → 退出码 1
DEGRADED  结果出得来，但有真实代价    → 退出码 0
INFO      值得报告，无需行动          → 退出码 0
```

**"没有 GPU" 和 "没有 ffmpeg" 不能读作同一件事**。测试抓到了我自己的一处误用：`libass` 探测失败被我当成阻塞，于是 `report.ok` 为 False——但缺 libass 只是不能烧录，`--burn skip` 依然完全可用。判成阻塞会**拒绝本可成功的工作**。

所以 `Finding` 只能经 `passed`/`degraded`/`blocked`/`info` 构造，不允许单独设置 `severity`：否则会出现 `ok=False, severity=INFO` 这种"只看严重性的调用方认为一切正常"的组合。

本机 9 项全通过：

```
[OK] Python 3.14.6    [OK] yt-dlp 2026.08.19   [OK] ffmpeg n9.0.1
[OK] libass           [OK] NVIDIA NVENC        [OK] deno 2.9.6
[OK] Microsoft YaHei  [OK] Output directory    [OK] ffmpeg & ffprobe
```

#### 补上 v0.1 缺的 3 项检查，都因为失败是**静默**的

| 检查 | v0.1 的状态 | 静默失败的表现 |
|---|---|---|
| `libass` | **有，但写错了** | 用 `"subtitles" in output or "ass" in output` 搜 `ffmpeg -filters` 输出——子串 `ass` 会匹配 `pass`、`classes`、`atadenoise`，所以**对没有 libass 的构建也报通过**。改为 `runner.has_filter("ass")` 按过滤器名解析 |
| `字体` | **完全没有** | 缺 CJK 字体不报错，只是每个汉字渲染成**空框**。任务成功，视频不可用 |
| `JS runtime` | **完全没有** | yt-dlp >= 2025.11.12 起 YouTube 需要外部 JS runtime，缺了**不报错**，只是静默返回更少的格式。`fc-match` 的用法也要注意：它对不存在的字体**从不失败**，而是返回替代字体，所以必须比对返回的族名而不是退出码 |

#### 两个测试抓到的真问题

1. **修复步骤被 `--verbose` 藏起来**。默认只显示 `[WARN] yt-dlp needs an external JavaScript runtime`——却不告诉操作员装什么。修复步骤属于**失败**的呈现，不该受 verbose 控制；verbose 应该只控制**通过项**的细节。
2. **guide 文案里有 em-dash（U+2014）**。`LANG=C` 下 CPython 把 stdout 解析为 ASCII，一个 em-dash 就抛 `UnicodeEncodeError`——**正好在用户排查故障时把 `porter doctor` 整个命令弄崩**。已全部改为 ASCII 并加测试钉住。

### 13.14 P3 结构移植中发现 P2 漏掉整个 SRT 层

写 ASR 接口（`parse_srt_items`）时发现 `parse_srt` 不存在。全面排查后确认 P2.1 **只移植了 3 个转换器**，而 `subtitles/__init__.py` 的文档声称还有 `parse_srt`、`normalize_subtitle_items`、`generate_*_srt` 等。计划 §4.5 把这些分配给 `srt.py`/`phrasing.py`，但从未落地。

**文档声称有、实际没有，比文档空白更糟**：它让"P2.1 完成"这个结论显得有依据。

已补齐并加了回归测试：

| 函数 | 归属 | 来源 |
|---|---|---|
| `parse_srt` | `srt.py` | v0.1 `formatter.py` |
| `generate_bilingual_srt` / `generate_zh_srt` | `srt.py` | 同上 |
| `normalize_subtitle_items` | `phrasing.py` | 同上 |
| `align_bilingual_items` | `phrasing.py` | 同上 |
| `has_chinese_translation` | `phrasing.py` | v0.1 `controller.py` |

顺带修掉 `normalize_subtitle_items` 里 v0.1 的一个 gap：重叠修复循环写的是 `range(len - 1)`，**从不检查最后一条**，所以末尾的零长 cue 存活下来（渲染成单帧不可读文本）。已补。

### 13.15 P3.3/P3.4 链结构（先行）

用户选择"先移植结构"：链的语义（可用性探测、取消传播、错误聚合、CJK 自检）是真正的价值，而免 Key 抓取端点的解析逻辑是最易腐烂的部分。

#### ASR 链

**平台字幕不是"引擎"**。第一版草稿把 `PlatformSubtitleBackend` 做成了 `AsrBackend`，结果 `available(ctx)` 看不到它真正需要的材料（协议只给 context），只好在链里塞一个 `isinstance` 分支来特判。这是"看起来像引擎、行为是特例"的坏味道。

它其实是 **PREPARE 已经取回的一个文件**：没有引擎可探测，没有顺序问题，只有"存在/不存在"。重构为链里的**显式第一步**后，`isinstance` 消失，`available()` 也恢复了本义（"有没有 ASR 引擎可用"）。

**空结果 = 失败，不是成功**。Bcut 在配额耗尽时返回 HTTP 200 + 零条 utterance。把"返回空"当成功，就会写出空字幕文件并报告 DONE。因此空列表在链里走的是**失败**分支：记日志、试下一个。

**取消不是后端失败**。`JobCancelled` 必须在 `except` 之前重新抛出，否则用户按 Ctrl-C 之后还要再等 4 次网络往返。P2 的测试套件在平台抓取器里抓出过这个 bug，所以这里显式守卫而不是指望结构自然正确。

**`available()` 是参考性的，不是保证**。它只探测廉价的本地事实（有没有 key、有没有二进制），无法预测远端的 429，所以链在接受结果时仍然容忍"声称可用但实际失败"的后端。

#### 翻译链与 CJK 自检

自检针对的失败模式很具体：免 Key 后端返回 HTTP 200 + **原样回显输入**（检测到机器人、语言对不支持、配额耗尽后降级为直通）。此时每条 cue 的 `target_text` **非空但仍是英文**。

下游没有任何东西会发现：`SubtitleSet.has_translation` 为 True（目标文本非空），BURN 阶段成功，操作员拿到一个字幕文件里两条一模一样的英文轨，而任务报告 DONE。

所以每个后端的输出在**被接受前**检查是否真有 CJK，没有则与"返回错误"同等对待：记日志、试下一个。用 `has_chinese_translation`（判定 CJK 字符）而不是"目标文本非空"。

**对齐校验**：`outcome.texts[i]` 必须对应 `inputs[i]`。丢一个不可翻译元素会让其后每条 cue 平移，结果是"每一行都翻译得像模像样、但都配在错误的时刻"。链按长度校验（这是从这里能做到的全部），语义由各后端的测试保证。

#### 几何信息必须随 `SubtitleSet` 传下来

`Translator.translate(subtitles, target_lang, ctx)` 拿不到 `RawMaterials`，而 ASS 的 `PlayResX/Y` 是**绝对像素坐标系**——头部与视频不一致就会让每条字幕缩放错误。v0.1 无条件传 1920x1080，这正是竖屏视频拿到横屏排版的机制（与 §13.8 的 `get_video_dimensions` 假数据是同一个病根）。

因此给 `SubtitleSet` 加 `video_width`/`video_height`，由 TRANSCRIBE 从 `raw.info`（PREPARE 对**真实文件**的探测结果）填入；`None` 表示"未测量"，**不是**"假定 16:9"，由消费方决定回退。回退取 16:9 的理由是不对称代价：横屏排版放在竖屏视频上仍然可读，反过来会把文字推到屏幕外。

#### 已知回退（必须先于发布修掉）

v0.1 在送进 LLM 之前会 `reconstruct_sentences_from_fragments` 把碎片 cue 重组成整句，显著改善中文语序。**这条路径尚未移植**，因此当前是逐 cue 翻译，对中文语序可测地更差。这是相对 v0.1 的真实回退，已记录在 `porter/translate/base.py` 与 `porter/translate/chain.py` 的模块文档里。

| — | 0.6 | 新增 §13.12 P3.1 试编码档位探测（含"设备路径本身就是错判据"的实测证据）、§13.13 P3.2 doctor 事实/文案分离与严重性分级、§13.14 P3 结构移植中发现 P2 漏掉整个 SRT 层、§13.15 P3.3/P3.4 链结构设计与已知回退。 |

### 13.16 P3.3/P3.4 完成：10 个后端 + 2 条链

| 模块 | 类 | 保真度 |
|---|---|---|
| `asr/whisper_api.py` | `WhisperApiBackend` | 完整移植，可离线测试（client 可注入） |
| `asr/videocaptioner.py` | `VideoCaptionerBackend` | 完整移植，subprocess 适配器，binary 可注入 |
| `asr/bcut.py` | `BcutBackend` | **端点未验证** |
| `asr/google_web.py` | `GoogleWebBackend` | **端点未验证** |
| `translate/llm.py` | `LLMTranslationBackend` | 完整移植，client 可注入 |
| `translate/mymemory.py` | `MyMemoryBackend` | 完整移植（该 API 有文档） |
| `translate/videocaptioner.py` | `VideocaptionerBackend` / `VideocaptionerLLMBackend` | 完整移植，subprocess 适配器 |
| `translate/bing.py` | `BingTranslateBackend` | **端点未验证** |
| `translate/google.py` | `GoogleTranslateBackend` | **端点未验证** |
| `asr/chain.py` / `translate/chain.py` | `AsrChain` / `TranslationChain` | 链语义全部为新设计 |

"端点未验证"是关于**线上格式**的声明，不是关于结构的。这 4 个模块的可用性探测、错误映射、超时、分批、取消传播都是刻意设计且有测试的；逆向出来的请求/响应字段是尽力而为、未经验证的。每个此类模块的 docstring 开头都有显式声明。

#### 独立验证（不依赖 worker 报告）

10 个后端全部通过协议检查；`available()` 在全部 10 个后端上都不抛异常；依赖缺失时抛的是 `TranslationBackendError` 而非随机异常；空输入返回空输出（长度保持）。

#### 翻译 worker 顺带修掉的 v0.1 bug

v0.1 的逐 cue HTTP 回退会**吞掉错误并把英文原文当作"译文"返回**——这正是 §13.15 描述的"假双语"失败模式，只是发生在后端内部而非链内部。现在批次失败直接抛 `TranslationBackendError`，由链决定是否换引擎；CJK 自检是第二道防线。

#### 链语义（3 条硬规则）

1. **空结果 = 失败**。Bcut 配额耗尽时返回 HTTP 200 + 零条 utterance。当成功处理就会写出空字幕并报告 DONE。
2. **取消不是后端失败**。`JobCancelled` 在 `except` 之前重抛，否则 Ctrl-C 后还要再等 4 次网络往返。
3. **`available()` 只是参考**。它探测廉价的本地事实，无法预测远端 429，所以链容忍"声称可用但实际失败"的后端。

### 13.17 P3 中发现并修掉的一类真 bug：subprocess 与 stdout 的编码

这一类 bug 值得单独记录，因为它有 6 个独立发生点，而**doctor 自己就是受害者**——它本该诊断的正是这种机器。

#### 根因

`subprocess.run(..., text=True)` 不指定 `encoding` 时，用 `locale.getpreferredencoding(False)` 解码子进程输出。实测矩阵：

```
环境                               preferred encoding
LC_ALL=C（默认）                    utf-8          ← PEP 538 locale coercion 自动提升
LC_ALL=C + PYTHONUTF8=0            ANSI_X3.4-1968 ← ASCII
LC_ALL=C + PYTHONCOERCECLOCALE=0    utf-8
```

`PYTHONUTF8=0` + C locale 是**可达且常见**的配置（CI 镜像常显式设 `PYTHONUTF8=0` 让 locale 行为确定）。在这个组合下：

```
UnicodeDecodeError: 'ascii' codec can't decode byte 0xe5 in position 16
```

`0xe5` 是 UTF-8 三字节 CJK 序列的首字节。来源可以是 `fc-match` 返回的本地化字体名（`Microsoft YaHei,微软雅黑`）、ffmpeg 回显的元数据、或任何中文路径。**`porter doctor` 在这种机器上直接崩在探测阶段**，即"最能诊断故障的工具恰好在最需要时失效"。

修法：5 个调用点全部显式 `encoding="utf-8", errors="replace"`。`errors="replace"` 是必要的，因为 ffmpeg 会回显任意元数据字节。

#### 第二个点：stdout 是 `strict`，stderr 不是

PEP 528 让 `sys.stderr` 默认 `errors="backslashreplace"`（永不抛），但 **`sys.stdout` 默认 `strict`**。实测：

```
stdout errors: strict
UnicodeEncodeError: 'ascii' codec can't encode characters in position 11-14
```

所以 `porter inspect` 遇到带中文标题的视频、或 `porter inspect --json`，在 ASCII stdout 下**抛异常终止**——而那时下载/探测已经做完了。

修法两处：

1. `render.make_stdout_safe()` 在 CLI 启动时把 stdout 的 errors 改为 `backslashreplace`（显示降级而非中止命令）。
2. `emit_json` 改用 `ensure_ascii=True`。**这一条不能靠 backslashreplace 替代**：BMP 之外的字符 Python 会输出 8 字符的 `\UXXXXXXXX`，那**不是合法 JSON**，而 `json.dumps` 总是输出代理对。实测确认 `--json` 输出在 ASCII stdout 下仍是合法 JSON 且能被解析。

### 13.18 `Pipeline.default()` 接线与一个 `__len__` 陷阱

`Pipeline.default()` 现在组装 4 个 port：`PlatformDownloader`（新增，按 URL 分派到注册表里的提取器）、`AsrChain`、`TranslationChain`、以及 `_UnwiredRenderer`。

**`_UnwiredRenderer` 是刻意失败的桩，不是静默空操作**。BURN 实现属于 P4；一个"什么都不做"的渲染器会让任务报告 DONE 而没有任何烧录视频，操作员得自己去发现文件缺失。抛 `CapabilityMissingError` 会点名阶段并说明原因，而 `--burn skip`（默认）和 `--only-phase` 根本不经过它。

`PlatformDownloader` 单独成类而不是把 `fetch` 加到注册表上，理由有两条：注册表的 `UrlHandler` 契约刻意只要求 `name` + `can_handle`（这样测试可以放桩而无需 yt-dlp）；且分派是**管线**的关注点，注册表要能被 `inspect` 复用而 `inspect` 绝不能下载。

#### 陷阱：`__len__` 让 `or` 变成 bug

组装测试抓到一个我自己写的 bug：

```python
downloader=downloader or _default_downloader()   # ← 错
```

`PlatformDownloader` 定义了 `__len__`（返回平台数量），于是**空注册表的实例是 falsy**，`or` 会静默丢弃调用方注入的实例并新建一个真的。改用 `is not None` 判断，并把"空 registry 不被替换"写成回归测试。

这个陷阱对任何定义了 `__len__`/`__bool__` 的对象都成立，所以 `default()` 的两个可选参数都用显式 None 判断。

### 13.19 尚未完成的缺口（必须先于发布处理）

> **本节是历史记录。** 表中各项的最终状态见 §13.31；截至 §13.29，`local_video`、
> BURN、句子级翻译和 `porter run` 四项均已完成。

| 缺口 | 影响 | 归属 |
|---|---|---|
| `Pipeline.prepare()` 不支持 `local_video` | 本地视频文件无法处理，只能走 URL | P4 |
| BURN 渲染器未实现 | 无法产出烧录视频（`--burn skip` 可用） | P4 |
| **句子级翻译未移植** | v0.1 的 `reconstruct_sentences_from_fragments` 未移植，当前逐 cue 翻译，中文语序可测地更差——**相对 v0.1 的真实回退** | P4 |
| `porter run` CLI 未接线 | `Pipeline.default()` 可用，但 `run` 命令仍返回 `not_implemented` | 下一步 |
| `escape_ffmpeg_filter_path` | 契约已记录在 `tests/regression/README.md`，实现随 BURN | P4 |
| `porter jobs`（MCP 作业化） | 长任务目前无法通过 MCP 轮询 | P4 |

| — | 0.7 | 新增 §13.16 P3.3/P3.4 完成（10 个后端 + 2 条链，含独立验证与 4 个未验证端点的显式声明）、§13.17 subprocess/stdout 编码类 bug（6 个发生点，doctor 自身受害）、§13.18 `Pipeline.default()` 接线与 `__len__` 陷阱、§13.19 未完成缺口清单。 |

### 13.20 句子级翻译移植完成（修掉 P3 唯一的功能性回退）

§13.15 记录的"已知回退"已消除。v0.1 的流程是**全程句子级**的：

1. `merge_short_fragments` 合并滚动碎片（YouTube 自动字幕是 2-3 词的碎片）
2. `reconstruct_sentences_from_fragments` 重建整句
3. 把**句子**（不是 cue）交给翻译后端
4. `split_chinese_sentence_into_cues` 把译文切回带时间轴的字幕行

v0.2 之前只做了第 3 步的逐 cue 版本，中文语序可测地更差。

#### 差分验证（比读代码可靠）

把 v0.1 的 `formatter.py` 抽成独立模块，对同一批输入逐函数比对 v0.1 与 v0.2 的输出：**47 项检查全部一致，逐字节相同**。覆盖 `restore_english_punctuation_heuristic`、`split_chinese_text_by_phrase`、`split_english_text_to_n_parts`、`merge_short_fragments`、`reconstruct_sentences_from_fragments`、`split_chinese_sentence_into_cues`。

差分验证顺带回答了一个问题：v0.1 在 `reconstruct_sentences_from_fragments` 里跑了一次标点启发式，`split_english_text_to_n_parts` 里又跑一次。实测该函数**幂等**，所以二次应用无害——因此 v0.2 保留了这个双重调用以维持行为一致，而不是"顺手清理"。

#### `TranslationOutcome.sources`：LLM 修正后的英文

v0.1 的 LLM 提示词要求模型**同时**修掉 ASR 错字并输出修正后的英文，v0.1 用修正后的英文写双语轨。v0.2 的 `TranslationOutcome` 只有 `texts`，那份修正英文被丢掉了——结果是"用修正后的英文翻译，却在旁边印未修正的英文"。

新增可选字段 `sources: list[str] | None`（与输入位置对齐）。`None` 表示"没意见"；长度不符时链**拒绝使用**而非勉强配对，因为错配会把 A 句的英文配到 B 句的中文上。LLM 后端现在填充它；其他后端留 `None`。

#### `porter run` 接线

`run` 命令不再是桩。退出码即机器可读结果（0 成功 / 1 失败 / 2 误用 / 130 取消），`--json` 时 stdout 只有 JSON，进度走 `null_sink` 丢弃（而非抑制，这样各阶段不需要知道自己是否被监听）。

#### 修掉 `--only-phase` 的结构性 bug

`--only-phase` 原本实现为"只跑这一个阶段"，但每个阶段消费上一阶段的**内存**输出，所以除 `prepare` 外**每一次都必然失败**：

```
✗ transcribe failed: phase transcribe requires output from PREPARE,
  which did not run or did not complete
```

改为"**跑到这个阶段为止**"（前置阶段照跑），并更新帮助文本。从磁盘恢复单个阶段是另一个未实现特性（`force` 就是为它准备的）。`--only-phase burn --burn skip` 现在选择空集并如实什么都不做，而不是偷偷跑另外三个阶段。

### 13.21 实测结论：免 Key 的 ASR 端点已死，翻译端点正常

只有真实跑一遍才能得到的结论。在 10 分钟视频上跑完整管线，再用该视频的原始 16kHz PCM 直接探测端点。

#### ASR：全部不可用

| 端点 | 结果 |
|---|---|
| Whisper API | 未配置 key，不可用 |
| Bcut | API 主机有响应（`resource/create` 返回 HTTP 200），但真实转录**零条 utterance** |
| Google Web | **每次请求都返回 14 字节 `{"result":[]}`** |
| VideoCaptioner CLI | 未安装 |

Google Web 的探测证据：三个独立语音片段（100s/300s/…）与两种语言变体（`en-US`/`en-GB`）全部返回同样的空结果。**端点还在响应，但不再转录。**

而且它的 chunked 结束帧是坏的，所以 `speech_recognition` 的 `response.read()` 在那同样的 14 字节上抛 `http.client.IncompleteRead`。

**实际后果：今天不存在免 Key 的语音识别路径。** 必须配置 LLM / Whisper API key，或安装 VideoCaptioner CLI，否则转录一定失败。`porter doctor` 现在直接这么说。

#### 翻译：三个都正常

| 后端 | 实测 |
|---|---|
| Bing | ✓ `['你好，世界，这是一个测试。', '编码器比CPU更快。']` |
| Google | ✓ `['你好世界，这是一个测试。', '编码器比 CPU 更快。']` |
| MyMemory | ✓ `['大家好，这是一个测试。']`（首次探测超时属网络抖动，重测 3 次均 0.9-1.4s） |

所以"逆向端点不可靠"这个笼统判断是错的：**翻译可用，ASR 不可用**。这决定了 `porter doctor` 该怎么分级提示。

### 13.22 后端契约：每个预期失败都必须变成 `AsrBackendError`

真实运行时 `http.client.IncompleteRead` 从 `google_web.py` 逃出，穿过链的 `except (AsrBackendError, PorterError)`，以裸 traceback 杀掉进程。

链**刻意**只捕获这两种异常——链级的 `except Exception` 会吞掉真 bug（拼写错误、解包错误）并把它们报告成"后端不可用"，那是最难诊断的一类失败。代价是责任落在后端身上，而传输层正是容易漏掉的地方：

* `requests` 抛 `RequestException`（`bcut.py` 已捕获）
* `urllib` / `http.client` 抛 `HTTPException`，而**截断的 chunked 响应是 `IncompleteRead`——它既不是 `OSError` 也不是 `sr.RequestError`**
* 库还可能抛别的

修法：`google_web.py` 增加 `except (http.client.HTTPException, OSError)` 并映射为 `AsrBackendError`（带 `kind=` 便于日志区分）。契约本身也写进了 `asr/base.py` 的 docstring。

### 13.23 已知缺陷（继承自 v0.1，未修）

**两个无标点的短句会被并成一句。** ASR 输出完全没有标点，所以这不是边缘情况：

```
输入:  (0-700) "what is going on here"  (800-1500) "I think that's right"
输出:  "What is going on here I think that's right?"     ← 一句，且是疑问句
中文:  "我认为这是正确的，这是怎么回事？"                    ← 语序错乱
```

`reconstruct_sentences_from_fragments` 的第五个切分条件是"词数 ≥ 20 **且**下一片段首字母大写"，5 个词远不够，所以合并。而 `restore_english_punctuation_heuristic` 的首词疑问词规则看到 "What" 就加问号，把陈述句标成疑问句。

**这是继承行为，不是回退**，因此本轮不静默改动翻译结果。修它需要一个更保守的判据（例如"下一片段以句首代词/限定词开头"），并配差分测试。

### 13.24 本轮仍未完成的缺口

| 缺口 | 影响 |
|---|---|
| BURN 渲染器 | P4；`--burn skip` 与 `--only-phase translate` 可用 |
| `local_video` 输入 | 本地视频文件仍无法处理 |
| `--force` / 单阶段从磁盘恢复 | `force` 字段存在但无实现 |
| `porter jobs`（MCP 作业化） | 长任务仍无法通过 MCP 轮询 |
| §13.23 的短句合并 | 中文语序与标点可见地错 |
| 免 Key ASR 已死 | 转录必须有 key 或 VideoCaptioner CLI；建议把这一点写进 README/SKILL.md 的前置条件 |

| — | 0.8 | 新增 §13.20 句子级翻译移植完成（含 47 项差分验证与 `sources` 字段）、§13.21 免 Key 端点实测（ASR 已死 / 翻译正常）、§13.22 后端异常映射契约、§13.23 继承缺陷（短句合并）、§13.24 未完成缺口；`porter run` 接线并修掉 `--only-phase` 结构性 bug。 |

### 13.25 修掉短句合并缺陷（有意偏离 v0.1，第 7 条已声明偏离）

§13.23 记录的缺陷已修。做法不是调阈值，而是**先建差分 harness 再动手**：写一个脚本对同一批碎片语料分别跑"旧规则"和"候选新规则"，**逐条打印每一处分组变化**，然后人工判断每一处。修一个 bug 却切坏了间接引语，是净亏损；只有逐条读才知道自己得到的是哪一种。

候选规则：下一片段以主语代词或疑问词开头（`PRONOUN_STARTERS`），**且**当前文本没有以"不能结尾的词"结束。四道守卫各挡一类真实失败：

| 守卫 | 挡住的错误 | 例 |
|---|---|---|
| 不以 `NON_TERMINAL_WORDS` / `DANGLING_TAIL` 结尾 | 切在从句中间 | `the output of` + `the encoder`；`we were talking about` + `you and me` |
| 不以悬垂疑问/关系词结尾 | 从句未完成 | `I know what` + `you mean` |
| 不以转述动词结尾 | 间接引语 | `and then he said` + `I should go` |
| 词数 ≥ 3 | 太短不可能是完整句 | `he said` + `I should go` |

悬垂词表单独定义为 `DANGLING_TAIL` 而**不**并入 `NON_TERMINAL_WORDS`，因为后者与标点启发式共用，加宽它会改变 v0.1 忠实输出。`DANGLING_TAIL` 额外收了 `about` / `during` / `before` / `through` 等 `NON_TERMINAL_WORDS` 里没有的介词——漏掉它们时 `we were talking about` + `you and me` 会被误切。

#### 顺序陷阱：第一次修复是无效的

这条必须记下来，因为它**被宣布完成过一次，而实际没修好**。

第一版只在 `reconstruct_sentences_from_fragments` 加了条件。它的单元测试通过、差分 harness 通过，但真实链路**仍然输出合并后的句子**。原因在调用顺序：

```
translate/chain.py:133   merge_short_fragments(subtitles.items)          ← 800ms 间隔窗口
translate/chain.py:134   reconstruct_sentences_from_fragments(...)       ← 600ms 间隔窗口
```

`merge_short_fragments` 先跑，窗口 800ms **宽于**重建阶段的 600ms，所以它已经把两个片段并成一个，重建阶段根本看不到边界可切。

修法：把 `starts_new_sentence` 提到 `phrasing.py`（两级共用，放在 `transcript.py` 会导致 `phrasing` 反向导入 `transcript` 的循环），并在 `merge_short_fragments` 里也加守卫。

两条教训，各自都花了真实时间：

1. **差分 harness 必须模拟真实调用顺序。** 第一版 harness 把两个阶段分别单独测，报告成功，而产品是坏的。
2. **只有端到端执行能作为证据。** 该修复被宣布完成过一次，其实没有；重跑真实流水线才暴露出来。

#### 最终验证（真实 Google 后端，6 条碎片 → 4 行字幕）

```
So the output of the encoder is wrong,  -> 所以编码器的输出是错误的，
and we need to fix it before we ship.   -> 我们需要在发货前修复它
What is going on here?                  -> 这是怎么回事？
I think that's right.                   -> 我认为这是对的。
```

修复前最后两行是一条合并字幕，译作 `我认为这是正确的，这是怎么回事？`——从句顺序颠倒。

两级流水线下的差分结果：目标用例全部正确切分，"不得切分"用例全部受保护，剩余变化经逐条判断均为正确切分。零残留误伤。

既有 889 个测试**无一失败**——新条件只在旧规则会合并的位置触发，是纯增量改动。新增 `tests/unit/test_sentence_reconstruction.py` 48 个测试，其中 `TestTheTwoStagePipeline` 专门按真实顺序跑两级流水线——**就是这一组能抓到第一版无效修复**。该**输出变更**（不同于前 6 条签名/错误类型变更）记入 `tests/regression/README.md` 第 7 条已声明偏离。

### 13.26 文档：把"转录需要 Key"提到显眼位置

§13.21 的实测结论此前只存在于代码注释和本计划里，而 README 仍在宣称"纯 Python、**零 Key 可用**"、`[stt]` 是"Google Web STT 兜底"——两句话现在都是错的，用户会照着做然后撞墙。

改动：中英 README 各新增"转录必须有 Key（或 VideoCaptioner CLI）"一节，含后端状态表、失败时的确切报错文本、两条可行命令（配 LLM Key / 装 VideoCaptioner），并明确**翻译不受影响**。同时修正安装段的"零 Key"描述与 extras 表里 `[stt]` 的用途。

v0.2 的 `SKILL.md` 属 P5 尚未编写，因此在 §9 P5 把这条列为 SKILL.md 的**硬性内容**，并特别标注：v0.1 的 SKILL.md 宣称"纯 Python 零 Key 闭环"，那句话是错的，迁移时不能照抄。

| — | 0.9 | 新增 §13.25 短句合并缺陷修复（差分 harness 方法 + 四道守卫 + **两级流水线顺序陷阱：第一版修复无效** + 第 7 条已声明偏离）、§13.26 README 前置条件文档化；§9 P5 增加 SKILL.md 硬性内容要求。 |

### 13.27 P4：BURN 渲染器完成（`media/burn.py`）

`--burn dual` 现在真的能出片。`Pipeline.default()` 注入 `FfmpegRenderer`，§13.18 里那个"故意失败的桩"`_UnwiredRenderer` 已删除。

产物（`cooked/`）：

| 文件 | 字幕轨 |
|---|---|
| `video_bilingual.mp4` | `subtitle_bilingual.ass` |
| `video_zh.mp4` | `subtitle_zh.ass` |

#### 路径转义：v0.1 的做法根本不工作

v0.1 把**绝对字幕路径**塞进 filtergraph 并转义：

```
-vf ass='/home/me/it\'s_a_title/cooked/subtitle_zh.ass'
```

实测（真实 ffmpeg）**无效**，且任何单引号转义写法都无效。filtergraph 解析器把 `'` 当引号定界符吃掉，路径静默变成**另一个路径**：

```
subtitles='/tmp/esc/it's here/sub.srt'  ->  "Unable to open /tmp/esc/its here/sub.srt"
```

逐一试过并全部失败：不转义、1/2/3 个反斜杠、shell 的 close-escape-reopen、百分号编码、不加引号、`filename=` 选项语法。1-2 个反斜杠会留下一个字面反斜杠，其余全部丢引号。

**这是可达的**，因为 `sanitize_filename` **保留撇号**（实测 `"it's a title"` → `it's_a_title`），所以标题含撇号的视频（如 "It's a Wonderful Life"）产生的任务目录会让每一次烧录失败。方括号同样能存活。

修法不是转义，而是**把路径从 filtergraph 里彻底移除**：子进程以字幕所在目录为 `cwd`，filtergraph 只写裸文件名（`subtitle_zh.ass`，来自 `asr/chain.py` 的常量，不含任何特殊字符）。用户的输出目录从不进入 filtergraph。

为此给 `FFmpegRunner.run/_spawn` 增加了 `cwd` 参数，并把 `cwd` 一并记入错误详情——filtergraph 失败常常是"相对路径解析到哪"的问题，而不是路径文本的问题。

`escape_ffmpeg_filter_path` 仍然实现（契约已记录，且它对能转义的字符是对的，如 Windows 盘符冒号），但它的 docstring **明确写出它做不到什么**，并有测试断言这段 docstring 存在——因为失败模式是静默的：转义后的字符串看起来没问题，ffmpeg 却读了另一个文件。

#### 第一次实现踩的坑：`resolve()` 抵消了整个设计

`_filter_arg` 最初调用 `escape_ffmpeg_filter_path(Path(subtitle.name))`，而该函数内部 `Path(path).resolve()`——相对的是 **Python 进程的 cwd**，不是子进程的 `cwd`。于是 filtergraph 里出现了 `/home/rol1n/Projects/porter-skill/subtitle_zh.ass`，ffmpeg 报 `Could not create a libass track when reading file`。拆出不做解析的 `_escape_filter_text`，`escape_ffmpeg_filter_path` = 解析 + `_escape_filter_text`。

#### 修掉的 v0.1 缺陷

| 缺陷 | v0.1 | v0.2 |
|---|---|---|
| 先改名后验证 | 无条件 `temp.replace(output)`，截断的编码会被当成成片发布；`is_valid_video_file` 只在**复用**分支调用，而那分支不产生文件 | 临时文件先 `probe()` 通过才改名 |
| 无超时 | `subprocess.run` 不传 timeout，卡死的编码永不返回 | `BURN_TIMEOUT_SECONDS = 7200` |
| ASS 失败静默回退 SRT | 失败后改用同目录 `.srt` 重烧，产出**风格不同**的视频却报成功 | 直接 `RenderError`，并诊断原因（libass 缺失 → 指向 `porter doctor`） |
| `print()` | 复用提示走 stdout | logger |
| 硬件探测 | `detect_hardware_profile` 按设备路径猜 | `EncoderSelector` trial encode（P3.1） |
| `-pix_fmt` 重复 | — | 第一次实现自己加了 `-pix_fmt yuv420p`，与 profile 的重复；改为一律由 `EncoderProfile` 提供 |
| 转码参数 | `FFmpegConfig` 手写 codec/preset/crf | 一律走 `profile.args()` |
| 每次作业重新探测硬件 | — | `FfmpegRenderer` 持有一个 `EncoderSelector`，硬件判定**每进程一次**；MCP 一个进程跑多个作业，每次重探要付真实 ffmpeg 进程的代价 |

#### 顺带发现并修掉的真 bug：`JobOptions` 有两个来源

端到端集成测试抓到的，单元测试抓不到。

`JobOptions` 同时挂在 `JobRequest`（请求了什么）和 `RunContext`（正在跑什么）上，而管线**从不同地方读**：

* `_phases_for` 读 `request.options`（`burn` / `only_phase`）
* `burn()` 与 renderer 读 `ctx.options`（`burn` / `force`）

于是前端只要把两者设得不一样，`--burn zh_only` 就会**选中** BURN 阶段然后烧错变体——静默的，因为两个对象各自都合法。CLI 恰好躲过（它把同一个对象同时传给两者），所以只有真实端到端能发现。

修法：`Pipeline.run()` 开头 `ctx.options = request.options`，并写明**请求优先**（它才是工作单元）。这让"不一致"从"不太可能"变成"无法表达"，并顺带覆盖了 `force` 和 `target_lang` 的同一类缺陷。

#### 验证

* **真实 ffmpeg 端到端**（`tests/integration/test_burn_pipeline.py`，6 项）：整条管线跑到 BURN，两个成片可被 ffprobe 读出（320x240、2.0s、含音视频轨）；**像素级验证字幕真的烧进去了**——master 是纯色（0 个近白像素），成片有 >100 个近白像素。只看"文件存在"或 ffprobe 都会在"什么都没烧"时通过。
* 集成测试的视频标题**故意含撇号**（`It's a Burn Test`），并断言撇号确实进了目录名——用普通目录名测不出这个 bug。
* 中文渲染人工确认无豆腐块（`中文测试 / English test` 清晰可读）。
* `tests/regression/test_synthesizer_port.py` 6 项（v0.1 移植，第 3 条已声明偏离）。
* `tests/unit/test_burn.py` 36 项（argv、失败处理、复用、模式、端口）。
* 复用行为实测：`force=False` 第二次不重编码（mtime 不变），无 `.tmp_*` 残留。

### 13.28 仍未完成的缺口（更新 §13.24）

| 缺口 | 影响 |
|---|---|
| `local_video` 输入 | 本地视频文件仍无法处理；也是 `tests/test_pipeline.py` 移植的唯一剩余阻塞 |
| `--force` / 单阶段从磁盘恢复 | `force` 现在对 BURN 生效（复用判定），但"只跑一个阶段"仍需从磁盘恢复中间产物 |
| `porter jobs`（MCP 作业化） | 长任务仍无法通过 MCP 轮询 |
| 免 Key ASR 已死 | 转录必须有 key 或 VideoCaptioner CLI（§13.21） |
| 句子级翻译的短句合并 | 已修（§13.25） |
| 无字幕视频 | 未处理 |

| — | 1.0 | 新增 §13.27 P4 BURN 完成（路径转义实测否定 + `cwd` 设计 + 7 项 v0.1 缺陷 + `JobOptions` 双来源 bug）、§13.28 缺口更新；BURN 从缺口清单移除。 |

### 13.29 `local_video` 输入（v0.2 新特性，非移植）

v0.1 **只接受 URL**（`porter_skill/cli.py` 里 `url` 是唯一位置参数），所以本地文件不是移植，是新功能。动机写在计划里：「支持本地文件（MCP 场景需要）」——MCP 客户端手里有文件，想要字幕。

#### 架构选择：独立端口，不加宽 `Downloader`

`Downloader` 的三个方法签名都是 `(url: str)`。本地文件不是 URL：`can_handle("/home/me/v.mp4")` 没有意义，`probe` 得先自己判断「这是个文件吗」。

三选一，选了 B：

| 方案 | 代价 |
|---|---|
| A：`LocalFileDownloader` 实现现有 `Downloader`（`file://` 或裸路径） | 改动最小，但参数语义变谎言；两个生产者认领同一字符串时 dispatch 靠「路径存在吗」猜 |
| **B：新增 `LocalPreparer` 端口 + `Pipeline.local` 字段** | 类型诚实，改动局部（新端口 + 新字段 + default 接线） |
| C：把 `Downloader` 加宽成接收 source | 长期最正确，但要改所有平台 extractor、registry 和大量测试签名，对 ~150 行的特性偏重 |

`LocalPreparer` 只有 `name` 和 `prepare(path, ctx)`，**故意没有 `can_handle`**：`JobRequest` 的两个字段互斥且已校验，本来就说了是哪种来源，没什么可猜的。

#### 复用而非复制：`media/prepare.py`

PREPARE 里「来源之后」的工作（standardize → extract_audio → enhance → cover → metadata → cleanup）对 URL 和本地文件**完全一样**。这部分原本是 `YtDlpExtractor` 的私有方法——只有一个调用者时没问题，加第二个调用者正是 v0.1 产生五份粘贴副本的情形。

所以提到 `src/porter/media/prepare.py`，两个来源共用：一个实现，两个来源。文件名常量（`VIDEO_NAME` / `AUDIO_NAME` / `COVER_NAME` / `METADATA_NAME` / `ENHANCED_AUDIO_NAME`）也搬过去，因为它们是 `raw/` 的磁盘契约，不属于任何平台。`platforms` 在分层里高于 `media`，可以 import；反过来是循环。

`write_metadata` / `cleanup_tmp` 移到 `TaskLayout` 上——布局本来就知道这些文件在哪。

#### 身份推导

URL 自带 video id，这正是重跑同一作业能落进同一任务目录、复用 master 的原因。路径没有，所以推导一个：`local-<resolved path 的 sha256 前 8 位>`。

* **稳定**（重跑能复用）
* **唯一**（同名不同目录不冲突——用 stem 当 id 会让第二个视频静默复用第一个的 master）
* **看得出不是平台 id**

哈希的是**解析后**的路径，所以 `./v.mp4`、`v.mp4`、绝对路径、`file://` URL 四种写法都落进同一任务目录。实测确认。

**不**把文件所在目录当输出根：写在用户视频旁边是意外行为，在只读挂载上直接失败。

#### 来源分类：一个位置参数

两个前端都收一个位置参数，不让用户在 `--url` / `--file` 之间自己分类。规则放在 `JobRequest.from_source`（而不是 CLI），这样 MCP 拿到同一套规则而不是第二份会漂移的副本：

1. `file://` → 本地路径（明确指文件；没有 extractor 认领这个 scheme）
2. 其他任何 `scheme://` → URL。**包括不支持的 scheme**，这样调用者拿到 `UnsupportedPlatformError`（会列出支持的平台），而不是令人困惑的 `file not found: ftp://...`
3. 其余 → 本地路径

**不检查存在性**：那是 producer 的职责，放在那里错误信息才能给出解析后的路径。

#### 输入校验

每一个拒绝都**点名路径**，因为调用者可能是通过网络发来路径的 MCP 客户端。实测：

| 输入 | 消息 | 退出码 |
|---|---|---|
| 不存在的文件 | `local video not found: /tmp/.../nope.mp4` | 1 |
| 扩展名不认识 | `local video has an unrecognised extension '.txt': ...` + 支持的列表 | 1 |
| 目录 | `local video is a directory, not a file: ...` | 1 |
| 空文件 | `local video is empty: ...` | 1 |
| 无法解码 | `local video is not decodable: ...` | 1 |

扩展名用**白名单**（14 种）：没有它，`video.txt` 这种手误会被交给 ffmpeg，报一个关于解码器的错，完全没提真正的问题。校验**不**解码文件——「哪个文件」和「能不能解码」是两件事，解码检查要花一次 ffprobe，属于 metadata 那一步。

#### cover：抽一帧

本地文件没有缩略图可下。类比物是从视频本身抽一帧。位置取**十分之一处、上限 3 秒**：真实视频第一帧常常是黑色淡入，纯黑的封面比没有更糟；上限是因为两小时视频的十分之一意味着 seek 十二分钟。

#### 不接管 sidecar 字幕

视频旁边的 `.srt` 可能是源语言也可能是译文，猜错要么静默跳过 ASR、要么覆盖用户的文件。所以两个都不认领，留给后续版本。

#### 验证

* **真实 ffmpeg 端到端**（`tests/integration/test_burn_pipeline.py` 新增 6 项）：本地文件 → 真实 PREPARE → 真实 BURN，两个成片 ffprobe 可读，**像素级**确认字幕可见（master 0 近白像素，成片 >100）。
* 源文件的**目录和文件名同时含空格、撇号、中文**（`我的 视频目录/It's a Local Test 竖屏.mp4`），并断言撇号确实进了目录名。
* **竖屏**视频正确识别为 1080x1920 / `is_vertical: true`——这正是 v0.1 伪造 1920x1080 会掩盖的情形。
* 实测四种写法（绝对路径、另一 cwd 下的相对路径、`file://`）落进**同一**任务目录并复用 master。
* 重跑复用：master 的 mtime 不变，输出根下只有 1 个任务目录。
* `tests/unit/test_local_video.py` 57 项。其中 fixture 打桩的是 **runner**（真正的边界）而不是 `standardize_master`：第一版打桩了领域函数，结果 cover 事件从断言里消失——那是在测桩，不是在测代码。
* `porter run <path> --only-phase prepare` 真实 CLI 端到端，退出码 0。

### 13.30 `tests/test_pipeline.py`：被取代，不是移植

v0.1 的 4 个测试各有不同答案：

* `test_pipeline_orchestration` —— 断言两个成片「存在」。这正是集成测试在做的事，而且更严：驱动整个 `Pipeline.run`、同时覆盖本地文件来源、并且验证字幕**在像素里可见**。「什么都没烧」也会产出合法、时长正确的视频，所以 v0.1 的断言在这种失败下照样通过。
* `test_cli_doctor` / `test_cli_no_args` / `test_cli_inspect` —— 断言的是 v0.1 的 CLI **文本和退出码**，而 v0.2 刻意改了：诊断走 stderr（stdout 留给 `--json`）、无参数是 argparse 误用（stdout + 退出码 2）、`inspect` 是子命令而非 `--inspect`。移植它们等于断言 v0.2 删掉的行为。替代品在 `tests/unit/test_cli.py`。

### 13.31 缺口更新

| 缺口 | 影响 |
|---|---|
| ~~`local_video` 输入~~ | **已完成**（§13.29） |
| sidecar 字幕 | 本地视频旁边的 `.srt` 不被接管（歧义太大，见 §13.29） |
| `--force` / 单阶段从磁盘恢复 | `force` 对 PREPARE 与 BURN 的复用判定均生效，但「只跑一个阶段」仍需从磁盘恢复中间产物 |
| `porter jobs`（MCP 作业化） | 长任务仍无法通过 MCP 轮询 |
| 免 Key ASR 已死 | 转录必须有 key 或 VideoCaptioner CLI（§13.21） |
| 无字幕视频 | 未处理 |

| — | 1.1 | 新增 §13.29 `local_video` 完成（`LocalPreparer` 端口 + `media/prepare.py` 提取复用 + 身份推导 + 来源分类 + 真实端到端验证）、§13.30 `test_pipeline.py` 判定为被取代、§13.31 缺口更新；`local_video` 从缺口清单移除。 |

### 13.32 两个"测试污染仓库"的缺陷（用 mtime 检测发现）

收尾时用文件 mtime 做残留检查，发现跑一次测试套件会改动仓库根：多出一个名为 `-` 的文件，且 `porter_output` 的 mtime 每次都变。两个都定位并修掉了。

#### 缺陷一：`porter doctor` 会创建输出目录

`doctor/probes.py` 的 `probe_output_dir` 做的是：

```python
target.mkdir(parents=True, exist_ok=True)      # ← 副作用
probe_file = target / ".porter-write-probe"
probe_file.write_text(""); probe_file.unlink()
```

**写文件检查是对的**（docstring 论证充分：POSIX mode bits 不考虑只读挂载、ACL、磁盘满、WSL2 的 `/mnt/c` 转换，这些都会拒绝一个 mode bits 允许的写入）。**但创建目录是错的**：`porter doctor` 是只读体检，却会在用户当前所在的目录里留下一个输出目录。默认 `output_dir` 是相对路径 `./porter_output`，所以后果是「在任意目录跑一次 doctor，就在那里建一个 `porter_output`」。

修法：**永不创建**。目录不存在时，改为探测**最近的已存在祖先**——那正是决定「能否创建出这个目录」的那个目录，所以答案一样，而什么都不动。祖先不是目录（比如路径中间是个文件）时给出明确消息。

实测：在空目录里跑 `porter doctor`，报告 `[OK] Output directory`，目录保持为空。

测试里有一条 `test_it_creates_a_missing_directory` **断言了这个副作用**——把缺陷编码成了契约。改成 `test_it_does_not_create_a_missing_directory`，并且**同时断言仍返回 ok**：只断言「没创建」是不够的，一个拒绝回答的探针也能通过那一半。

#### 缺陷二：fake runner 把 `-` 当成文件路径

`FFmpegRunner` 的 trial encode 命令以 `-f null -` 结尾（丢弃输出）。测试里的 fake runner 普遍这么写：

```python
Path(args[-1]).write_bytes(b"\x00" * 64)
```

于是它在仓库根创建了一个名为 `-` 的文件。`tests/unit/test_fetch_pipeline.py` 里早前已经加了 `if str(target) != "-"` 守卫，另外两处（`test_burn.py`、`test_local_video.py`）没有。三处现在一致。

#### 缺陷三（根因）：MCP 测试依赖 CWD

`tests/unit/test_mcp_server.py` 调用 `porter_doctor` 时不给任何参数，服务端从**工作目录**解析配置，于是问的是仓库根自己的 `porter_output`——而那正是被探针写入的目录（347 MB 的无关历史产物）。

真正的缺陷是**测试不 hermetic**：结果依赖当前工作目录。加一个 autouse fixture `monkeypatch.chdir(tmp_path)`，一行解决，并且让测试表达出它真正的前提。

#### 检查方法

mtime 比对象本身更早暴露问题：

```
before=$(stat -c %Y porter_output); pytest; after=$(stat -c %Y porter_output)
```

跑完确认：仓库根无新增文件、`porter_output` mtime 未变、无新任务目录、`git status` 未变。

| — | 1.2 | 新增 §13.32：`porter doctor` 不再创建输出目录（只读体检不得有副作用）、三处 fake runner 不再把 `-` 当路径、MCP 测试改为 hermetic（autouse `chdir(tmp_path)`）；测试套件跑完对仓库零残留。 |

### 13.33 `porter jobs`：作业注册表与 MCP 作业化

缺口里最后一项影响 MCP 可用性的：MCP tool call 默认超时数十秒到数分钟，而压制 1080p 要数十分钟，所以长任务**不能**是一次阻塞调用。

#### 用户决策：持久化，而不是进程内

CLI 的 `jobs` 命令原本的 docstring 写的是「进程内状态，所以新进程永远看不到任何作业」。那等于做一个**看起来有用、实际永远空**的命令——和 v0.1 的 `get_font_dir()` 死代码同类。用户选了**持久化磁盘注册表**：`~/.cache/porter/jobs.json`（`platformdirs`，已是依赖，零新增）。

#### 两层结构

| 层 | 模块 | 回答的问题 |
|---|---|---|
| **live** | `jobs/store.py` | 「这个进程现在在跑什么」——内存、线程安全、对拥有者是权威 |
| **durable** | `jobs/records.py` | 「跑过什么」——共享 JSON 文件，供另一个终端或重连的 MCP 客户端读取 |

store 把状态**投影**进 registry。反过来（registry 为权威）会让每个进度事件都变成一次带锁的 read-modify-write，而轮询方每几秒才读一次。所以发布按 `PUBLISH_INTERVAL_SECONDS = 2.0` 节流。

`porter.jobs` 从 `jobs.py` 改成包（`store.py` / `records.py` / `__init__.py`），导入路径不变，因为原本没有任何消费者。

#### 顺带补上的分层缺口

`porter.jobs` **原本不在 import-linter 的分层契约里**——也就是说，两个前端都依赖的那个模块，恰恰是唯一不受约束的模块：它可以 import `porter.pipeline` 或 `porter.media` 而没有任何 gate 会说一句话。按真实依赖深度插到 `porter.ports` 与 `porter.context` 之间。**验证方式是故意加一行 `from porter.pipeline import Pipeline`，看 gate 是否报错**——报了。

#### 三个真实缺陷（都不是单元测试能发现的）

**1. 跨进程取消完全不起作用**（设计缺陷）

第一版从**事件 sink** 观察取消：sink 已经存在、每次阶段变化都跑、不需要额外线程。看起来合理。实际在长下载期间**完全无效**——下载一个事件都不产生，而那恰恰是用户最想取消的窗口。一个只断言「CLI 写入了标志」的测试会一直通过。

改成**专用看门狗线程**（每 `CANCEL_POLL_SECONDS = 1.0` 查一次文件）。不用信号：信号要发给可能已被回收的 PID，而且 MCP 服务端拥有的作业和终端拥有的作业会走不同路径。

**2. `Job.to_record()` 不填充 `artifacts`**

只有 `record_from_result` 会填。于是同一个作业，共享文件里有产物路径，而 `porter_job_result`（优先读内存）报 0 个产物——`porter jobs status` 却能列出它们。两个视图对同一事实说法不同，这是最难察觉的一类 bug。现在 `to_record()` 在有 result 时委托给 `record_from_result`。

**3. 裸 `porter jobs` 抛 `AttributeError`**

`jobs_action` 为空时回落到 `list`，但 `list` 子解析器没运行过，`args.show_all` 根本不存在。默认值改到父解析器上显式声明。

#### 另一个测试污染用户目录

`porter run` 现在会注册作业，而 `~/.cache/porter/jobs.json` 是真实路径。两个测试因此写进了用户的 cache，其中一个还 reap 掉了真实运行的记录。在 `tests/conftest.py` 加了 **autouse fixture 重定向 `registry_file` 路径接缝**——让隔离变成结构性保证，而不是每个新测试都要记得的规矩。这和 §13.32 的 `porter_output` 是同一类缺陷，也是同一个教训：**用 mtime 检查残留比用眼睛看代码有效**。

#### 验证

* **真实端到端**（`tests/integration/test_job_cancel.py`，`slow`）：本地转码视频 → 真实 PREPARE，**另一个进程**的真 CLI 发出取消 → 作业在下一个检查点停止，最终状态 `cancelled`。两个进程通过 `XDG_CACHE_HOME` 各自解析到同一个注册表，而不是被喂一个路径——和线上一致。
* **测试有效性已反向验证**：故意让看门狗不应用取消请求，测试立刻失败；恢复后通过。一个从不失败的测试没有价值。
* 另一次完整的 MCP 端到端（`/tmp` 脚本，非提交物）：`porter_job_start` 0.11s 返回、轮询到终态、`porter_job_result` 列出产物、**另一个进程**的 `porter jobs list` 看到同一作业且状态与产物一致、CLI 取消真的停掉了 MCP 服务端的作业。
* 单元测试 72 项（`tests/unit/test_jobs.py`）+ CLI 13 项 + MCP 12 项。

#### 已知语义（有意如此）

取消是**协作式**的，落在**阶段边界**。所以：

* 取消请求可能在状态仍为 `running` 时就被观察到——`porter jobs status` 会区分「已请求」与「已停止」。
* 在**最后一个**请求阶段期间取消，结果是 `done` 而非 `cancelled`：用户要求的工作确实完成了，把已完成的作业报成取消是更糟的错误。
* 长下载中途取消，要等下载结束才停——这是阶段边界的直接后果，不是缺陷。

#### MCP 工具面

`porter_job_start` / `_status` / `_result` / `_cancel` / `_list` + `porter://jobs/{id}/log` 资源。作业用后台线程跑，`_HEAVY_JOBS` 信号量**串行化重活**（一次编码已经打满机器，并发只会让两者都变慢）。`_run_job` 绝不放过异常——线程静默死掉会让作业永远停在 `running`。

| — | 1.10 | 新增 §13.51：一轮六个已知问题。**①** `--translator`/`--llm-model` 是与 `--asr-engine` 完全同类的死字段（两个前端都收下、`src/` 零消费者），修法与之一致（提到链首、其余回退），`llm_model` 抽成 `effective_llm_model()` 因为有两个后端读它；顺带修正两处与实现不符的帮助文本。**②** bing 的失败既不是 §13.40 的回归也不是限流，而是**载荷尺寸上限**——实测 1/2/4/8 条（≤665 字符）可译、**15 条（1259 字符）被 HTTP 200 内的 `statusCode: 400` 拒绝**，故批量降到 8 且被拒时降级为逐句。**③** `_describe_body` 让被丢掉证据的错误消息重新可行动，并在真实运行中立刻兑现。**④** TRANSCRIBE 断点复用（`cooked/.transcribe.json` 记 provenance，无 sidecar 则不复用），真实运行抓出新判定**永远无法满足**（与每次都被重写的 `audio_enhanced.wav` 比较）。**⑤** 429/5xx 重试退避（可取消、尊重 `Retry-After`、上限 8s），逐句路径接同一 helper；反向验证抓出一个**靠换 client 而通过的空洞测试**。**⑥** 新增 `--subtitle-file`（不猜 sidecar，让用户明说），`NO_CUES_HINT` 抽常量——第一版 hint 写在几乎到不了的路径上。**⑦** 清理开发机 jobs.json 里 40 条测试垃圾。全量 **1452 passed**，9 个 seam 反向验证全绿。 |
| — | 1.9 | 新增 §13.48：**本地 Whisper 后端**（`[asr-local]` = MIT 的 `faster-whisper`，无需 PyTorch；链首、无 Key、无网络、`endpoint_verified` 是结构性的），修掉 `--asr-engine` 死字段（只有三个 VideoCaptioner 引擎名有效，而 CONFIG.md 声称会重排链），`porter doctor` 的 ASR 文案随之改正，并记下「装了 ctranslate2→numpy 会让 mypy 在 3.10 下整个中止」的排除过程（有效旋钮只有 `follow_imports=skip` on `openai._extras.*`）。首次真实 CLI 运行又抓到两个真缺陷：**burn 的相对路径**（`cwd=subtitle.parent` 但 argv 未变绝对；全部测试用绝对 `tmp_path` 所以看不见）与**测试污染真实 jobs.json**（模块级 `JobRegistry()` 在导入时冻结了 seam）。真实交付：Instagram 作业 68.2 s 完成，NVENC 烧出双语与纯中文两版，像素级与肉眼双重验证。全量 **1388 passed**。 |
| — | 1.8 | 新增 §13.47：YouTube **自动配音**让 `*-orig` 不再唯一（一个视频每种配音语言各一条，实测 20+ 条），`select_source_lang` 无条件取"第一个"等于取文档顺序——真实英文视频因此取到 `ar-orig`，又因复用自动翻译的 `zh-Hans` 而宣布"不需要翻译"，输出里没有英文。修法为三级裁决（声明的 `language` → `SOURCE_LANG_PRIORITY` → 文档顺序），并修掉**第二个调用点** `_build_metadata`（元数据与计划曾对同一份 info 给出不同答案）。三种反转矩阵 + 一条由反向验证抓出的**空洞测试**（`declared_lang="en-US"` 在未修状态下也通过，改用 `ar` 才真正覆盖）。全量 **1349 passed**。 |
| — | 1.7 | 新增 §13.46：CI 矩阵证伪了一个从 P1 起就存在的声明——`requires-python >=3.10` 是**假的**，`src/porter/config.py` 的模块级 `import tomllib`（3.11+ stdlib）使 3.10 上 `import porter` 直接失败；grep 确认 `tomllib` 是唯一障碍（代码库其余部分刻意做了 `(str, Enum)` 等 3.10 适配），修法为 `tomli as tomllib` 条件导入 + 条件依赖，并在**真的 3.10.21** 上跑完全量套件（1341 passed）。 |
| — | 1.6 | 新增 §13.45：上线后 CI 首次运行暴露的「测试偷偷依赖开发机环境」三类缺陷（测试替身漏了真实文件系统 / 硬编码 `/usr/bin/ffmpeg` / 断言把开发机核数写死）、`uv run` shim PATH 的复现方法与它覆盖不到的 3 个、三处“只改环境取数不改断言”的修法、CI 装 ffmpeg 而非标 `slow` 的依据（`test_burn.py` 的 docstring 指定了真编码层），以及新增的 **hermetic 作业**（先装再藏 ffmpeg 并自证藏成功）。 |
| — | 1.5 | 新增 §13.44：P5 收尾——四份 `docs/` 逐条对照代码复核（含文档比代码更诚实的几处死字段断言）、三个 GitHub Actions 工作流（test 矩阵 / PyPI Trusted Publishing / 三平台 EXE 发布）与 `packaging/launcher.py` 引导器的四个设计细节及本地验证。 |
| — | 1.4 | 新增 §13.43：P5 skill 资产落地（SKILL.md + references + scripts + assets，含文案诚实性测试 34 项）、机制决策「skill 驱动 CLI、MCP 作补充文档」、`porter plan` CLI 命令补齐对称性、以及本轮发现的真 bug（`porter plan` 对不存在的本地文件报可行）与其连带的「三个 plan 测试假绿」修正。 |
| — | 1.3 | 新增 §13.33：持久化作业注册表（`jobs/store.py` + `jobs/records.py`，两层投影）、5 个 MCP 作业工具与日志资源、CLI `jobs` 四个动作、`porter.jobs` 补入分层契约、看门狗式跨进程取消（修掉 sink 观察在长下载期间完全无效的设计缺陷）、`to_record` 产物丢失、conftest 全局隔离用户 cache。 |

### 13.34 缺口更新

| 缺口 | 影响 |
|---|---|
| ~~`local_video` 输入~~ | **已完成**（§13.29） |
| ~~`porter jobs`（MCP 作业化）~~ | **已完成**（§13.33） |
| ~~sidecar 字幕~~ | **已完成**（§13.51）：仍不自动接管（歧义太大，见 §13.29），但新增 `--subtitle-file` 让用户显式点名，歧义被移除而不是靠猜解决 |
| ~~`--force` / 单阶段从磁盘恢复~~ | **已完成**（§13.51）：`--only-phase X` 的前置阶段照跑，但 PREPARE（母版）/ TRANSCRIBE（源 cue，新增 `cooked/.transcribe.json` 记 provenance）/ BURN（成片）各有复用判定。实测第二次运行 `reusing 19 cached source cues`。**TRANSLATE 仍无缓存**（`.ass` 与样式耦合，需要指纹方案） |
| ~~免 Key ASR 已死~~ | **已解决**（§13.48）：`[asr-local]`（MIT 的 `faster-whisper`，无 PyTorch）提供免 Key 免网络的本地识别；`bcut` / `google-web` 仍然实测失效（§13.21） |
| ~~无字幕视频~~ | **已完成**（§13.51）：无平台轨又无可用 ASR 时，失败信息指向 `--subtitle-file` 或 `[asr-local]`（`NO_CUES_HINT`） |
| ~~`porter_plan` / 阶段级 MCP 工具~~ | **已完成**（§13.38、§13.41）：`porter_plan`、`porter_inspect`、`porter_config`、`porter_transcribe` / `_translate` / `_burn`，工具面 13 个 |
| ~~§8.2 资源与提示~~ | **已完成**（§13.42）：`porter://docs/architecture`、`porter://config` 与 `localize-video` 提示 |
| MCP sampling 翻译 | §8.3 的零 Key LLM 级翻译（用宿主模型）未实现 |
| TRANSLATE 无缓存 | `.ass` 与样式耦合，需指纹方案；重复运行会重新翻译（§13.51） |
| PREPARE 元数据 / 缩略图不复用 | 实测占第二次运行的 ~55s（元数据抓取 ~40s + 缩略图超时 ~15s），已超过 ASR 成为主要开销（§13.51） |

**§13.34 当时列为“发布前必须先处理”的两项（`--force` 单阶段恢复与免 Key ASR 现状）已在 §13.48 / §13.51 完成。**当前剩下的都是功能增量，其中最值得做的是上表最后一行。

### 13.35 `porter_inspect`：MCP 预检工具

§8.1 规划的 `porter_inspect`。引擎侧 `inspect_url` 早已实现并有测试，缺的只是 MCP 形状——而形状本身要做三个决定，CLI 不需要：

**1. 坏链接是一次成功的调用。** `ok` 描述**调用**，`is_valid` 描述**链接**。把 404 报成 `ok: false` 等于告诉 agent 工具坏了，而它最可能的反应——重试——恰恰是错的。两个字段都返回，因为它们回答不同问题。

**2. 不提供任何 cookie 参数。** CLI 有 `--cookies` / `--cookies-from-browser`；放到 MCP 上，这些值会被写进对话记录以及随后的遥测里（§8.5）。通过 CLI 配置一次的 cookie 在这里从解析后的 config 读取，所以需要认证的链接照样能查——只是**不能从 tool call 里认证**。

**3. 并发上限。** 对本机便宜，对远端不便宜：agent 扇出五十个链接不该开五十个 socket，而每次尝试还可能重试。同时 4 个（§8.4 的 LIGHT）。

**参数名用 `source` 而非 §8.1 写的 `url`**，与 `porter_job_start` 一致——学过其中一个工具的 agent 不该为同一输入再学一个名字。传本地路径时返回**解释**而不是误导性的"unsupported platform"：本地文件不需要预检，`porter_job_start` 会直接校验并给出具体错误。

**纠正一处不实说法。** 从 v0.1 继承的"in about a second"是错的：实测真实 YouTube 探测 **8–10 秒**（yt-dlp 取元数据）。已改为"a few seconds"，CLI 与工具描述同步更正。

**验证**：真实网络五种输入全部正确——真实 URL（3840x2160、有字幕、213s）、`youtu.be` 短链带 tracking 参数（正确规范化）、死链（`ok=true, is_valid=false`，立即返回不重试）、不支持的主机（0.0s）、本地路径（清晰解释）。14 项单元测试；并发上限测试**反向验证有效**：去掉信号量后 peak=8 失败，恢复后 ≤4 通过。

### 13.36 审计发现：bilibili 字幕轨整条链路是断的

做 `porter_inspect`/`porter_plan` 时为了诚实预测"走原生轨还是 ASR"去读真实判定逻辑，发现了一个**未被任何文档记录**的缺陷。

#### 事实链

1. `bilibili.py` 声明 `SubtitleSource(remote=False, prefer_existing_chinese=True, as_source=True)`，`notes` 写"CC subtitles are fetched over HTTP, not via yt-dlp"。
2. `_plan_subtitles` 第一行就是 `if not self.spec.subtitles.remote: return {}` → **bilibili 永远返回空计划**。
3. `_download_subtitles` 拿到空计划直接 `return {}` → **从不抓取任何字幕**。
4. 即使抓到了，`_first_existing(scratch, ("sub.srt", "sub.vtt"))` **只看 srt/vtt，不看 json**。
5. `bilibili_json_to_srt`——正是为 bilibili 的 JSON CC 格式写的转换器——**已移植、已导出、有 5 个回归测试，但没有任何生产代码调用它**（只有它自己的测试调用）。
6. `as_source` 字段**在 v0.2 源码里从未被读取**，计划文档也从未提过它；它是 v0.2 自己发明的。对 tiktok（`remote=True, as_source=False`）它还做了**虚假声明**：链会照常采用抓到的轨，而字段说不该采用。

#### 影响

**bilibili 作业永远走 ASR**，丢弃平台自己已有的精确中文 CC 轨——正确拼写、正确标点、正确专有名词、免费。这是**相对 v0.1 的质量回退**：v0.1 的 bilibili 设了 `writesubtitles: True, subtitleslangs: ["all"]`，是真的抓的（JSON 转换器就是为那条路准备的）。

#### 为什么一直没被发现

每一块单独看都正常：转换器**有测试**，spec **有注释**，字段**有 docstring**。缺的是"谁调用它"。这是比 v0.1 `get_font_dir()` 更隐蔽的一类死代码——**一个带测试的函数和一个带文档的字段，两者都是惰性的**。

#### 两个修法

| 方案 | 内容 | 代价 |
|---|---|---|
| **A（v0.1 忠实）** | bilibili 改 `remote=True`，`_download_subtitles` 认识 `sub.json` 并调用 `bilibili_json_to_srt` | 小，~30 行，恢复 v0.1 行为 |
| **B（按 spec 注释）** | 实现 HTTP CC 抓取（需 WBI 签名 / 412 反爬处理） | 大，且 spec 注释本身可能是对 v0.1 行为的**错误描述**（v0.1 其实走 yt-dlp） |

另需决定 `as_source`：删除（无 v0.1 依据、无消费者、且对 tiktok 说假话）还是接上（会让 tiktok 行为偏离 v0.1）。

### 13.37 修复 bilibili 字幕链路（方案 A）

§13.36 的缺陷，按用户选择的**方案 A（v0.1 忠实）**修复。

#### 先做了经验验证，因为方案 A 有个前提

spec 注释声称"bilibili 的 CC 轨不暴露给 yt-dlp"。**这个说法经实测是对的**：

```
BV1GJ411x7h7   subtitles=[]   automatic_captions=[]
BV1xx411c7mD   subtitles=[]   automatic_captions=[]
av170001       subtitles=[]   automatic_captions=[]
```

所以方案 B（实现 HTTP 抓取）的方向是错的——但**这不意味着方案 A 没价值**。关键在于 `remote=False` 的语义：

> `remote=False` 不是"换个方式抓"，而是"**永远不抓**"——`plan_subtitles` 直接返回空计划。

因此 v0.2 的 `remote=False` 保证了 bilibili **即便配了 cookie 也永远用不上自己的轨**。而 v0.1 设的是 `writesubtitles: True, subtitleslangs: ["all"]`——等价于 `remote=True`。方案 A 就是恢复这个。

#### 三处改动（缺一处都不成立）

| # | 文件 | 改动 | 不做的后果 |
|---|---|---|---|
| 1 | `bilibili.py` | `remote=False` → `True` | 计划永远为空，从不抓取 |
| 2 | `base.py` `_download_subtitles` | 搜索 `sub.json` 并调 `bilibili_json_to_srt` | 抓到了也被跳过（只看 srt/vtt） |
| 3 | `ydl.py` `subtitle_format` | `"srt/vtt/best"` → `"srt/vtt/json/best"` | 请求的是平台没有的容器，轨仍被跳过 |

第 3 处是原分析漏掉的：即使 1 和 2 都修好，只要不**显式请求 json**，bilibili 的 JSON-only 轨依然拿不到。v0.1 的 `subtitlesformat` 就是 `"srt/vtt/json/best"`。

另外把 JSON 转换失败（`{"body": []}`、畸形 JSON）处理成**不写文件**并落到 ASR。一个 0 字节的 `subtitle.srt` 比没有更糟：它让 PREPARE 看起来成功，而 ASR 链的"平台轨存在"分支随后产出一份没有任何 cue 的转录。

#### 同时删除 `as_source`

该字段在 v0.2 源码里**从未被读取**，计划文档也从未提过（v0.2 自造），且对 tiktok（`remote=True, as_source=False`）是**虚假声明**——链会照常采用抓到的轨。已从 `SubtitleSource` 及 5 个平台声明中移除。`prefer_existing_chinese` 保留（它确实被 `_plan_subtitles` 消费）。

#### 两处不实注释已更正

* `bilibili.py` 模块 docstring 与 spec `notes` 都声称"CC 轨走 HTTP 抓取"——改为陈述实测结果与 JSON 容器的事实。
* `spec.py` 的 `remote` docstring 现在明确写出"`False` 意味着**完全不抓**"，因为把它误读成"换个方式抓"正是这个缺陷的成因。

#### 验证

* 新增 5 项测试，**逐一反向验证有效**：把三处修复全部回退，恰好 4 项失败（`bilibili_plans_a_track`、`the_requested_format_includes_json`、`a_bilibili_json_track_is_converted`、`bilibili_requests_its_subtitle_track`）；恢复后全绿。
* 第 5 项（空 JSON 不写文件）在回退下仍通过——它守的是另一种失败模式（写出空文件），本就独立。
* 真实 bilibili 匿名探测确认**无副作用**：`plan={}`，与修复前一致。
* 计数 1192 → **1196 passed**。

#### 未验证的部分（诚实标注）

**配了 cookie 之后 yt-dlp 是否真能拿到 bilibili 的 CC 轨，未经验证**（本机没有 bilibili 账号）。这次修复的作用是"把路打开"：有轨就抓、能转就转、没有就照旧走 ASR。若加了 cookie 仍拿不到，那说明需要方案 B，届时再做——但至少不再是"代码里根本没有这条路"。

### 13.38 `porter_plan`：解析后的执行计划

§8.1 规划的 `porter_plan`，MCP 工具面里最有价值的一个——它让 agent 在**投入算力之前**知道这个作业会做什么、以及会不会成。

#### 派生，而非重写

计划是从**真正会干活的那些对象**上读出来的：

* `Pipeline.default(ctx)` —— 与运行时**同一个构造器**，不是照抄一份装配逻辑；
* `phases_for(request)` —— 运行本身迭代的那个方法（原 `_phases_for`，已公开化）；
* `YtDlpExtractor.plan_subtitles(info)` —— PREPARE 决定抓哪些轨时调用的那个方法（原 `_plan_subtitles`，已公开化）。

外加两条链新增的 `availability(ctx)`：返回每个 backend 的名字与探测结果，**复用链自己的 `_probe` 守卫**。

**为什么这点比看起来重要**：一个描述了"没人会跑的那条流水线"的计划，比没有计划更糟——agent 会照着它行动。

#### 为什么不能靠 `has_subtitles` 猜

诱人的实现是读 `InspectionResult.has_subtitles` 就完事。但那个标志是**平台列表**，不是路由决定：

* 五个平台里**三个**标了 `remote=False`（完全不抓轨，无论平台列出多少）；
* 选哪个语言由**每平台的优先级表**决定；
* 平台自带的**中文**轨会整个省掉翻译阶段。

每一项都是已存在的真实逻辑，计划直接调用它。测试里专门有一条 `test_the_route_comes_from_the_platform_not_from_has_subtitles` 钉住这点：给 instagram 一个"声称有字幕"的 info dict，计划必须仍然报 `route=asr`。

#### 诚实性：`available` 不足以回答"能不能成"

第一次真实运行时暴露：计划对 bilibili 报 `feasible=True`，而 `available=True` 的 `bcut` / `google-web` 正是先前实测**返回空结果**的两个端点。计划的核心承诺就是"能不能成"，报错这个就失去意义。

所以给两个协议加了 `endpoint_verified`，**由各 backend 自己声明**（不是计划模块里的一张表——那会是第二个真相来源，第一次有人验证后端时就会漂移）：

| 已验证 | 未验证（逆向工程） |
|---|---|
| `whisper-api`、`videocaptioner`、`llm`、`mymemory` | `bcut`、`google-web`、`bing`、`google` |

当 ASR 路线**只**依赖未验证端点时，计划加一条 note（不是 blocking——"未验证"不等于"坏了"，换个网络或过一周可能就好了）。

#### 没有时间估算，这是有意的

§8.1 要"预估耗时"，诚实的回答是本模块给不出。可信的数字需要**本机 × 本分辨率 × 本编码器**的实测编码速率，而唯一知道它的是试编码（`porter doctor` 在跑）。所以计划报告**驱动成本的实测输入**（时长、分辨率、跑哪些阶段），并说明其余由什么决定。一个编造的"大约 12 分钟"会被当真，然后在第一个不寻常的视频上出错。

#### 顺带修掉的两个问题

**1. `plan_for(source, options, ctx)` 静默忽略 `options`。** 同时传 `options` 和 `ctx` 时，`options` 被丢掉——**我在上一层重新引入了之前刚在 pipeline 里修掉的"两个 options 对象"陷阱**。调用者问 `--burn skip` 会得到一份完整运行的描述。现在显式 `options` 优先，与 pipeline 的"request 权威"规则一致。**这个 bug 是被测试抓到的**（`test_the_phase_list_honours_burn`）。

**2. §8.4 的并发上限原本是两个各自为政的信号量。** 抽出 `porter_mcp/limits.py`（`HEAVY=1`、`LIGHT=4`），三个工具共用，并加测试断言它们指向同一个对象。用 `threading.Semaphore` 而非 `asyncio.Semaphore`：被守护的工作是同步的（yt-dlp、ffmpeg 阻塞线程），FastMCP 在 worker 线程里跑同步 tool body——asyncio 信号量会在 tool body 阻塞的瞬间被**另一个 task** 释放。

#### 验证

* 真实网络四类输入：YouTube（`route=platform`、双轨、`asr_runs=False`、`translation.needed=False`——预测这个作业**既不需识别也不需翻译**）、bilibili（`route=asr` + 未验证端点警告）、本地文件（`route=asr` + sidecar 说明）、死链（`feasible=False`）。
* 19 项引擎测试 + 11 项 MCP 测试。
* `porter.plan` 已补入 import-linter 分层契约（位置一度放反，契约立刻报 `porter.plan -> porter.pipeline` broken——**证明它真的受约束**）。

### 13.39 ⚠️ 严重缺陷：所有平台字幕抓取从未生效

做 `porter_plan` 的端到端验证时，计划预测一个 YouTube 作业"不需要 ASR"，而作业在 153 秒后死于 `every speech-to-text backend failed`。顺着查下去发现了**比 §13.37 更根本的缺陷**。

#### 事实链

`_download_subtitles` 用 `outtmpl="sub.%(ext)s"`，然后：

```python
found = _first_existing(scratch, ("sub.srt", "sub.vtt", "sub.json"))
```

但 **yt-dlp 会把语言标签插进文件名**。实测：

```
[info] Writing video subtitles to: sub.zh-Hans.srt
$ ls /tmp/subdl_out/  →  sub.en.srt
```

所以它**每次都找不到文件**——在所有有字幕轨的平台上。v0.1 的 `_save_sub` 用的是 `temp_dir.glob(f"download*.{lang_tag}.*")`，**带语言标签做 glob**，是好的。

#### 为什么从未被发现

三层遮蔽，缺一不可：

1. **缺失字幕轨是"尽力而为"的设计**——`_download_subtitles` 记一条 warning 然后继续，因为 ASR 本该接手。所以失败是一条没人读的日志行。
2. **单元测试的假 yt-dlp 写的是 `sub.srt`**——正是生产代码要找的名字。**测试替身比它替代的东西更宽容**，于是两边一致地错着，测试全绿。
3. §13.37 那次修复的验证也不足以发现它：bilibili 的匿名路径本来就抓不到轨，`plan={}` 与修复前一致，看起来"无副作用"。

#### 修复

`_find_subtitle_file(directory)` 改为 `glob("sub.*")`，保留格式偏好（srt > vtt > json）并跳过空文件。**同时把测试的假 yt-dlp 改成写真实的名字**（`sub.{lang}.{ext}`）——这是关键一步，因为不改它，测试永远无法发现这个 bug。

#### 验证（这次是反向验证）

* 假 yt-dlp 改成忠实命名后，**回退修复 → 4 项测试失败**；恢复 → 全绿。
* 新增 7 项 `_find_subtitle_file` 直接测试（语言标签、连字符标签 `zh-Hans`、格式偏好、空文件不算轨、媒体文件不被误认）。
* **真实端到端**：修复前作业死于 TRANSCRIBE；修复后 `raw/subtitle.srt` 拿到 273 行平台自己的英文轨，TRANSCRIBE 通过，作业推进到 TRANSLATE。

#### 仍未完成的部分（诚实标注）

那次真实运行最终仍失败，但原因是**环境性的**：Google 与 MyMemory 翻译都返回 **HTTP 429**（我自己反复探测把它们打限流了——几分钟前的独立探测两者都正常），YouTube 的中文自动字幕下载也是 429。`bing` 另有真实脆弱性：`bing batch response was not recognised`。

计划现在明确写出这个区别：`tracks` 是**将被请求**的轨，不是**保证拿到**的轨，并说明抓取失败会回落到 ASR、平台会对这类抓取限流（429）。计划不可能知道某次网络请求会不会成功，也不该暗示它可以。

### 13.40 `bing` 批量翻译：分隔符被翻译器改写，且失败方式选错了

上一轮真实运行里 `bing` 报 `bing batch response was not recognised`。查下去是两个独立问题叠加。

#### 事实

`_DELIMITER` 是 `"\n\n"`：批量请求把多段台词用**空行**连接，再把响应用空行切回来。实测：

```
发送: "hello world\n\ngood morning"
收到: "你好，世界\n早上好"      ← 空行被塌陷成一个 \n
按 "\n\n" 切 → 1 段（期望 2）→ 抛错
```

响应里还有 `"usedLLM": true`——**Bing 现在用模型翻译**，它是在重写整块文本，段落结构不是它会保留的东西。

但真正的设计错误是**失败方式**：段数不匹配时它 `raise TranslationBackendError`，于是整个 backend 判失败，台词交给链上的 `google`。**为了一个被翻译器改写的分隔符，放弃一个逐句翻译完全正常的服务。** 批量是优化，不该是契约。

#### 修复（两半，缺一不可）

1. **分隔符换成 `"\n[[|]]\n"`**。实测：`\n\n` 会塌陷，`[[|]]` 三次 15 段批量全部原样返回 15 段。它是个翻译器没有理由去动的 token。
2. **段数不匹配 → 降级为逐句请求**（复用已存在的 `_translate_each`），不再抛错。

第二半才是关键：**分隔符仍然是对别人文本处理器的猜测**，而实测显示 Bing 的行为是**间歇性的**——同一组输入这次保留空行、下次塌陷，连空格分隔符都保留过。所以计数检查必须在，且失配必须降级而非失败。

#### 验证

* **单元测试 5 项**（`TestBingBatching`）：分隔符不是裸空行（钉住回归）、批量载荷含分隔符、**塌陷后降级为逐句且长度保持**、空台词仍走逐句路径。
* **真实服务**：15 段批量 → 15 段返回，长度保持，翻译正确（`我们对爱情并不陌生` / `你知道规则，我也一样`…）。
* 降级路径由单元测试证明（真实服务太不一致，无法稳定复现塌陷——这本身就是加计数检查的理由）。

#### 附带发现

`bing` 的 `endpoint_verified` 仍是 `False`（逆向工程）。它现在能工作了，但那是**这次**能工作；`available()` 依旧只探测 `requests` 能否导入，对这个端点说不出一句话。这正是 §13.38 加 `endpoint_verified` 的理由。

---

### 13.41 阶段级工具与只读配置：`porter_translate` / `porter_burn` / `porter_transcribe` / `porter_config`

MCP 工具面从 9 个补到 **13 个**，§8.1 的表里只剩采样式 `porter_run` 未做。四个新工具分两类：**产物级阶段工具**（吃文件、吐文件）与**只读配置**。

#### 阶段工具的设计前提：接受的输入必须"很快能做完"

§8.1 明确写了 job API 才是可靠路径，理由是 **MCP 工具调用有超时**（一两分钟），而 1080p 压制要几十分钟。所以这三个工具遵循同一条规则——**凡是被接受的输入，其工作都能快速结束**：

| 工具 | 接受的输入 | 为什么可以阻塞 |
|---|---|---|
| `porter_translate` | 本地 `.srt` | 无下载、无识别，只有网络往返 |
| `porter_burn` | 本地视频 + 本地 `.ass` | 只有 ffmpeg |
| `porter_transcribe` | **本地**媒体文件 | 无下载 |

`porter_transcribe` 对 **URL 明确拒绝**并指向 `porter_job_start(only_phase="transcribe")`。§8.1 把输入写成 `audio|url`；`url` 那一半移交给了 job API，理由是"先抓 URL"就是一次下载，而下载正是 job API 存在的意义。**拒绝并给出可执行的下一步，好过接受然后超时。**

三者都不重新推导任何东西：翻译链来自 `Pipeline.default(ctx)`，编码器判定来自压制阶段用的同一个 `EncoderSelector`——**阶段工具与完整运行不可能对"选了哪个后端/编码器"产生分歧**（与 §13.38 `porter_plan` 同一条原则）。

#### `porter_config`：唯一的纯只读工具

两个动作，都是读：

* `list` → 段落名、`source`、用户级/项目级配置文件路径、`output_dir`
* `get` → 掩码后的配置（可用 `section` 收窄到一段）

两件**故意做不到**的事：

1. **没有写动作**。测试钉住 `input_schema.properties == {"action", "section"}`——出现 `value`/`api_key` 参数就是 §8.5 被静默破坏（参数会开始把密钥收进对话记录）。
2. **不自己实现掩码**。用的是引擎的 `PorterConfig.masked()`。第二套掩码逻辑就是第二个出错的地方，而出错的那一套就是泄漏的那一套。

#### 实测验证（真实 MCP 客户端）

| 调用 | 结果 |
|---|---|
| `porter_config list` | `ok=true`，sections `[llm, asr, ffmpeg, style]`，密钥显示 `<not set>` |
| `porter_config get(section=llm)` | `ok=true`，只返回 llm 段，不夹带 `output_dir` |
| `porter_config action=set` | `ok=false`，`unknown action: 'set'` |
| `porter_translate`（真实 3 条 SRT） | `ok=true`，中文输出正确 |
| `porter_burn`（把上一步生成的 ASS 压回视频） | `ok=true`，**encoder=`h264_nvenc`**（硬件） |
| `porter_transcribe`（本地 mp4） | `ok=false`，`every speech-to-text backend failed`（ASR 现状，诚实报错） |
| `porter_transcribe`（URL） | `ok=false` + 指向 job API 的 `hint` |

**像素级验证压制**：纯黑视频底部条带 `最大灰度=0`（1 种取值），烧录后 `最大灰度=255`（218 种取值，非黑像素 3.20%）。字幕**真的进了像素**，不是"文件存在"。

**反向验证**（证明断言会咬）：把 `burn_hardsub` 换成 `shutil.copy`（正是 v0.1"把截断编码当成品发布"的失败模式）→ 工具仍报 `ok=true` 且文件存在，但像素断言**失败**。测试抓得住假烧录。

#### 本次发现的两个真实缺陷

**1. `porter_burn` 显式 `output` 的父目录不存在时会崩**

ffmpeg 在目标旁写临时文件再改名，所以父目录必须已存在。管线在 `render_release` 里做了 `mkdir`，独立压制没做 → 抛出裸 `OSError`（工具层崩溃，不是数据错误）。已修复：建父目录，并把 `OSError` 映射为 `{"ok": false, "error": ...}`。**由测试发现**（`test_an_explicit_output_wins`）。

**2. 掩码测试原本是空转的（假绿）**

初版测试设 `PORTER_LLM_API_KEY`，但引擎读的是 **`OPENAI_API_KEY`**（`config._first_env`）。变量没被读到 → `api_key` 是空 → `secret not in rendered` 与 `api_key != secret` **两条断言都因为"什么都没设"而成立**。反向验证（删掉 `masked()` 的掩码）本该失败却**通过**，暴露了空转。

修复：改用 `OPENAI_API_KEY`，并加一条前置断言 `assert data["config"]["api_key"]`（"密钥根本没进配置，这个测试什么也没证明"）。再跑反向验证 → **FAILED**，测试这才真正咬住。

**教训**：反向验证不只是"证明测试有效"，它是**唯一能发现"测试因为错误的原因通过"的手段**。这条和 §13.39 的"假 yt-dlp 比真的更宽容"是同一类病——测试替身/夹具与被测代码**一致地错**，于是双双绿灯。

#### 已知限制（诚实记录，未修）

`porter_translate` 对**已经是句级切分的 SRT** 仍会合并相邻短句：实测 3 条输入 → 2 条输出，把两句无关歌词（`You know the rules...` + `Never gonna give you up`）并成一条（合并后的 cue 时间跨度正确覆盖两条原 cue，所以**行为正确但边界变了**）。合并是句子级翻译为 ASR 滚动碎片设计的，对已切分好的 SRT 属于过度合并。

处理方式是**如实上报**而非掩盖：返回 `input_cue_count` / `cue_count` / `cues_merged` 与一条说明。真正要修需要给链加 `merge_shards` 开关——那是全库最精调的部分（6 个切分条件、4 组守卫），不宜在本轮末尾仓促改动。

#### 测试与门禁

`tests/unit/test_mcp_stages.py` 新增 38 项（契约层钉住"暴露了什么/拒绝什么"，行为层 stub 掉 `Pipeline.default` 与 `burn_hardsub`，末尾 2 项 `slow` 真实集成）。

* ruff / mypy（92 文件）/ import-linter（2 kept, 0 broken）全绿
* **1283 passed**（1245 → 1283），快速回路 1249 passed / 34 deselected

---

### 13.42 §8.2 资源与提示：`porter://docs/architecture`、`porter://config`、`localize-video`

§8.2 的契约到此补齐。落地在 `src/porter_mcp/tools/docs.py`（`_MODULES` 变成 8 个模块），资源面变成 3 个静态资源 + 1 个模板，另有 1 个提示。

#### 核心设计：什么该派生，什么只能手写

散文（**为什么**管线这样设计）只能手写——没有东西可供派生。但本模块里**每一张表**（阶段、平台、后端及其"端点是否验证过"）都在调用时从引擎读出：阶段来自 `Phase` 枚举，平台来自 `registry().handlers()` 的 spec，后端来自 `Pipeline.default(ctx)` 实际装配出的链的 `.backends`。

理由与 §13.38 `porter_plan` 同源：**手工维护的后端清单，在有人加后端的那一刻就是错的**。这不是理论担忧——`porter_plan` 和本模块先后都因此受益（bilibili 的 `prefer_existing_chinese` 修复自动反映进了平台表）。

**故意不含"实时可用性"。** 文档报告**存在哪些**后端、以及它们的线格式**是否曾被验证**，但**不做探测**。探测是 `porter_doctor` 与 `porter_plan` 的职责；一个"客户端为了渲染文档而拉取"的资源若偷偷开网络连接，就是在只读操作里塞副作用。有测试钉住这一点（把两个链模块的 `_probe` 换成抛异常的桩，文档仍须正常返回）。

#### `porter://config` 与 `porter_config` 工具的关系

两者返回同一份数据，且**用的是引擎的同一个 `PorterConfig.masked()`**——不是各自实现一遍掩码。有一条测试断言**两者结果相等**：同一件事有两个出口时，"它们是否一致"应当是断言，而不是假设。

#### `localize-video` 注册为 MCP prompt，而非该 URI 的资源

§8.2 写作 `porter://prompts/localize-video`。实现为名为 `localize-video` 的 **prompt**：prompt 才是客户端会渲染成可复用命令的原语，而"引导 agent 走闭环"正是这个意思；做成资源则需被手工拉取并重读。提示接受可选 `source` 参数，会把链接前置进正文。

提示内容不只是步骤清单，它把本轮踩过的坑写进去了：平台字幕轨是**"requested, not guaranteed"**（§13.38 那次 429 真的毁掉了一个作业）、`blocking_issues` 为真时应当**停下**而不是开作业、以及**为什么必须轮询**（工具调用有超时，而压制要几十分钟）。

#### 验证

* **真实资源读取**：架构文档正确渲染（含派生的平台表——bilibili 显示 "Chinese reused when present"，正是 §13.37 修复的反映——与 10 个后端及其 verified 列）；`porter://config` 密钥显示 `sk-...cdef`；提示在有/无 `source` 两种情况下都正确。
* **22 项测试**（`tests/unit/test_mcp_docs.py`），其中**两条是派生保证**：平台表必须与 `registry()` 完全一致，后端表必须与实际装配的链完全一致。
* **反向验证**：把后端表改成"手写漏更新"的旧表 → 派生测试 **FAILED**；让平台表只取前 3 个 → **FAILED**。测试确实咬得住漂移。

#### 门禁

ruff / mypy（93 文件）/ import-linter（2 kept, 0 broken）全绿；**1305 passed**（1283 → 1305），快速回路 1271 passed / 34 deselected。

---

### 13.43 P5 资产：SKILL.md、skill references 与 `porter plan` CLI 命令

P5 的第 12 项（`skills/porter-skill/`）落地，同时补掉一个此前没注意到的**对称性缺口**：`porter_plan` 只在 MCP 侧存在，CLI 路径没有预检。

#### 机制决策：skill 驱动 CLI，MCP 作为补充文档

用户提出的问题是 SKILL.md 该走 CLI 还是 MCP。结论是 **CLI 必须是基线**，理由是硬性的：

* skill 是**给 agent 的指令**，它自己不能调用 MCP 工具；`npx skills add` 只往 skill 目录写文件，**不会安装 MCP server**。若 SKILL.md 以 MCP 为主路径，全新安装的用户会读到「调用 `porter_inspect`」却找不到该工具。
* MCP 工具**自带描述与 schema**，是自解释的；skill 的增量价值是领域知识（四阶段、回退链、质检步骤、前置条件），这部分与机制无关。
* 但当 MCP **已配置**时它严格更好（无 shell 引号/编码风险、原生结构化结果、资源面），且 §8.3 的宿主模型采样式翻译**只有 MCP 能做**——CLI 进程拿不到 agent 的模型。

所以：SKILL.md 写 CLI 命令形式，另加一句指向 `references/MCP.md`（渐进式披露，只在需要时加载）。用户选定此方案。

#### 落地的文件

| 文件 | 行数 | 内容 |
|---|---|---|
| `skills/porter-skill/SKILL.md` | 235 | 英文 frontmatter + 中文正文；inspect → plan → 用户确认 → run → 轮询 → 质检 |
| `skills/porter-skill/README.md` | 75 | 目录说明；记录「skill 驱动 CLI 而非 MCP」的原因 |
| `skills/porter-skill/references/ARCHITECTURE.md` | 147 | 四阶段、`--only-phase` 语义、两条字幕路线、后端链与 verified 标记 |
| `skills/porter-skill/references/CONFIG.md` | 154 | 配置搜索顺序、全部键与默认值 |
| `skills/porter-skill/references/MCP.md` | 120 | CLI ↔ MCP 对照表、setup、安全约束 |
| `skills/porter-skill/assets/config.example.json` | — | v0.2 键模板（含 `_comment` 说明） |
| `skills/porter-skill/scripts/{porter,inspect,bootstrap}.sh` | — | `exec uvx --from "porter-workflow[all]" porter "$@"` 等 |

frontmatter 经实测校验：`name` 长度 12（≤64）且等于父目录名，`description` 527（≤1024），`compatibility` 310（≤500），正文 214 行。三个脚本 `bash -n` 全过；全仓库只有一个 `SKILL.md`。

#### 长作业：轮询而非抬高 bash 超时

v0.1 的 SKILL.md 让 agent 把 bash `timeout` 抬到 1200 秒。v0.2 改为**后台 `porter run` + `porter jobs status` 轮询**——这正是作业注册表（§13.33）存在的理由，也让 skill 与 MCP 的长作业处理方式一致。

#### `porter plan`：补上 CLI 侧缺失的预检

`porter_plan` 此前只有 MCP 实现（§13.38）。CLI 路径若没有它，agent 用 shell 驱动 porter 时只能「先开作业才发现走不通」。新增 `src/porter_cli/commands/plan.py`（与 MCP 工具共用同一个 `porter.plan.plan_for`，因此两者不可能给出不同预测），支持 `--burn` / `--target-lang` / `--only-phase` / `--json`，退出码与 `inspect` 一致（可行 0 / 不可行 1）。

#### 本轮发现的真 bug：`porter plan` 对不存在的本地文件报「可行」

`porter plan /nope/gone.mp4` 输出 `Feasible: yes` 且退出码 0。`porter/plan.py` 的 `_local_plan` 从不检查文件是否存在——本地路径分支只做 ASR 引擎判定。已修：文件存在性作为**第一条** blocking issue（`no such file: <path>`），理由是可执行性——对一个不存在的路径讲 ASR 后端是噪音。

修的过程中 `mypy` 报 `src/porter/plan.py:142: Item "None" of "Path | None" has no attribute "is_file"`，因为 `request.local_video` 是 `Path | None`。改法是把路径显式传入（`_local_plan(..., path: Path, ...)`），而不是在函数内 `assert`。

同一轮里 `src/porter_cli/commands/plan.py` 的 `_format_plan` 也被 ruff 的 `S101`（`assert isinstance(plan, Plan)` 这个类型收窄手法）拦下，改成模块级 import `Plan` 并直接标注参数类型——顺带确认了 CLI 命令模块**允许**模块级引擎 import（`run.py`、`jobs.py`、`doctor.py` 都这么做）。

#### 连带修正：三个 plan 测试原本是「假绿」

`plan_for` 现在会先判文件存在，于是 `tests/unit/test_plan.py` 里用 `/videos/a.mp4` 的三个本地测试全部撞上新的第一条件，**不再覆盖它们声称覆盖的 ASR 判定**——其中 `test_no_available_engine_makes_it_infeasible` 直接失败，另两个则静默变成空转。加了 `local_media` fixture（`tmp_path/clip.mp4`）让它们回到真实路径，并补两条反向测试：`test_a_missing_local_file_is_infeasible`（文件检查必须压过 ASR 检查）与 `test_a_real_file_with_an_engine_is_feasible`（文件检查不得误杀真实文件）。反向验证：还原修复后新测试 **FAILED**。

#### 测试与门禁

新增 `tests/unit/test_skill_assets.py` 34 项，覆盖：skill 布局（全仓库唯一 `SKILL.md`、目录名等于 frontmatter `name`、脚本可执行且 `bash -n` 通过）、frontmatter 上限、**文案诚实性**（不得出现 v0.1 的「零 Key」说法、必须写明转录需要 Key、翻译不需要、平台轨是 requested 而非 guaranteed、要求轮询而非抬高超时、要求质检）、MCP 参考文档覆盖每个已暴露工具、`config.example.json` 与 pydantic 模型一致、以及 `porter plan` 的行为。

* ruff / mypy（94 文件）/ import-linter（2 kept, 0 broken）全绿
* **1341 passed**（1305 → 1341），快速回路 1307 passed / 34 deselected

---

### 13.44 P5 收尾：四份 `docs/` 与三个 GitHub Actions 工作流

#### 四份文档：生成它们的子代理崩了，所以逐条对照代码复核

四份文档由子代理生成，但那些子代理在写出文件后以 provider error 终止，所以「文件存在」推不出「内容正确」。复核方式只能是对照代码：

| 文档 | 行数 | 复核的关键断言 | 结论 |
|---|---|---|---|
| `docs/ARCHITECTURE.md` | 550 | §2 分层列表逐行比对 `pyproject.toml` 的 layers 契约（12 层，含 `porter.plan` / `porter.doctor` / `porter.jobs`）；§1 惰性导出；§6 作业注册表；§7 媒体层；§8 字幕路线；§10 缺口表 | 一致 |
| `docs/CONFIG.md` | 342 | §1/§4 逐字段比对 `config.py` 五个模型（含 `SubtitleStyleConfig` 里 `zh_primary_color` / `en_primary_color` / `outline_color` / `outline_width` / `shadow` / `fade_in_ms` / `fade_out_ms` 七个计划文档未列的字段）；§3 逐条比对 `_first_env` 调用点 | 一致 |
| `docs/MCP.md` | 274 | §2 逐个核对 13 个工具名与参数；§3 资源（3 静态 + 1 模板 + 1 prompt）；§4.3 「无信号处理」 | 一致 |
| `docs/MIGRATION.md` | 303 | §7 库 API 重命名表；§8 清单里的 v0.1 flag（`--config-show` / `--config-set` / `--doctor` / `--inspect` / `--skip-burn` / `--only-zh` / `--only-bilingual`）逐条比对 `main` 分支的 `porter_skill/cli.py` | 一致 |

复核里最值得保留的是几处**文档比代码更诚实**的写法——它们是反向验证过的，不是猜的：

* **「半生效」表**：`grep -rn "config.asr.audio_denoise" src/` 确实零结果，`media/prepare.py:132` 只用 `ctx.options.audio_denoise`；`cookies_file` 只在 inspect 路径被读（`platforms/base.py` 读的是 `options.cookies_file`，而 `run.py` 传的是各自的 flag）。
* **死字段**：`--asr-engine` / `--translator` / `--llm-model` 三者只出现在 `models/request.py` 的字段定义与 CLI 的传参处，`src/porter/` 内无消费者（`Pipeline.default()` 用的是自己的 `_default_translator(ctx)`）。
* **MCP §4.3 如实写明没有信号处理**：`grep -rn "import signal\|atexit\|KeyboardInterrupt" src/porter_mcp/` 零结果，并说明兜底来自注册表的 owner-PID **事后**纠正，而不是优雅退出。

四份文档**均未修改**。这不是「没检查」，而是一个结论：完整性无法从文件存在推出，也不会因为写得长就成立，只能靠对照代码判定。

#### 三个 GitHub Actions 工作流

`.github/` 此前是空的。按 §9-P5 第 14 项落地：

| 文件 | 触发 | 内容 |
|---|---|---|
| `test.yml` | push `main` / `refactor/**`、PR | 矩阵 3.10–3.13：ruff + mypy + import-linter + `pytest -m "not slow"`；另有独立 `build` 作业跑 `uv build` 并断言 wheel 含三个顶层包 |
| `release-pypi.yml` | tag `v*`、手动 | gate（含「tag 必须等于 `porter.__version__`」）→ build → PyPI **Trusted Publishing**（OIDC）发布 |
| `release-exe.yml` | tag `v*`、手动 | 三平台矩阵，PyInstaller 只打包 `packaging/launcher.py`，产物附到 GitHub Release |

三个设计决定：

1. **`slow` 测试不进 CI。** 它们真的调 ffmpeg 与网络，既慢又不稳定；非慢速套件通过桩与假实现覆盖同一批代码。
2. **`build` 是独立作业，不是 pytest 之后的一步。** 打包错误（包目录改名、`py.typed` 丢失）对测试套件**完全不可见**——测试从源码树 import。只有真构建一次才会暴露。
3. **PyPI 用 Trusted Publishing 而不是 API token。** 没有可轮换、可泄漏的密钥，`id-token: write` 只授予 publish 作业；仓库里不留任何发布凭据。

#### `packaging/launcher.py`：exe 是引导器，不冻结引擎

§7.3 的方案落地为一个**只依赖标准库**的引导器：定位/创建 `~/.porter/venv` → 首次安装 `porter-workflow[all]` → 约每周刷新一次 yt-dlp → 交棒。四个实现细节是设计而非常识：

* **引导器自己的消息走 stderr。** 交棒后的进程是 CLI，它的 stdout 可能被 `--json` 消费者或 MCP 主机当作 JSON-RPC 帧读取；安装日志出现在 stdout 会同时破坏两者。
* **`uv venv` 不装 pip。** 所以 uv 分支用 `uv pip install --python ...`，回退分支用 `python -m venv`（ensurepip 自带 pip）+ `python -m pip`。两个分支各有自己的安装命令，不能合并成一条。
* **刷新失败也写时间戳。** 离线或代理后面的机器不该在**每次**启动时都付一次必然失败的网络往返；一周后再试。
* **Windows 不用 `os.execv`。** 它不会替换进程映像，会留下一个引导器父进程并破坏 Ctrl-C 传播；改为 spawn 子进程并直接传播退出码。

验证（本地、未联网）：

* **11 项单元检查全过**：`PORTER_HOME` 覆盖、venv 路径布局、刷新判定（无时间戳 / 新鲜 / 8 天前三种）、`PORTER_LAUNCHER_NO_UPDATE` 逃生口，以及**交棒本身**——用一个假 venv（`venv/bin/porter` 打印 argv）跑真实入口，确认退出码 0、argv 透传、且 **stdout 里没有任何引导器消息**。
* **`uv build` + 从 wheel 装进干净 venv**：`porter --version` 输出 `porter 0.2.0`，`porter --help` 列出六个命令（含新增的 `plan`），`porter` 与 `porter-mcp` 两个控制台脚本均就位。这一步顺带证明了 P5 打包配置真的能发布。
* **三个 YAML 用 `yaml.safe_load` 校验**，触发条件与作业名如实打印。
* **工作流里两段 shell 在本地按同样逻辑跑过**：tag/version 比对通过；exe 冒烟测试（假 venv + 假 `porter`）输出 `SMOKE-OK:smoke-test arg`。

#### 门禁

ruff / mypy（94 文件）/ import-linter（2 kept, 0 broken）全绿；**1341 passed**（第二次全量运行，与 §13.43 同一数字——本轮只改文档、workflow 与新增的 `packaging/`，不进包也不进测试路径）。

---

### 13.45 上线后 CI 首次运行：测试对开发机环境的依赖，与新增 hermetic 门禁

仓库改名完成后把 `main` 推到 `origin`，立即触发 `test.yml` → **失败**（52 s，9 failed / 5 errors）。失败的**读法**很关键：表面是「CI 没装 ffmpeg」，实际是**几个测试把开发机环境悄悄写死了**。三类：

| 类 | 具体 | 本地为何测不出 |
|---|---|---|
| 测试替身漏了真实文件系统 | `TestProbeAll._context` 注入了 fake runner / fake `which` / fake `cpu_count`，却把 `tools` 留成 `None`，于是 `probe_ffmpeg` 仍走真实 `shutil.which` + `Path.is_file` | 开发机有 ffmpeg，报告不会短路 |
| 硬编码本机绝对路径 | `TestFfmpegProbe` 把 `/usr/bin/ffmpeg` 当作「存在的那一个」 | 本机该路径确实存在 |
| 断言把开发机 CPU 写死 | 断言 `SOFTWARE_FAST`，而 `_FAST_CPU_CORES = 8`；本机 10 核过、runner 4 核挂 | 纯硬件差异，本地永远看不到 |

第三类最能说明问题：测试的**意图**是「决定档位的是试编码，而不是 `-encoders` 清单里有没有」，但断言写成具体档位，等于把开发机的 CPU 编进了测试。

#### 复现手段：造一个「除 ffmpeg 外什么都有」的 PATH

`ubuntu-latest` 没有 ffmpeg（CI 日志里 `FileNotFoundError: 'ffmpeg'` 与 doctor 的 `not found on PATH` 都指向这一点）。但开发机上 ffmpeg 在 `/bin`、`/usr/bin`、`/usr/sbin` **三处**都有，且 `bash` 也在 `/usr/sbin` —— 所以**不能靠砍 PATH 目录来模拟**：第一次尝试砍 PATH 顺带砍掉了 `bash`，冒出来一批与 ffmpeg 无关的假失败（`test_jobs` 8 个、`test_cli` 1 个、`test_skill_assets` 1 个），差点把归因带偏。

最终做法是建一个 shim 目录：把 `/usr/bin`、`/bin`、`/usr/sbin`、`/sbin`、`/usr/local/bin` 下**除 `ffmpeg*` / `ffprobe*` 之外**的每个可执行文件都符号链接进去（共 2270 个），并把 `.venv/bin` 接在后面。这个 PATH 精确复现了 CI 的 14 个失败中的 **11 个**。

剩下 **3 个无法用 shim 复现**，因为它们根本不是「ffmpeg 不在 PATH 上」：

* 两个 `TestFfmpegProbe` 硬编码 `/usr/bin/ffmpeg`——本机该文件存在，与 PATH 无关；
* `test_the_trial_encode_is_what_decides` 是**核数**差异（本机 10 核 vs runner 4 核），与 ffmpeg 无关。

这两类只能靠读代码 + CI 日志定位。**这是本轮最重要的方法结论：复现手段只能覆盖它覆盖的那一类，不能因为「shim 下只剩 3 个」就把它们归到别的原因上。**

#### 修法（三处，都不是「让它变绿」）

1. `tests/unit/test_doctor_probes.py` —— 新增 `present_tools` fixture，在 `tmp_path` 里造两个**真实存在**的空文件交给 `FFmpegTools`，并作为 `TestProbeAll._context` 的默认 `tools`（显式传 `tools=` 的测试仍然覆盖默认值——`defaults.update(kwargs)` 的顺序保证了这一点）。理由是：`probe_ffmpeg` 在注入了 fake runner 时**仍然**检查真实文件系统，所以不给它存在的路径，报告就会短路成一个 ffmpeg blocker，测试**静默地不再覆盖它声称覆盖的东西**。
2. `tests/unit/test_encode.py` —— 改成断言**不变量**：回落后是本机的软件档位 `software_profile_for().tier`；并补一条 `assert profile.tier in (SOFTWARE_FAST, SOFTWARE_SLOW)` 保持鉴别力，防止第一条把它变成空转。
3. `tests/unit/test_pipeline_assembly.py` —— `TestRendererWiring` 加一个**类内** autouse fixture，把 `EncoderSelector.select` 打桩成 `software_profile_for()`。该类的主题是「render 失败必须变成数据、且归属 BURN 阶段」，它需要**真实**的 `FfmpegRenderer`（假 stub 不写 ASS 文件，正是真实校验拒绝烧录），但不需要真实的硬件探测。**没有放进 `conftest.py`**，因为全局 autouse 会影响真正要测编码器行为的 `test_encode.py`。

三处的共同点：**保留全部原有断言**，改的是「测试从环境里取什么」，不是「测试断言什么」。两处反向验证都做了（撤掉修复即复现原报错）。

#### CI 装 ffmpeg，而不是把那批测试标 `slow`

`tests/regression/test_synthesizer_port.py` 的 5 个测试跑**真的** ffmpeg。它们没有被标 `slow` 是**有意的**：`tests/unit/test_burn.py` 的 `_stub_probe` docstring 明确写着「真实探测与真实编码由 `tests/regression/test_synthesizer_port.py` 覆盖，它跑真的 ffmpeg」。也就是说那批测试**被设计成**真编码层——把它们踢出 CI，等于丢掉最易出错的 burn 路径的覆盖。所以 `test.yml` 与 `release-pypi.yml`（同一个 gate）都装上 ffmpeg（Ubuntu 的包带 libass），并在装完后立刻 `ffmpeg -version | head -1` / `ffprobe -version | head -1` 自证。

顺带确认**不需要 CJK 字体**：`CapabilityReport.ok` 的实现是 `return not self.blockers`，字体缺失只是 DEGRADED，不影响 `ok`。

#### 新增门禁：hermetic 作业

把这次的 bug 变成永久门禁。`test.yml` 的第三个作业（`hermetic`）**先装 ffmpeg，再把它藏起来**，然后重跑非慢速套件：

```bash
sudo mv "$(command -v ffmpeg)" "$(command -v ffmpeg).hidden"   # ffprobe 同理
uv run pytest -m "not slow" --deselect tests/regression/test_synthesizer_port.py
```

两个自证细节：**先装再藏**（否则作业会在「本来就没有」的情况下通过，什么都没证明）；藏完立刻用 `command -v` 复查，还在就 `exit 1`。也就是说这个作业**不可能因为「没藏成」而假绿**。

本地验证了同一套 shell 逻辑（用替身工具跑隐藏逻辑，并单独确认「工具不存在」时会走进 vacuity 分支并 `exit 1`），并在无 ffmpeg 的 shim PATH 下确认 `--deselect` 后是 **1300 passed, 1 skipped, 40 deselected, 0 errors**。

#### 验证矩阵

| 环境 | 命令 | 结果 |
|---|---|---|
| 有 ffmpeg | `pytest -m "not slow"` | 1307 passed, 34 deselected |
| 无 ffmpeg（shim）| `pytest -m "not slow"` | 1301 passed, 1 skipped，**仅 `test_synthesizer_port.py` 的 5 个 error** |
| 无 ffmpeg（shim）| 同上 + `--deselect tests/regression/test_synthesizer_port.py` | 1300 passed, 1 skipped, 40 deselected, **0 errors** |
| 有 ffmpeg | `pytest`（全量）| **1341 passed** |
| — | ruff / mypy（94 文件）/ import-linter | `All checks passed!` / no issues / 2 kept 0 broken |

第二行是本次修复的核心证据：**唯一残留的 ffmpeg 依赖，就是那个故意的真编码层。** 这也把「CI 需要 ffmpeg」从一句经验之谈，变成了一条被测过的边界。

---

### 13.46 CI 矩阵证伪了一个从 P1 起就存在的声明：Python 3.10 上 `import porter` 直接失败

§13.45 的修复推送后，CI 只剩 `gate (py3.10)` 一个作业红（退出码 2 = collection error），其余全绿——包括**新增的 hermetic 作业**（53 s 通过）。

#### 根因

`src/porter/config.py:32` 的**模块级** `import tomllib`。而 `tomllib` 是 **Python 3.11** 才进入标准库的。于是 3.10 上：

```
ModuleNotFoundError: No module named 'tomllib'
ERROR collecting tests/integration/test_burn_pipeline.py
ERROR collecting tests/regression/test_ass_port.py
...（共 11 个模块）
```

`tests/` 里**没有任何一处**直接 import `tomllib`——它们只是 import 了 `porter`，而 `porter` 拉进 `porter.config`，整个包在那行就死了。

而项目同时声明着四处 3.10 支持：`requires-python = ">=3.10"`、classifier `Programming Language :: Python :: 3.10`、ruff `target-version = "py310"`、mypy `python_version = "3.10"`。**这条声明从 P1 起就从未被真正执行过**：开发机是 Python 3.14，本地一直全绿。

`mypy` 对这类错误恰好是**盲的**：`[tool.mypy]` 设了 `ignore_missing_imports = true`，所以即便 `python_version = "3.10"`，缺失的 `tomllib` 也只是被当作 `Any` 放过。这正是「target-version 不是兼容性测试」的具体体现。

#### 它确实是唯一障碍

讽刺的是代码库其余部分**刻意**为 3.10 做了适配：`media/encode.py:90` 与 `doctor/probes.py:107` 都有注释说明「用 `(str, Enum)` 而不是 `enum.StrEnum`，因为后者是 3.11+，而本项目的下限是 3.10」。也就是说 3.10 的功课是做过功课的，`tomllib` 是唯一漏掉的一处。

在动手前先用 grep 扫过全部 3.11+ 构造确认这一点（`datetime.UTC` / `Self` / `StrEnum` / `typing.override` / `TaskGroup` / `hashlib.file_digest` / `except*` / `ExceptionGroup` / PEP 695 的 `type X =` 与 `class X[T]`），除上述两处**注释**外零命中。

#### 修法（用户选定：保留 3.10 支持，补后向移植）

```python
if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib      # 3.11 的 tomllib 就是 tomli 上游化的结果
```

外加条件依赖 `tomli>=2.0; python_version < "3.11"`。

选**模块级**而不是把导入挪进 `load_config_file()`：它是标准库级模块，没有启动开销可省；而模块级导入能在条件依赖于缺失时**尽早且响亮**地失败，而不是等用户第一次读 TOML 才报错。

#### 验证：在真的 3.10 上跑

这是本轮唯一有意义的验证方式——开发机是 3.14，在 3.14 上怎么跑都不可能重现。用 `uv python install 3.10` 装上 Python 3.10.21，建独立 venv，`uv pip install -e ".[all,dev]"`：

| 检查 | 结果 |
|---|---|
| `import porter`（原本就死在这一行） | OK → `0.2.0` |
| `porter.config.tomllib is tomli` | True（tomli 2.4.1） |
| 真实读一个 `.toml` 并 `resolve()` | `output_dir` / `style.zh_font_size` 都正确 |
| 全量 pytest @ 3.10.21 | **1341 passed**（与 3.14 同数） |
| mypy @ 3.10 venv | no issues in **94** source files |
| ruff / import-linter @ 3.10 | `All checks passed!` / 2 kept, 0 broken |
| 回归：全量 pytest @ 3.14 | **1341 passed** |

第一次安装撞上 `openai` wheel 的下载超时（`UV_HTTP_TIMEOUT` 默认 30 s 偏小），改成 `UV_HTTP_TIMEOUT=180` 后通过。与代码无关，但值得记下来——CI 上没出现只是因为 runner 的网络更好。

#### 教训

**「声明支持某版本」与「在该版本上跑过」是两件事。** `requires-python`、classifier、`target-version`、`python_version` 四处的 3.10 声明全部没被执行过，而 `ignore_missing_imports = true` 恰好让 mypy 对「stdlib 版本差异」这一类错误失明。CI 的版本矩阵是这里唯一能发现它的东西——这就是为什么那个矩阵值得占四个并行作业。
---

### 13.47 YouTube 自动配音让 `*-orig` 不再唯一：字幕源轨选错，且测试恰好看不见

§13.39 那次端到端验证留下的作业，重跑时产出了一份**阿语源字幕 + 中文译文字幕**，而视频是英文的——英文在任何地方都没出现。顺着查，缺陷在 `select_source_lang` 的第一条规则。

#### 根因：`*-orig` 从「唯一答案」变成了「每语言一个」

`src/porter/subtitles/srt.py` 的 `select_source_lang` 原文是：

```python
for key in subtitles:
    if key.endswith(_ORIGINAL_SUFFIXES):   # -orig / -original
        return key                          # ← 无条件信任"第一个"
```

在 YouTube 推出**自动配音（auto-dubbing）**之前这是对的：`*-orig` 标记「与原始音轨一致的那条轨」，一个视频最多一条。自动配音之后，YouTube 会为**每一种配音语言**各生成一条 `<lang>-orig` 轨。实测该视频有 **20+ 条**：`ar-orig`、`bn-orig`、`nl-NL-orig`、`en-orig`、`fr-FR-orig`、`de-DE-orig`、`iw-orig`、`hi-orig`、`uk-orig`…

于是「第一个」变成「YouTube 恰好先列出哪个」——真实数据里 `ar-orig` 在索引 6，`en-orig` 在索引 157。取到 `ar-orig` 之后，又因为 `prefer_existing_chinese` 复用了自动翻译出的 `zh-Hans`，计划便宣布「不需要翻译」，最终输出里一条英文都没有。

#### 证据链

| 证据 | 观察 |
|---|---|
| `yt-dlp --list-subs` | `ar-orig` 只有 **1 组**格式；`ar` / `en` / `zh-Hans` 等各有 **21 组重复**（21 = 翻译目标数）→ `-orig` 是 ASR 原始轨，其余是自动翻译轨 |
| 轨道内容 | `en-orig` 是英文原文（"This is my speech to text extension for pie. Alt M to open up the mic."）；`ar-orig` 是这段英文解说的阿语译文 |
| info dict | `language: en-US`（视频自己声明的语言），human `subtitles` 为空 |
| 计划结果 | `plan[source] = ("ar-orig", True)`、`plan[zh] = ("zh-Hans", True)` |

**为什么测试全绿而行为是错的**：`tests/` 里每一处 caption map 都**只有一个** `-orig` 轨（`{"en-orig": [...]}`、`{"de-original": [...]}`）。单轨情况下「第一个」就是唯一一个，任何实现都对。这与 §13.45 是同一类病——测试夹具与被测代码**一致地错**，于是双双绿灯；也再次印证 §13.41：反向验证是唯一能发现「测试因为错误的原因通过」的手段。

#### 修法：用视频自己声明的语言做裁决

三级选择，而不是「第一个」：

1. `_matching_lang(originals, declared_lang)` —— 主语言子标签（`en-US` → `en`）与候选匹配者。
2. `_preferred_original(originals)` —— 按 `SOURCE_LANG_PRIORITY` 在候选中挑（英文打头，因为流水线常态是 en → zh）。
3. `originals[0]` —— 无任何信号时的最后手段（保留原行为）。

`declared_lang` 由 `info["language"]` 传入，经 `PlatformSpec.select_source_lang` 转发。

**为什么第 2 级不能取代第 1 级**：优先级列表以 `en` 打头，而一个**阿语**视频同时带 `ar-orig` 与 `en-orig` 时，只看列表就会给阿语视频配英文字幕。视频自己声明的语言才是权威信号；列表只是「没有声明时」的退路。

**第二个调用点**：`_build_metadata` 也调用 `select_source_lang`（决定 `VideoMetadata.official_subtitle_lang`）。第一轮只改了 `plan_subtitles`，于是同一份 info 会让元数据说 `ar-orig`、计划去取 `en-orig`。已一并传入 `declared_lang`，并加一条**「元数据与计划不得分歧」**的测试把这条不变式钉住。

#### 反向验证矩阵（三种反转，各自必须失败）

| 反转 | 结果 |
|---|---|
| 撤销整段选择逻辑（回到「第一个 `-orig`」） | **5 个**新测试失败 |
| 只撤销 `_build_metadata` 的 `declared_lang` 传递 | **1 个**失败（元数据/计划一致性测试） |
| 只撤销 `declared_lang` 项、保留优先级列表 | **2 个**失败（两条「两个信号冲突」的测试） |

第三种反转是本轮**最有价值的一步**，因为它暴露了我自己写的一个空洞测试：为 `_build_metadata` 写的第一版用了 `declared_lang="en-US"`，而它在**未修**状态下也通过——因为新增的优先级列表兜底恰好也选 `en-orig`。是反向验证把它抓出来的。改写成 `declared_lang="ar"`（唯一让两个信号给出不同答案的取值）之后才真正覆盖到那个调用点。**测试必须落在两种信号给出不同答案的地方**，否则它测的不是裁决逻辑，而是兜底逻辑。

#### 验证

| 检查 | 结果 |
|---|---|
| 真实输入（17:06 实测的 caption map，15 条 `-orig`，document order 首个是 `ar-orig`） | `plan[source] = ("en-orig", True)`、`plan[zh] = ("zh-Hans", True)`、`metadata.official_subtitle_lang = "en-orig"`，三者一致 |
| 全量 pytest | **1349 passed**（1341 + 8 个新测试） |
| ruff（`src tests`）/ mypy / import-linter | `All checks passed!` / no issues in 94 files / 2 kept, 0 broken |

**未能完成的一步**：真实 URL 重跑与端到端作业被 YouTube 的 `Sign in to confirm you're not a bot` 挡住——17:04 那次抓取还能匿名访问，17:07 之后本机 IP 就进了这个检查。没有改用 `--cookies-from-browser`：那是读取浏览器凭据，属于需要用户明确同意的动作，不该为了补一次验证就顺手做掉。因此本轮的「真实输入」是 17:06 捕获的真实 caption map，不是一次新的网络抓取。

#### 残留风险

`prefer_existing_chinese` 仍会复用自动配音视频上的 `zh-Hans` 自动翻译轨。该轨是 YouTube 用它自己的 ASR 原文翻译出来的，而那份原文正是被标成阿语的 `ar-orig`——也就是说复用来的中文轨，其翻译源不可知。本轮只保证**英文字幕会出现**（原缺陷的症状），不保证复用中文轨的质量。这是另一个缺陷，未在本轮处理。
---

### 13.48 本地 Whisper 后端（零 Key 免网络的 ASR），以及它暴露的两个真缺陷

起点是一个具体的任务：处理一条 Instagram 链接。Instagram 的平台字幕轨不存在（`yt-dlp --list-subs` 直接答 `has no subtitles`），所以必须跑 ASR；而 §13.21 的结论是"今天不存在免 Key 的语音识别路径"。用户追问：**为什么不能把 VideoCaptioner 的本地免 Key ASR 借鉴过来集成进 CLI？** 这个问题的答案就是本轮的工作。

#### 先回答：VideoCaptioner 的"免 Key ASR"只有一支值得拿

`pipeline.py` 的 `_VIDEOCAPTIONER_ENGINES = {bijian, jianying, whisper-cpp}` 是它暴露的全部选项，而它们分属两类：

| 引擎 | 本质 | 能否解决零 Key |
|---|---|---|
| `bijian`（必剪） | **在线**逆向接口——porter 自己的 `asr/bcut.py` 就是它 | ❌ 已实测：`resource/create` HTTP 200，真实转录零条 utterance |
| `jianying`（剪映） | 同族的字节**在线**接口 | ❌ 同一类风险，且是随时可失效的逆向协议 |
| `whisper-cpp` | **真·本地**：whisper.cpp + ggml 模型 | ✅ 唯一有价值的一支 |

所以 §13.21 的结论需要加一个前提：**不存在免 Key 的*在线*路径**；**本地推理这条路从来没被堵上**。VideoCaptioner 的免 Key 里 2/3 是 porter 已经有、且已经死掉的在线端点。

另外不能直接调用它：GPL-3.0 + pin `python<3.13`，不能进 `dependencies`、不能 import（许可证污染），只能子进程；本机也没有那个二进制。而且它的 `ENGINES` 列表**缺 `faster-whisper`**——那才是它本地转录的主力引擎。

#### 实现：`src/porter/asr/whisper_local.py`

自己实现本地 Whisper，用宽松许可的组件，而不是抄 GPL 代码：

* **`faster-whisper`**（MIT）。相对 `openai-whisper` 的决定性优势是它跑在 CTranslate2 上，**不需要 PyTorch**——extra 约 100 MB 而不是 ~2 GB。
* 新 extra `[asr-local] = ["faster-whisper>=1.0"]`，并入 `all`；惰性导入，`available()` 在缺包时报 `False` 而不是炸掉整个 ASR 包。
* 后端只有协议要求的四个成员（`name` / `endpoint_verified` / `available(ctx)` / `transcribe(audio, ctx)`），所以接入 = 一个文件 + `_default_transcriber()` 里一行。

四个刻意的设计决定：

1. **`endpoint_verified = True`，而且是结构性的**。这个字段问的是"协议是否被实测过"；本地推理没有远端协议，实测一次就永远成立，不受对方改接口影响。这正是 §13.21 留下的"可用后端全是 unverified 逆向端点"那个坑的解药——`porter_plan` 现在可以说"可行"而不用加但书。
2. **`available()` 不联网**。协议要求它便宜且每个作业每个后端都调一次，所以它只回答"`[asr-local]` 装了没"。**不是**"模型下好了没"：首次运行没权重也能成功（会下载），在这里报 False 会让链跳过唯一能工作的后端、掉进已实测失效的端点——最坏的答案。模型是否已在本地由 `model_is_cached()` 单独回答，`porter doctor` 用它把首次下载的成本提前说清。
3. **音频整段交给模型，不分片**。`CHUNK_SECONDS = 480` 是云端防上传超时用的；faster-whisper 自带 VAD 与分段，先切文件只会切断句子、在时间轴上留缝。这是最容易照抄错的地方。
4. **`auto` 设备先试 CUDA 再退 CPU，显式设备不退**。CUDA 快得多但需要 cuDNN/cuBLAS，而设备探测看不到这一点（本机就没有），只有真正构造模型才知道；显式点名设备则是请求，静默替换会掩盖错误。

模型缓存检查用 `faster_whisper.utils.download_model(..., local_files_only=True)`，而不是直接摸 `huggingface_hub`：同一个库自己解析尺寸到仓库、尊重 `HF_HUB_CACHE`、把**残缺下载**当作不存在（本机的 `faster-whisper-medium` 正是 67 MB 的 config/tokenizer 而没有 `model.bin`）。自己实现等于再写一份会漂移的规则。

#### 链位次与 `--asr-engine` 的修复

**本地 Whisper 排在链首**。v0.1 把付费 API 排第一的理由（质量、速度）写于免费端点还能用的年代，而它们已实测失效（§13.21）——第一位应该给最可能跑完的后端。想要付费端点的用户现在可以**明确指定**，这比"如果碰巧配了 key 就偷偷重排"是更好的契约。

为此 `asr.engine` 从"只有三个 VideoCaptioner 引擎名有效"改为通用规则：三个 VC 名字仍然把外部 CLI 提到最前（v0.1 行为），**其他任何能匹配后端名的值把该后端提到最前**，其余后端保留为回退；匹配不到则记一条 warning 而不是默默忽略。这修掉一个真实缺陷：

* `--asr-engine whisper-api` 被 CLI 收进 `JobOptions` 后**无人读取**，链序完全不变；
* 而 `docs/CONFIG.md` 明写"写 `whisper-api` 只是让它排在最前"——**代码里没有任何这种重排逻辑**，文档比代码更宽松；
* SKILL.md 的「常用参数」还写着"强制某个后端（跳过回退链）"，两条都不成立。

（同一轮还确认 `--translator` / `--llm-model` 是**完全死字段**：`src/` 里零消费者，翻译链顺序硬编码。**未修**，留作后续。）

#### `porter doctor` 的文案必须跟着改

`probe_asr_route` 原先告诉每个操作者"唯一免 Key 的路是两条不转录的逆向端点"。装了本地模型之后那句话就是**假的**，而报陈旧事实的 doctor 比不报还糟。现在它按链序回答，并说出模型是否已下载：

```
[OK  ] Speech-to-text route: local Whisper (small, already downloaded) — no key, no network
```

#### 副作用：装了 `faster-whisper` 会让 `mypy` 整个挂掉

`faster-whisper` 依赖 `ctranslate2`，后者依赖 `numpy`。而 numpy 的 `__init__.pyi` 用了 PEP 695 `type` 语句（3.12+），mypy 在 `python_version = "3.10"` 下**不是报一个模块的错误，而是中止整个运行**：

```
numpy/__init__.pyi:737: error: Type statement is only supported in Python 3.12 and greater  [syntax]
Found 1 error in 1 file (errors prevented further checking)
```

numpy 不是我们的依赖、我们的代码也不 import 它——它经 `openai/_extras/numpy_proxy.py`（OpenAI SDK 为可选依赖做的代理模块）进入图。逐个试过才确定哪个旋钮有效：

| 手段 | 结果 |
|---|---|
| `ignore_errors = true` on numpy | ❌ 语法错误无法抑制 |
| `follow_imports = "skip"` on `numpy` | ❌ mypy 仍会解析被直接 import 的模块 |
| `follow_imports = "skip"` on **`openai._extras.*`** | ✅ 有效 |
| `mypy_path` 里放一个宽松的 numpy stub 遮蔽 | ❌ site-packages 优先 |
| `no_site_packages = true` | ✅ 但会连带放弃 pydantic 等全部第三方类型检查，损失太大 |

所以修法是**只对 `openai._extras.*` 停止跟随**——那个包的全部职责就是 import 可选的第三方库（numpy/pandas 代理），我们不用它，而 openai 自身的类型、以及 `python_version = "3.10"` 的底线都保住了。注释里记了完整的排除过程，因为"为什么是这一行"比"这一行是什么"更容易丢。

#### 真实验证

| 检查 | 结果 |
|---|---|
| 30 秒真实音频直接调后端 | 5 条 cue，英文准确；`origin = faster-whisper:small:cpu` |
| CUDA 回退 | 真实发生：`libcublas.so.12 is not found or cannot be loaded` → 退到 CPU int8（本机确实没有 cuDNN/cuBLAS） |
| 速度 | 30 秒音频 9.0 s（CPU int8, small，约 3.3× 实时） |
| 真实作业（Instagram） | 19 条 cue，11.1 s |
| `porter doctor` | `local Whisper (small, already downloaded) — no key, no network` |

#### 首次真实 CLI 运行暴露的缺陷一：burn 的相对路径

作业在 BURN 阶段失败，报 `ffmpeg could not read subtitle_bilingual.ass or the master video`，而 `raw/video.mp4` 明明在。

根因：`burn_hardsub` 为了把撇号路径赶出滤镜图，让子进程以 `cwd=subtitle.parent` 运行、滤镜图只写裸文件名——**但 argv 里其余的路径没跟着变绝对**。管线传的是输出相对路径（也正是它打印给用户看的那些），于是 ffmpeg 去找 `cooked/porter_output/<task>/raw/video.mp4`。

**为什么所有测试都看不见**：它们一律用 `tmp_path`，而 `tmp_path` 天生是绝对路径。相对路径这一半完全没有覆盖——**cwd 这个修法上线时就是带洞的**。加三条测试（相对路径变绝对、滤镜图仍只有裸文件名、临时文件仍在目标同目录）并反向验证（只撤掉 `.resolve()` → 2 条失败；第三条是防过度修正的守卫，两种情况都通过，这点在记录里说清楚）。

#### 首次真实 CLI 运行暴露的缺陷二：测试污染开发者真实状态

查作业时发现 `porter jobs list` 里有 `/videos/a.mp4` 这种条目。查下去：**每跑一次完整测试套件就往 `~/.cache/porter/jobs.json` 写入 4 条真实记录**（`porter_job_*` 的取消与失败测试）。

根因：`src/porter_mcp/tools/jobs.py:44` 是模块级 `_STORE = JobStore(registry=JobRegistry())`，而 `JobRegistry.__init__` **在构造时**就把 `registry_file()` 求值成路径。于是"导入时"就绑定了真实的 `~/.cache/porter/jobs.json`——**早于任何 fixture 能重定向那个 seam**。§13.33 加的 conftest 隔离 fixture 本身没错，只是补得太晚。

修法是把默认路径改成**按访问解析**（`path`/`lock_path` 变成只读 property，显式传入的路径仍然钉住），这样对所有"导入时构造"的持有者都成立，而不只是这一个。验证：修复前 `tests/unit` 单跑 +4，修复后 +0；全量套件 +0。反向验证（把赋值放回 `__init__`）→ seam 测试失败。

这条与 §13.45、§13.47 是同一类病：**测试替身/夹具与被测代码一致地错，于是双双绿灯**；而这次发现它的是"去看真实状态文件"，不是任何一条测试。

#### 交付结果（真实作业，端到端）

`porter --config <skill config> run <instagram url>` → **done in 68.2 s**：

```
→ Burning hardsubs
20:39:55 INFO porter.media.encode: using NVIDIA NVENC for video encoding
20:39:59 INFO porter.media.burn: burned subtitle_bilingual.ass -> video_bilingual.mp4 (15.3 MB)
20:40:03 INFO porter.media.burn: burned subtitle_zh.ass -> video_zh.mp4 (14.2 MB)
```

质检（不只看退出码）：`subtitle_zh.srt` 含 CJK；两个成片均 h264/720×1280/aac、时长 73.9 s 与母版一致；**像素级证明**字幕真的烧进去了——母版字幕带 YMAX 141–166，成片 238–239（近白文字），同时抽出帧用眼睛看过，纯中文版与双语版（中文白、英文黄）排版都正确。

#### 未修的实测发现：免 Key 翻译链本轮全灭

同一个作业的第一轮在 TRANSLATE 阶段失败，四个后端各自的原因都不同：

| 后端 | 本轮实测 |
|---|---|
| `llm`（用户配置的 DeepSeek key） | `402 Insufficient Balance` —— 余额不足 |
| `bing` | `bing batch response was not recognised` —— §13.40 修过一次的症状**复现** |
| `google` | `HTTP 429`（`gtx` 与 `dict-chrome-ex` 两个 client 都是） |
| `mymemory` | 首次 `read timeout (8.0s)`，随后成功 |

最终是 `mymemory` 完成了翻译（19 条 cue 全部译出，语义正确）。两条值得后续处理：**bing 的批量响应识别又坏了**（是 §13.40 的回归还是端点又变了，未查），以及 `--translator` 这个本该用来绕开它的开关是死的。

---

### 13.51 六个已知问题：三个死字段、一个实测的载荷上限、一个无法满足的新鲜度判定

一轮"修已知问题"。六个都来自此前的诚实记录（§13.34 缺口表、§13.48 的未修发现），其中三个的根因只有真实运行才能确定。

#### ① `--translator` / `--llm-model`：同一类死字段的第三、第四次

§13.48 刚修过 `--asr-engine`，而 `JobOptions.translator` 与 `llm_model` 是**完全一样的病**：两个前端的入口都收下它们，`src/` 里零消费者。`_default_translator()` 无条件装配整条链，LLM 模型只取自 `ctx.config.llm.model`。`docs/CONFIG.md` 当时已经把三者一起标成死字段——这是项目里少见的"文档比代码诚实"。

修法与 `--asr-engine` 完全对齐，因此也复用了同一个 `_promote_named`：

* `translator` → 把该后端**提到链首**，其余保留为回退；名字写错记 warning，链序不变。可用名字是后端自身的 `name`：`llm`、`bing`、`google`、`mymemory`、`videocaptioner-llm`、`videocaptioner`。**不做别名猜测**——`videocaptioner` 与 `videocaptioner-llm` 是两个独立后端（后者额外需要 key），点名哪个就只提哪个。
* `llm_model` → 新增 `effective_llm_model(ctx)`（`ctx.options.llm_model or ctx.config.llm.model`）。它必须放在一个地方：**有两个后端读这个值**——`translate/llm.py` 自己，以及 `videocaptioner-llm` 适配器（把它作为 `--model` 转发给外部 CLI）。只修前者会让后者继续用配置里的默认值，而这种分裂没有任何单条测试能发现。

顺带修掉两处**帮助文本与实现不符**：`--asr-engine` 写的是"Force one ASR engine instead of walking the fallback chain"，而实现是"提到链首、保留回退"（§13.48 的取舍）。用户明确点名一个后端，要的是**先试它**，不是"只准用它"——§13.48 那轮实测 bing 被限流、google 429，若点名即独占，作业会直接失败而不是换到 mymemory。

**反向验证（三重，每个 seam 单独撤）**：撤掉 `translator` 的读取 → 6 个参数化用例失败；撤掉 `llm.py` 的 `effective_llm_model` → 1 个失败；撤掉 `videocaptioner.py` 的 → 1 个失败。

#### ② bing：不是回归，也不是限流，是**载荷尺寸上限**

§13.48 记录 bing 报 `bing batch response was not recognised`，当时无法判断是 §13.40 的回归还是端点又变了。直接探测真服务得到结论——而且与两个猜测都不同：

| cues | chars | 结果 |
|---|---|---|
| 1 | 77 | 翻译成功 |
| 2 | 161 | 翻译成功 |
| 4 | 329 | 翻译成功 |
| 8 | 665 | 翻译成功 |
| **15** | **1259** | **HTTP 200，body 是 `{"statusCode": 400}`** |

所以：**不是限流（重试无用），也不是响应格式变了**（小批量完全正常）。共享常量 `MAX_TEXTS_PER_REQUEST = 15` 是按 Google 的截断阈值定的，它高于 bing 的真实上限，于是**每一个 15 条批量都被拒**。

修法沿用 §13.40 自己的结论——"批量是优化，不是契约"：

1. bing 用自己的 `_BATCH_SIZE = 8`（实测能过的最大值），注释里带上整张测量表；
2. **被拒绝时降级为逐句请求**，而不是判整个 backend 失败。区分"拒绝"（body 里带 statusCode，是**关于这次请求**的陈述，换个更小的请求可能成功）与"无法识别的形状"（换个请求形状也不会好）——后者仍然直接报错，否则格式一变就会先浪费 N 次请求再失败。

**这条修完，真实作业的翻译由 bing 完成**（§13.48 那轮是 mymemory 兜底）。

#### ③ 那个把证据丢掉的消息

`_describe_body` 是这轮最有价值的十行：旧消息是裸的 `"bing batch response was not recognised"`，**把唯一的证据扔掉了**，于是 §13.48 那轮只能靠手工重跑探测才能知道 bing 到底说了什么——而手工重跑当时又成功了（因为限流/尺寸的组合没复现）。

现在同一个失败会给出 `reason='service refused: statusCode=400 message=None'`。它在**真实运行**里立刻兑现了：修复后的一次作业日志里直接看到 `statusCode=400`，再据此设计出上面的尺寸探测。**一个不可行动的错误消息，代价是一整次调查。**

#### ④ 429/5xx 重试：可取消、尊重 `Retry-After`、有上限

§13.48 那轮四个翻译后端同时失败，其中 google 两个 client 都是 HTTP 429。原来 google 有 client 级回退、bing 没有，而两者都**没有重试**：一次限流就把"慢一点"变成"这个后端坏了"，然后交给链上可能更差的引擎。

新增共享策略（`translate/base.py`），并复用 `platforms/inspector.py` 的既有惯例——**退避常量放在模块级**（测试可以置零）且**用 `ctx.cancel.wait` 而非 `time.sleep`**（取消要立刻生效，不能先睡完）：

| 项 | 取值 / 规则 |
|---|---|
| 可重试 | 429 与 5xx；403/404 不重试（重复不会改善，只浪费预算） |
| 退避 | 线性递增（1.5s × 尝试数），上限 8s |
| `Retry-After` | **替换**计算出的退避（服务端比我们更清楚何时会接受），但仍受 8s 上限——`Retry-After: 3600` 不能让作业挂一小时，链上还有四个后端现在就能做完 |
| 次数 | 3 |

google 的**逐句路径也接同一个 helper**。第一版只改了 `_translate_batch`，这正是 §13.47 在 `_build_metadata` 上犯过的"第二个调用点"错误，这次在写的时候就一起收了。

**反向验证抓出一个空洞测试**：`test_google_recovers_from_a_throttle` 第一版只给了一个 429，撤掉重试后**依然通过**——因为 google 的两个 client 分别限流，第二个 client 顶上了。那是本来就有的 client 回退，与重试无关。改成连续两个 429（只能靠重试同一 client 才能活）并断言三次请求的 `client` 参数相同，撤掉重试才真正失败。

**实测代价（诚实记录）**：google 的 429 是**按 client 的硬封禁**，不是限流——重试救不了它，只是把失败诊断得更清楚。一次真实探测：bing 11.7s 成功；google 29.1s 失败（含约 9s 退避），`reason='client=dict-chrome-ex returned HTTP 429'`。这 9 秒只在链走到 google 时才付出，且换来"瞬时限流能被吸收"。

#### ⑤ 单阶段从磁盘恢复：TRANSCRIBE 复用，以及一个**无法满足**的新鲜度判定

§13.34 把"`--force` 的单阶段从磁盘恢复"列为**发布前必做**。实测痛点是明确的：第二次跑同一个作业时**重跑了 TRANSCRIBE**（74 秒的视频 16–21 秒，长视频是分钟级），而音频和 ASR 配置都没变。

修法照抄 BURN 已有的复用惯例（"产物至少与它的输入一样新"），加一件 SRT 装不下的事实：

* 新增 `cooked/.transcribe.json` 记录 `{used_asr, origin}`。**没有 sidecar 就不复用**——`used_asr` 是要报给操作者的（"这次作业付了 Whisper 的钱吗"），猜一个值等于在报告里写假话；旧任务目录重跑一次识别即可。"只复用能被诚实描述的东西"就是全部规则。
* 复用条件：非 `force`、`only_phase` **不是** TRANSCRIBE（点名一个阶段就是要**跑**它，静默返回缓存会让 `--only-phase transcribe` 变成空操作）、SRT 存在且不比输入旧、sidecar 可读。
* **`--only-phase` 的语义因此变得完整**：前置阶段照跑，但每个阶段自己的复用规则让昂贵的工作被跳过。用户改样式后 `--only-phase burn`：PREPARE 复用母版、TRANSCRIBE **复用缓存**（跳过 ASR）、TRANSLATE 重跑（样式变了，必须重跑）、BURN 重烧。

**真实运行抓到一个单元测试永远看不见的缺陷**：第一版的新鲜度判定拿 `_best_audio(raw)` 比较，而它优先取 `audio_enhanced.wav`——**PREPARE 每次调用都重跑音频增强**，于是那个文件每次都有新 mtime，判定**永远不可能满足**。实测证据：run A 写了 SRT（22:17:44），run B 的 PREPARE 在 22:20:34 重写了增强音频 → run B 又转录了一遍。改为与 `raw.audio`（标准化母版音频，母版复用时它是稳定的）比较，并补一条测试钉住"增强副本被重写不得使缓存失效"。

残余限制与 BURN 一致：改**增强设置**不会使缓存失效，`--force` 是文档化的出口。

**真实作业验证**：`reusing 19 cached source cues from subtitle.srt`，ASR 被跳过。同一作业的耗时结构随之可见：40s 在 PREPARE 的元数据抓取、15s 在缩略图超时、13s 在翻译、11s 在压制——**下一个瓶颈已经不是 ASR**，这点值得后续处理。

#### ⑥ 无字幕路径：显式 `--subtitle-file`，而不是猜

§13.29 刻意留空的一半：本地视频旁边的 `.srt` **不**自动接管，因为一份 `.srt` 可能是源字幕也可能是译文字幕，猜错会静默跳过 ASR 或覆盖用户文件。另一半：平台无字幕轨 + ASR 不可用时，作业报 `every speech-to-text backend failed`——准确，但没告诉用户下一步。

选择**移除歧义而不是靠猜解决它**：新增 `--subtitle-file FILE`（`JobOptions.subtitle_file`，MCP `porter_job_start(subtitle_file=...)`），支持 `.srt` 与 `.vtt`（VTT 走已有的 `vtt_to_srt`）。它与 `load_platform_subtitles` 的关键区别是**故意不 total**：后者在不可读时返回 `[]`，因为"没有平台字幕轨"是正常结果（意思是用 ASR）；而用户点名了这个文件，静默退回语音识别会把一个拼写错误藏在几分钟的计算和一个更差的转录后面。所以每种失败都报错，并说明怎么办：文件不存在、格式不支持（列出支持的后缀）、内容无 cue。

它同时是**无字幕视频的出口**，因此 `NO_CUES_HINT` 指向它（以及 `[asr-local]` extra）。

**写的时候就犯了一次错，并被测试抓住**：hint 第一版只写在 `_write()` 的 raise 里，而那条路径**几乎到不了**（只在识别出的 cue 全被规范化丢弃时触发）——真实作业看到的消息来自 `_run()`。抽出 `NO_CUES_HINT` 常量给两处共用，并把测试的断言从 `details["backends"]`（`_write` 的键）改为 `attempted`/`failures`（`_run` 的键）。

**真实作业验证**：`using the supplied subtitle file /tmp/hand_made.srt (2 cues)`，ASR 未运行，作业 exit=0，两版成片产出，sidecar 记为 `{"used_asr": false, "origin": "supplied"}`。附带确认一条既有规则不是缺陷：手工 SRT 的两条 cue 被合并成一条，因为合并规则"遇到句末标点才停，**除非**合并后仍不足三秒"——我的测试输入没有标点，合并是设计行为。

#### ⑦ 顺带清理

开发机真实 `~/.cache/porter/jobs.json` 里积了 **40 条测试垃圾**（`source` 为 `/videos/a.mp4`，§13.48 修复前的污染），只有 1 条是真实作业。按 source 精确清理，保留全部真实记录，先备份到同目录（`jobs.json.bak-<epoch>`）再原子替换；`porter jobs list --all` 复核为 1 条。

#### 验证汇总

| 项 | 结果 |
|---|---|
| 全量测试 | **1452 passed**（1388 → +64） |
| 门禁 | ruff `src tests` 全绿、mypy 95 文件、import-linter 2 kept / 0 broken |
| 反向验证 | ① 三重（3 seam）、②/③ 两重（重试预算 + 错误描述）、⑤ 两重（优先级 + hint）、④ 两重（复用调用 + sidecar 门）——**共 9 个 seam，全部"撤掉即失败"** |
| 抓出的空洞测试 | 1 个（google 靠换 client 而通过） |
| 真实作业 | 翻译改由 bing 完成；`reusing 19 cached source cues`；`--subtitle-file` 全链路可用；成片像素级验证（母版 YMAX 141 → 成片 238） |
