# 引擎架构（贡献者版）

本文面向**改引擎的人**：它回答"为什么这么分层""这段代码住在哪""动这里会踩到什么"。

面向使用者的架构说明在 `skills/porter-skill/references/ARCHITECTURE.md`——那份讲的是"四个阶段是什么、后端链什么可用"。本文不重复它，而是补齐它不必回答的问题：分层的强制手段、端口与装配、stdout 纪律的成因、作业注册表为什么要看门狗、以及媒体层每一条规则是为了防哪个具体失败。

**代码是唯一权威。** 本文与代码不一致时以代码为准，并请直接修正本文。

---

## 1. 仓库形态

```
src/porter/            ← 引擎（库）。业务逻辑只在这里
src/porter_cli/        ← 前端 A：`porter` 命令，人类可读渲染
src/porter_mcp/        ← 前端 B：`porter-mcp` 服务器，JSON schema + 异步作业
skills/porter-skill/   ← agent 资产：SKILL.md、references/、scripts/、assets/
tests/                 ← 引擎与两个前端的测试
```

三个包由 `[tool.hatch.build.targets.wheel]` 一次性发布为同一个发行包 `porter-workflow`，但它们是**三个独立的顶层 import 名**。`porter` 是库，`porter_cli` / `porter_mcp` 是它的两个对等消费者；两者互不依赖。

### 为什么引擎不知道部署形态

`src/porter/__init__.py` 的模块 docstring 把三条规则写成了契约：

1. `porter` 永不 import `porter_cli` / `porter_mcp`；
2. `porter` 内禁止 `print()`（见 §5）；
3. `porter` 内不得假设部署布局——没有 agent skill 目录、没有第三方项目的配置路径、没有"配置该往哪写"的推断。

第 3 条是 v0.1 的直接教训。旧版 `config.py` 硬编码了：

```python
Path.home() / ".pi" / "agent" / "skills" / "porter-skill" / "config.json"
Path.home() / ".config" / "videocaptioner" / "config.toml"
```

并且用 `(skill_root / "SKILL.md").is_file()` 来判断该往哪写用户配置。那是把"某个 agent 宿主把 skill 装在哪"和"兼容另一个项目的历史配置"这两件与领域无关的事，缝进了配置解析。今天的替代品是 `$PORTER_CONFIG`：需要的调用方自己声明位置，引擎不需要猜。

代价是很实在的——同一个引擎可以同时被 CLI、MCP 服务器、被 skill 的脚本、被将来的 Web 服务使用，而不需要为任何一方加分支。加平台的成本也随之下降。

### 导入开销是一条被照顾的约束

`porter/__init__.py` 用 :pep:`562` 的模块级 `__getattr__` 做惰性导出：

```python
_LAZY_EXPORTS = {"JobOptions": "porter.models", "Pipeline": "porter.pipeline", ...}
```

`import porter` 只加载 `errors` 与 `logging`（都是标准库级开销）。`porter.pipeline`、`porter.models`、yt-dlp、openai 都不在冷启动路径上。这不是微优化：`porter --help` 会 import `porter_cli.app`，而它引用了 `Pipeline` 与 `JobOptions` 的**类型**，如果没有惰性导出，一个 `--help` 就要付 yt-dlp 的 import 成本。

同样的理由贯穿 `Pipeline.default()`（见 §4 的装配小节）：**所有具体实现都在方法体内 import**。

---

## 2. 分层契约（`import-linter` 强制）

`pyproject.toml` 的 `[tool.importlinter]` 定义两条契约，`lint-imports` 是构建门禁的一部分——**不是愿景文档，是会让构建失败的检查**。

### 契约一：引擎不得依赖前端

```toml
[[tool.importlinter.contracts]]
name = "engine must not import frontends"
type = "forbidden"
source_modules = ["porter"]
forbidden_modules = ["porter_cli", "porter_mcp"]
```

配套的 `include_external_packages = true` 是必需的：两个前端是 `porter` 的顶层兄弟包，不这样声明，契约根本看不到它们。

> "两个前端互不依赖"没有写进 `import-linter`，因为它的 `independence` 契约只能解析位于 `root_package` 内部的 source。改用 `tests/unit/test_architecture.py` 直接断言。

### 契约二：层序（自上而下）

```toml
type = "layers"
layers = [
    "porter.plan",
    "porter.pipeline",
    "porter.doctor",
    "porter.asr | porter.translate | porter.platforms",
    "porter.media | porter.subtitles",
    "porter.ports",
    "porter.jobs",
    "porter.context",
    "porter.models",
    "porter.config",
    "porter.events",
    "porter.logging | porter.errors | porter.utils",
]
```

**方向规则**：列表自上而下编码的是真实依赖图。**一个层只能 import 位于它下方的层**；不得 import 上方的层，也不得 import 同一行内用 `|` 归并的兄弟模块。因此这个列表必须是**严格全序**，不能把平级模块随手归并成一行。

需要点名确认的几处：

- **`porter.plan` 在 `porter.pipeline` 之上**。计划模块会构造一个 `Pipeline.default()` 并观测它，所以它在 import 意义上更高。一个层可以 import 它下方的东西，所以这是自洽的。
- **`porter.doctor` 是横切服务**，位于 pipeline 之下（管线不得无条件跑 doctor）、位于它要报告的 asr/translate/platforms 之上。
- **`porter.jobs` 位于 `porter.ports` 与 `porter.context` 之间**。两个前端都依赖它，而它只需要 models / context / events。

### 这两条路径最初都不在契约里

`porter.jobs` 与 `porter.plan` 一开始**完全不在分层契约中**。也就是说，两个前端都依赖的那个模块，恰恰是唯一不受任何约束的模块——它可以 import `porter.pipeline` 或 `porter.media`，没有任何 gate 会说一句话。它们现在按真实依赖深度各就各位。

验证方式值得记住：**故意加一行违规 import，看 gate 是否报错**。`porter.plan` 第一次被放进契约时位置放反了，契约立刻报 `porter.plan -> porter.pipeline` broken——这证明它真的受约束，而不是被配置忽略。

### 配套的静态检查

`[tool.mypy]` 是 `strict = true`，且必须显式声明 `mypy_path = "src"` / `explicit_package_bases = true` / `namespace_packages = true`。缺后两项时 mypy 会把 `porter_mcp/tools/meta.py` 当成两个不同模块名，直接拒绝检查 src 布局。

`[tool.ruff.lint]` 的 select 里有两项是刻意的：`T20`（禁 print，§5）与 `BLE`（禁裸 `except`）。`BLE` 的用法有约束——引擎只在明确的边界捕获宽泛异常，每处都带 `# noqa: BLE001` 并写明理由，这样异常是"可审计的"而不是"隐形的"。`errors.py` 与 `stdout_guard.py` 被豁免 `N818`：控制流信号与守卫异常不是"故障"，为满足命名规则改名会模糊语义并破坏公开 API。

---

## 3. 四个阶段与 `Pipeline`

`porter/pipeline.py` 的 `Pipeline` **只负责时序，不负责实现**。每个阶段委派给一个端口（§4），因此同一份编排在生产里跑真后端、在测试里跑替身。

| 阶段 | 端口 | 读（内存） | 写（磁盘） |
|---|---|---|---|
| `prepare` | `Downloader` / `LocalPreparer` | — | `raw/video.mp4`、`raw/audio.wav`、`raw/audio_enhanced.wav`（可选）、`raw/cover.jpg`、`raw/subtitle.srt`、`raw/subtitle_zh.srt`、`raw/metadata.json` |
| `transcribe` | `Transcriber` | `RawMaterials` | `cooked/subtitle.srt`、`cooked/transcript.json`、`cooked/transcript.txt` |
| `translate` | `Translator` | `SubtitleSet` | `cooked/subtitle_bilingual.srt`、`subtitle_zh.srt`、`subtitle_bilingual.ass`、`subtitle_zh.ass` |
| `burn` | `Renderer` | `RawMaterials` + `SubtitleSet` | `cooked/video_bilingual.mp4`、`cooked/video_zh.mp4` |

任务目录契约由 `TaskLayout` 独占（`models/materials.py`），v0.1 里五个 extractor 各自手写的 `mkdir` 逻辑收敛到 `ensure_dirs()`：

```
<output_root>/<video_id>_<safe_title>/
    raw/     标准化后的母版资产
    cooked/  字幕与成片
    .tmp/    临时区，两次运行之间可以安全删除
```

`raw/` 与 `cooked/` 是**用户可见契约**，改名会让已有产物失效。

### 阶段之间靠内存传递，不靠磁盘

`run()` 的循环体把每个阶段的返回值直接喂给下一个：

```python
if phase is Phase.TRANSCRIBE:
    raw = self._require(raw, "PREPARE", phase)
    subtitles = self.transcribe(raw, ctx)
elif phase is Phase.TRANSLATE:
    subtitles = self._require(subtitles, "TRANSCRIBE", phase)
    subtitles = self.translate(subtitles, ctx)
```

`_require()` 在前提缺失时抛 `PorterError`，错误信息明说"哪个阶段没跑"。

**这直接决定了 `--only-phase` 的语义。** 它曾经被实现为"只跑这一个阶段"，而那对除 `prepare` 外的每个阶段都**必然失败**：

```
✗ transcribe failed: phase transcribe requires output from PREPARE,
  which did not run or did not complete
```

现在是"**跑到这个阶段为止**"（`phases_for()` 用切片实现）。`--only-phase burn --burn skip` 会得到一个空集并如实什么都不做，而不是偷偷跑另外三个阶段。想从磁盘恢复单个阶段是另一件事（正是 `force` 为之准备的方向），尚未实现。

### request 的 options 是唯一权威

`run()` 的第一件事是 `ctx.options = request.options`。成因是一个真实的静默错误：`JobOptions` 同时存在于 request（被要求做什么）与 context（正在跑什么）上，而管线从**不同地方**读它们——`phases_for` 读 request，`burn()` 与渲染器读 context。于是一个把两者设得不同的前端会得到"`--burn zh_only` 选中了 BURN 阶段，然后烧错了版本"，而且两边各自都合法所以毫无报错。只有真实端到端测试能抓到它；每个单元测试都传同一个对象，永远看不到。

现在绑定发生在入口处，使两者不一致变成**不可表示**，而不是"不太可能"。

### 失败是数据，不是异常

`run()` 对预期失败从不抛出：

- `JobCancelled` → `JobState.CANCELLED`（CLI 映射为退出码 130）；
- `PorterError` → 发 `PhaseFailed` 事件，返回带 `ErrorInfo` 的 `JobResult`；
- 其他 → `JobState.DONE`。

MCP 前端因此可以把失败当作结构化数据回报给 agent，而 agent 能读懂"哪个阶段、为什么"并改写请求重试。

---

## 4. 端口与适配器

`porter/ports.py` 是管线与其实现之间的缝。管线只依赖这些 `Protocol`，从不依赖具体的 extractor、ASR 后端、翻译器或渲染器——这正是没有网络与 ffmpeg 也能测引擎的原因，也是 GPL-3.0 的 `videocaptioner` 能以"又一个可选实现"的身份待在进程边界后面的原因。

| 端口 | 阶段 | 关键方法 |
|---|---|---|
| `Downloader` | PREPARE（URL） | `can_handle(url)`、`probe(url, ctx)`、`fetch(url, ctx)` |
| `LocalPreparer` | PREPARE（本地文件） | `prepare(path, ctx)` |
| `AsrBackend` | TRANSCRIBE（单个引擎） | `available(ctx)`、`transcribe(audio, ctx)` |
| `Transcriber` | TRANSCRIBE（链） | `available(ctx)`、`transcribe(raw, ctx)` |
| `Translator` | TRANSLATE（链） | `available(ctx)`、`translate(subs, target_lang, ctx)` |
| `Renderer` | BURN | `render(raw, subs, mode, ctx)` |

`Transcriber` 与 `Translator` 是**链**：它们占有回退顺序，并暴露 `available()` 探测供装配步骤剔除不可用后端。它们身后的具体引擎实现的是 `asr/base.py` 与 `translate/base.py` 里更窄的协议。

### 为什么 `LocalPreparer` 是独立端口而不是 `Downloader` 的扩展

因为两者接受的输入不同。`Downloader` 以 URL 为键——`can_handle` 匹配模式、`probe` 解析 URL——而文件系统路径不是 URL：`can_handle("/home/me/video.mp4")` 没有意义，`probe` 还得先回答"这是个文件吗"。

把 `Downloader` 拓宽成接受泛化"source"会为每个平台 extractor 和注册表抹掉这个区别，改动量大，只为了省 `Pipeline` 上的一个字段。而且 `LocalPreparer` **刻意没有 `can_handle`**：`JobRequest` 已经说明了它携带哪种输入，没有什么可猜的。

### 装配：`Pipeline.default()`

```python
Pipeline.default(ctx, *, downloader=None, renderer=None, local=None)
```

`downloader` / `renderer` / `local` 可注入（前端传入已构造的实例，测试传入替身）。`transcriber` 与 `translator` 每次都从 `ctx` 重建。

**方法体内 import 是硬要求。** `porter.pipeline` 被 CLI 的参数解析器引入：

```python
def _default_renderer(ctx: RunContext) -> Renderer:
    from porter.media.burn import FfmpegRenderer
    return FfmpegRenderer(config=ctx.config.ffmpeg)
```

如果这些 import 在模块顶部，`porter --help` 就要付 yt-dlp + openai + subprocess 机械的冷启动成本。

**一个 `__len__` 陷阱值得单独记住**：判空必须写 `is not None`，绝不能用 `or`。

```python
downloader=_default_downloader() if downloader is None else downloader,
```

`PlatformDownloader` 定义了 `__len__`，所以一个**持有空注册表**的 downloader 是 falsy 的，`or` 会静默丢弃调用方传入的实例并构造一个真的——这事真的发生过，只有装配测试抓到了。

### 链的装配顺序

`_default_transcriber()`：

| 顺序 | 后端 | 条件 |
|---|---|---|
| 1 | VideoCaptioner CLI | **仅当** `asr.engine` 是 `bijian` / `jianying` / `whisper-cpp` 之一 |
| 2 | Whisper API | 需要 OpenAI 兼容 Key |
| 3 | Bcut | 免 Key，端点未验证 |
| 4 | Google Web | 免 Key，端点未验证 |
| 5 | VideoCaptioner CLI | 否则作为最后手段 |

CLI 出现两次不是疏漏。v0.1 就是这样：**明确配置的引擎是一个请求，未配置的引擎是最后兜底**。这是用户可见行为，因此保留而不是"顺手整理"。

`_default_translator()`：LLM → Bing → Google → MyMemory → VideocaptionerLLM → Videocaptioner。LLM 第一，因为它是唯一带上下文翻译的引擎，而质量正是用户配 Key 的理由；免 Key 端点按便宜程度排；GPL 的 CLI 最后。链自身的 CJK 自检作用于**任何**实际运行的引擎，这才是排序安全的原因：一个把输入原样回显的免 Key 端点会被拒绝，然后换下一个，而不是产出一份标着"中文"的英文双语字幕。

### 链语义的三条硬规则

1. **空结果 = 失败。** 配额耗尽通常返回 HTTP 200 + 零条 cue；当成成功就会写出空字幕并报告 DONE。`AsrChain._run()` 显式检查 `if not items`。
2. **取消不是后端失败。** `JobCancelled` 在 `except` 之前重抛，否则用户按下取消后还要再等 4 次网络往返。
3. **`available()` 只是参考。** 它探测廉价的本地事实，无法预测远端 429，所以链容忍"声称可用但实际失败"的后端。链用 `_probe()` 包裹它：后端若违反"`available()` 永不抛"的契约，记一条 error 级日志（含 traceback，不吞），跳过这一个，其余四个照常工作。

---

## 5. stdout 纪律

**引擎内零 `print()`。** 由 ruff 规则 `T20` 强制，只有 `src/porter_cli/**` 被豁免。理由不是风格：MCP over stdio 的 stdout **就是** JSON-RPC 通道，任何一行杂输出都会让客户端在协议解析时断开连接，而故障现场离成因很远，调试代价很高。

配套的三层防御：

1. **引擎自身**：`porter/logging.py` 的 `configure()` 只往 `sys.stderr` 装 handler（默认参数就是 `sys.stderr`，换流只应在测试里做）。引擎根 logger `porter` 的 `propagate = False`，因此引擎记录永远不会到达宿主应用的全局 root logger，也就不会被宿主的日志配置复制到 stdout。
2. **依赖**：yt-dlp 的 `quiet=False` 会把下载进度写进 **stdout**。所有 `YoutubeDL` 实例必须经 `platforms/ydl.py::build_ydl()` 构造，它统一设置 `quiet=True` / `noprogress=True` / `no_warnings=True`，并把 yt-dlp 输出经 `_YtDlpLoggerAdapter` 转进 `logging`。
3. **前端边界**：`porter_mcp/stdout_guard.py` 兜住前两层管不到的东西——某个传递依赖里的 `print()`。它把工具体内的 stdout 替换成 `_GuardedStdout`，**把写入重定向到 stderr** 并记录下来；生产模式（`strict=False`）服务器存活、缺陷仍然可见，测试模式（`strict=True`）立即抛 `StdoutViolation`，让 CI 在那一行失败。只有 tool body 被守护，绝不包住 server loop——FastMCP 需要直接访问真实 stdout 才能写协议帧。

重定向的目标**必须是 stderr**，不能是"被替换掉的那个 stdout"：写回 stdout 会同时废掉守卫本身并污染协议流。

日志记录在 stderr 上并不意味着随便打。`get_logger("asr.bcut")` 解析为 `porter.asr.bcut`；子 logger 必须 `propagate = True`。早期版本在**每个** logger 上都设了 `propagate = False`，切断了子 logger 与引擎根 handler 的联系，于是 `configure()` 看起来能用、实际没有任何记录到达它，`--log-level` 完全失效。

---

## 6. 作业注册表（`src/porter/jobs/`）

### 为什么存在

MCP tool call 会在数十秒到数分钟内超时，而压制一个 1080p 视频要数十分钟。所以长任务被建模成**作业**：`start` 立即返回，`status` 廉价轮询，`result` 在终态后取产物，`cancel` 翻转 `RunContext.cancel`。

而"轮询"这件事决定了它**不能是进程内状态**。第一版的 docstring 写的是"进程内状态，所以新进程永远看不到任何作业"——那等于做一个看起来有用、实际永远空的命令。用户选择的方案是持久化磁盘注册表。

### 两层，且注册表是投影

| 层 | 模块 | 回答的问题 | 权威性 |
|---|---|---|---|
| live | `jobs/store.py` | 这个进程现在在跑什么 | 对拥有者权威：内存、线程安全 |
| durable | `jobs/records.py` | 跑过什么 | 共享 JSON 文件，供另一个终端或重连的 MCP 客户端读 |

方向是 **store → registry**，反向会错：如果注册表是权威，每个进度事件都要变成一次带锁的 read-modify-write，而轮询方每几秒才读一次。因此发布按 `PUBLISH_INTERVAL_SECONDS = 2.0` 节流。

### 磁盘契约

- 路径：`platformdirs.user_cache_dir("porter", appauthor=False) / "jobs.json"`（受 `XDG_CACHE_HOME` 影响）。
- 锁文件：同目录下的 `jobs.lock`。
- 文档：`{"version": 1, "jobs": [...]}`；`SCHEMA_VERSION = 1`，版本不符**直接忽略而不是迁移**——它是缓存，丢失的代价只是"想不起来那个作业叫什么"。
- 保留 `MAX_RECORDS = 200` 条，裁剪时**永不丢弃未完成的记录**。

### 原子写

`_write_unlocked()` 写同目录临时文件 `.{jobs.json}.tmp`，再 `os.replace()` 换到位。`os.replace` 只在同一文件系统内原子，所以临时文件必须是**同目录的兄弟**而不是 `/tmp`。写入用 `ensure_ascii=True`，这样在 ASCII locale 下文件依然合法。

读取侧对**损坏或不可读的文件一律当作空**：一个坏掉的缓存永远不该让 CLI 无法工作。

### 文件锁

`_exclusive_lock()` 是一个 contextmanager，在 `jobs.lock` 上取 `fcntl.flock(LOCK_EX)`（Windows 走 `msvcrt.locking`）。它是**建议锁**，但使用它的集合恰好就是全部写者。两者都不可用时锁退化为空操作：在奇异环境下丢一次更新，好过完全拒绝记录任何东西。锁文件打不开（例如只读缓存目录）同样降级为警告 + 继续。

### PID 陈旧检测：PID 不是身份

内核会回收 PID，所以一条写着"owner 是 PID 4321"的记录可能悄悄开始指代一个毫无关系的进程。`process_marker()` 因此记录 `(pid, start_time)`：

- 启动时间取自 `/proc/<pid>/stat` 的**第 22 个字段**（自启动起的时钟滴答数）；
- 第 2 个字段是命令名，**自身可能含空格和括号**，所以字段切分在最后一个 `)` 之后进行，索引取 19；
- `/proc` 不可用时启动时间返回 `None`，调用方退回纯存活检查。

`_owner_is_alive()` 优先比较启动时间；拿不到时才用 `os.kill(pid, 0)`（`PermissionError` 视为"活着且不是我们的"）。

`JobRecord.reap()` 把一个"owner 已死"的记录标成 `FAILED`，并写一条能读懂的错误。被 `kill -9` 的进程无法写自己的讣告，一条永远停在 `running` 的记录是**轮询客户端会一直等下去的谎话**。读取时 `reap=True` 才会把修正持久化——普通读取不写盘，这样只读挂载或只读缓存目录依然可用。

### 看门狗线程：为什么不是事件 sink

这是本节最重要的一处设计。**第一版是通过事件 sink 观察取消的**：sink 已经存在、每次阶段变化都跑、不需要额外线程，看起来完全合理。

它实际在长下载期间**完全无效**。下载一个视频的整个过程中，**一个事件都不产生**，而那恰恰是用户最想取消的窗口。一个只断言"CLI 写入了标志"的测试会一直通过。

所以现在是**专用看门狗线程**，每 `CANCEL_POLL_SECONDS = 1.0` 查一次注册表：

```python
def watch() -> None:
    while not job._watchdog_stop.wait(CANCEL_POLL_SECONDS):
        if job.cancel.is_set():
            return
        if self._registry.is_cancel_requested(job.job_id):
            job.cancel.set()
            return
```

`JobStore.attach()` 在管线启动前把 `ctx.cancel` 交给 job、用一层 sink 包住 `ctx.events`（追加进 `deque(maxlen=200)` 的回放缓冲并节流发布），然后启动看门狗。

不用信号的原因：请求方得给一个可能已被回收的 PID 发信号，而且 MCP 服务端拥有的作业与终端拥有的作业会走不同的代码路径。

### 已知语义（有意如此）

取消是**协作式**的，落在**阶段边界**：

- 一个取消请求可能在状态仍为 `running` 时被观察到——`porter jobs status` 区分"已请求"与"已停止"；
- 在**最后一个**被请求的阶段期间取消，结果是 `done` 而非 `cancelled`——用户要求的活确实干完了，把已完成的作业报成取消是更糟的错误；
- 长下载中途取消，要等下载结束才停。这是阶段边界的直接后果，不是缺陷。

---

## 7. 媒体层（`src/porter/media/`）

### 唯一的 ffmpeg/ffprobe 出口

`FFmpegRunner` 是引擎里**唯一** spawn 子进程的地方。所有调用（`probe.py`、`standardize.py`、`enhance.py`、`burn.py`、`encode.py` 的试编码）都经由它。

`FFmpegTools.resolve()` 解析两个可执行文件：配置的绝对路径存在就用它，否则查 PATH，最后回退到裸名字——这样 `FileNotFoundError` 里出现的是工具名而不是空字符串。`require()` 抛 `CapabilityMissingError`，其意义是**在长作业开始前就报错**，而不是等到 BURN 阶段才发现 ffmpeg 不存在。

### 两条进程安全规则

**规则一——绝不让 ffmpeg 碰 stdin。** argv 里加 `-nostdin`，同时 `subprocess.run(..., stdin=subprocess.DEVNULL)`。

值得强调的是这条的诚实定性：它**不是**在修一个线上 bug。实测 ffmpeg 用 `tcgetattr(0)` 决定是否读 fd 0，管道上该调用会失败，所以在 Linux + 管道 stdin 下它本来就不读。保留它是因为：该行为是 POSIX 专有的（Windows 没有 `tcgetattr`）；MCP 客户端可能给的是 pty 而非管道；`-nostdin` 是 ffmpeg 自己文档给非交互调用方的建议。两条保险，代价是一个 flag。**它是纵深防御，不是正在修的实时缺陷**——这个区别在读提交历史时很重要。

**规则二——报告 stderr 的尾部，并且总是报告。** v0.1 做的是 `proc.stderr[:200]`，把有用的部分截掉、留下版本号。现在：

```python
_STDERR_TAIL = 2000

def _tail(text, limit=_STDERR_TAIL):
    cleaned = text.strip()
    if len(cleaned) <= limit:
        return cleaned
    return f"...{cleaned[-limit:]}"
```

`MediaError` 带着这段尾部、退出码、完整 argv 与 `cwd` 一起抛出。`cwd` 被记录是因为 filtergraph 里的失败常常是"相对路径解析到哪"的问题，而不是路径文本的问题。

### 编码与解码：显式 utf-8 + `errors="replace"`

`subprocess.run()` 的所有调用点都显式写：

```python
capture_output=True, text=True, encoding="utf-8", errors="replace"
```

这一条来自一类真实 bug（`REFACTOR_PLAN.md` §13.17）：subprocess 的文本解码默认走 **locale 编码**，在 `POSIX`/`C` locale 下是 ASCII，于是任何非 ASCII 路径、标题或 ffmpeg 输出都会让解码抛 `UnicodeDecodeError`——而异常发生在读结果的时候，看起来像是"命令失败"。`errors="replace"` 是第二道：即便字节真的不是合法 UTF-8，也应该降级为替换字符，而不是让整个作业死在读取一行日志上。

同一个理由出现在所有 `Path.read_text()` / `write_text()` 上：引擎里没有一处省略 `encoding=`。

### 硬件档位：试编码，而不是设备路径

`encode.py` 用**实际编码一帧**判断硬件编码器能不能用：

```
-f lavfi -i color=c=black:s=256x144:d=0.1:r=1 -frames:v 1 <profile args> -f null -
```

`TRIAL_FRAME_SIZE = "256x144"`——小到几百毫秒，大到真实编码器会在管线搭不起来时报错；尺寸选偶数是为了避免 yuv420p 对齐警告混进探测结果。输出到 `-f null -` 丢弃，不落盘。

v0.1 的档位表错在两点：

1. **编译进去 ≠ 能跑。** `ffmpeg -encoders` 列出的是构建支持，不是机器能力。CI 容器、驱动没加载的笔记本、WSL2 客户机都会列出 `h264_nvenc` 而无法加载 `libcuda`。
2. **设备路径也是错误的问题。** 开发机上既没有 `/dev/dri` 也没有 `/dev/nvidia*`，NVENC 却完全可用——WSL2 经 `/dev/dxg` + `/usr/lib/wsl/lib/libcuda.so` 访问 GPU。任何基于路径的探测都会把这台机器读成"软件档"，白扔一个实测 **1.46×** 的 1080p30 编码加速。

而失败方式才是必须修的真正理由：**误判不会在发生时被报告**。ffmpeg 带着 `-c:v h264_nvenc` 跑起来，错误直到作业**结束时**才以 `Cannot load libcuda.so.1` 的面目出现——下载、转录、翻译全都白做。

`EncoderSelector.select()` 返回第一个试编码成功的硬件档，否则返回 `software_profile_for(cpu_count)` 的软件档。它**总是返回一个可用 profile**，所以调用方永远不需要"没有编码器"的分支，也就不存在"跑了一小时才发现编不了"的路径。软件档不试编码（`libx264` 不会因为缺设备失败），其 preset 由 CPU 核数决定（`_FAST_CPU_CORES = 8`）——那正是旧的 Tier B/C 分割里唯一站得住的部分。

两个刻意的例外：**软件回退**不探测；**VAAPI 保留设备路径检查**，因为 VAAPI 的接口**就是**由设备节点定义的，`-vaapi_device` 必须指向一个存在的节点，否则 ffmpeg 在编码前就报错。这是一次**快速的、诚实的拒绝**，而不是"GPU 可用"的证据。`device` 与 `device_flag` 成对存放，同样是为了让存在性检查和那个 flag 无法漂移——第一版设了 `device` 却没设 flag，结果 VAAPI 看起来可选而每次试编码都失败。

硬件档位按"平台实际存在的可能性"排序，所以常见情况只需一次试编码而不是四次。`HardwareTier` 保留 v0.1 的 `HARDWARE` / `SOFTWARE_FAST` / `SOFTWARE_SLOW` 命名，因为名字已经进了配置文件和运维词汇；用 `(str, Enum)` 而非 `StrEnum` 是为了兼容 Python 3.10。

### 成片是原子输出的

`burn_hardsub()` 先写到目标**旁边**的临时文件 `.tmp_<输出文件名>`，**探测确认可读后**才改名到位：

```
写 .tmp_video_bilingual.mp4 → probe() → Path.replace() 到位
                          ↘ 失败：unlink 临时文件
```

v0.1 是无条件改名，于是一个被截断的编码会被当作成品发布。这正是质检步骤要跑 `ffprobe` 判定的原因。

### 路径不进 filtergraph

`burn.py` 的做法是把子进程的 `cwd` 设成字幕所在目录，filtergraph 里只写**裸文件名**：

```python
cwd=subtitle.parent
```

理由是实测的：filtergraph 解析器把 `'` 当引号定界符，**没有任何写法能表达一个真正的单引号**——无转义、一层反斜杠、两层三层、shell 的 close-escape-reopen 形式、percent-encoding，全都试过并失败。所以任意用户输出路径根本不能进 filtergraph 参数。

`escape_ffmpeg_filter_path()` 仍然导出，因为它是记录在案的契约（见 `tests/regression/README.md`），但它只对 ffmpeg 允许转义的字符正确；`_escape_filter_text()` 与它分开，因为两者的语境不同。`FFmpegRunner.run(cwd=...)` 的存在就是为了这个用法，`MediaError` 里记录 `cwd` 也是。

### 其余模块

| 模块 | 职责 |
|---|---|
| `probe.py` | ffprobe 的类型化视图。未知尺寸保持 `None`，而不是变成 `1920x1080`——v0.1 的"猜测"正是竖屏视频按横屏边距渲染的原因 |
| `standardize.py` | 下载文件 → `raw/video.mp4` + `raw/audio.wav`。可流式复制的源走 `-c copy`，否则按 `FFmpegConfig` 转码（见下） |
| `enhance.py` | `raw/audio.wav` → `raw/audio_enhanced.wav`，**仅供 ASR**。成片音频走 `-c:a copy`，所以识别准确率与成片音质可以兼得 |
| `prepare.py` | URL 与本地文件两条 PREPARE 路径共用的步骤（音频提取、增强、抽封面帧、尺寸回填） |
| `encode.py` | 见上 |

`FFmpegConfig` 的 `video_codec` / `preset` / `crf` / `pixel_format` / `audio_codec` / `audio_bitrate` / `audio_sample_rate` 只在 **standardize 的转码分支**生效，不作用于 BURN——BURN 的编码器完全由试编码决定。`ffmpeg_path` / `ffprobe_path` 由 `FFmpegTools.resolve()` 消费，`wav_sample_rate` 由 `prepare.py` 消费。

---

## 8. 字幕获取策略

TRANSCRIBE 有两条路，由平台规格决定。

### `SubtitleSource` 的两个标志

`platforms/spec.py` 里的 `SubtitleSource` 只有两个字段：

| 字段 | 默认 | 含义 |
|---|---|---|
| `remote` | `True` | yt-dlp 是否自己抓字幕轨。**`False` 意味着完全不抓**——`plan_subtitles()` 直接返回空计划。它不是"换个方式抓" |
| `prefer_existing_chinese` | `True` | 平台自带中文轨时直接复用，省掉整轮 LLM 翻译 |

`remote=False` 语义被写进 docstring 是有原因的：把它误读成"换个方式抓"正是本节末尾那个 bilibili 缺陷的成因。

各平台的实际声明：

| 平台 | `remote` | `prefer_existing_chinese` | 备注 |
|---|---|---|---|
| YouTube | `True` | `True` | 人类字幕 + 自动字幕；需要外部 JS 运行时 |
| Bilibili | `True` | `True` | CC 轨以 JSON 返回；匿名下 yt-dlp 拿不到，带 cookie 可能可以 |
| TikTok | `True` | `False` | 元数据常缺尺寸，默认竖屏 |
| X / Twitter | `False` | `False` | 没有可用轨 |
| Instagram | `False` | `False` | 没有可用轨 |

`local`（本地文件）不声明 spec；它旁边的 sidecar `.srt` **不被接管**，因为歧义太大。

### 抓取流程

`YtDlpExtractor.fetch()` 里的顺序是：`plan_subtitles()` → `_download_subtitles()` → 媒体 → 音频 → 封面 → metadata。

`plan_subtitles(info)` 从 `info["subtitles"]`（人类）与 `info["automatic_captions"]`（自动）里按**每平台的共享优先级表**选源语言，返回 `{目标文件名: (语言标签, 是否自动)}`。`prefer_existing_chinese` 为真时再为 `subtitle_zh.srt` 选一条中文轨（且不与源语言重复）。

`_download_subtitles()` 对计划里的每一项单独起一个 yt-dlp 调用，写进 `.tmp/subs/<lang>/`：

```python
sub_policy = replace(
    policy,
    skip_download=True,
    write_subtitles=not is_auto,
    write_auto_subs=is_auto,
    subtitle_langs=(lang,),
    outtmpl=str(scratch / "sub.%(ext)s"),
    ignoreerrors=True,
)
```

`YdlPolicy.subtitle_format` 是 `"srt/vtt/json/best"`——`json` 在列表里是因为 bilibili 的 CC 轨只以那个容器提供；不显式请求它，等于请求平台根本没有的容器，轨会被跳过。v0.1 的 `subtitlesformat` 也是这个值。

### 平台轨是"请求"而非"保证"

这是本策略最重要的一句话，并且它在代码里有三层体现：

1. **抓取整体是 best-effort**：`_download_subtitles` 除了 `JobCancelled` 之外不抛任何异常，只记 warning 然后 continue。
2. **文件查找按实际命名**：`_find_subtitle_file()` glob `sub.*`（因为 yt-dlp 会把语言标签插进文件名，写 `sub.en.srt` 而不是 `sub.srt`），按 `srt > vtt > json` 偏好返回，并**跳过空文件**。
3. **消费侧是全集函数**：`platform_subs.load_platform_subtitles()` 对 `None`、`OSError`、空文本、解析不出 cue 的情况一律返回 `[]`。缺轨是正常结果，不是错误——抛异常会把"没有字幕"变成"任务失败"。

平台轨存在时它**优先**：那是作者的原文，拼写、标点、专有名词都正确，而且免费。同段音频跑 ASR 得到的是错名字和没有标点。`used_asr=False` 会被如实记录，因为"这个作业到底付钱跑了 Whisper 吗"是运维会问的问题。

还有一处容易忽略的收益：平台自带的中文轨在 **TRANSCRIBE** 阶段就被对齐到 cue 上（`_align_platform_chinese()`），而不是留给 TRANSLATE。它是 PREPARE 抓来的**源材料**，不是某个引擎的产物。TRANSLATE 于是看到已有目标文本的 cue，直接跳过所有后端。

### 两类真实缺陷（值得作为教训记住）

**其一：字段和函数都在，但没人调用。** Bilibili 曾经声明 `SubtitleSource(remote=False, ...)` 并注释"CC 轨走 HTTP 抓取"。但没有任何代码实现那条 HTTP 路径，而 `remote=False` 让 `plan_subtitles` 永远返回空计划。于是 bilibili **即便配了 cookie 也永远用不上自己的精确中文 CC 轨**，每个作业都白付一次 ASR——一块独立看完全正常的死代码：转换器 `bilibili_json_to_srt` 有测试、spec 有注释、字段有 docstring，缺的是"谁调用它"。修复需要**三处同时改**：`remote=True`、`_download_subtitles` 认得 `.json` 并调用转换器、`subtitle_format` 显式包含 `json`。只改前两处，轨依然拿不到。

**其二：所有平台的字幕抓取从未生效。** `_download_subtitles` 用 `outtmpl="sub.%(ext)s"` 然后去找字面名 `sub.srt`，而 yt-dlp 写的是 `sub.en.srt`，所以**每个有字幕轨的平台上它都找不到文件**。三层遮蔽让它活了很久：缺轨是 best-effort 设计所以只留一行没人读的 warning；单元测试的假 yt-dlp 恰好写 `sub.srt`——**测试替身比它替代的东西更宽容**，两边一致地错着；之前的修复验证又恰好在匿名 bilibili 上做，那本来就没有轨。修法里最关键的一步是**把假 yt-dlp 改成写真实文件名**，不改它，测试永远发现不了这个 bug。

结论写进策略：**计划报告 `tracks` 时说的是"将被请求"的轨，不是"保证拿到"的轨**，并说明抓取失败会回落到 ASR、平台会对这类抓取限流（429）。

### 平台注册表

`platforms/registry.py` 里是**显式的字面列表**，不是 import 副作用，也不是装饰器：

```python
for module in (bilibili, x, instagram, tiktok, youtube):
    _REGISTRY.register(YtDlpExtractor(module.SPEC))
```

- **幂等**：`register_builtins()` 由模块级标志守卫，第二次调用是 no-op。
- **顺序有意义**：它是"多个模式都匹配同一个 URL"时的决胜规则。YouTube 排最后，因为它的模式最宽。
- **可枚举**：`registry()` 每次访问都会重申注册，所以不需要先 import 任何平台模块就能列出平台——这是 v0.1 做不到的（旧版靠 `@register_extractor` 装饰器的 import 副作用往模块级可变 list 追加，导入顺序成为隐式契约，且 `porter doctor --list-platforms` 无从实现）。

URL 解析按插入顺序遍历 `handler.can_handle(url)`。每个平台不再是一个 YtDlpExtractor 子类，而是 `YtDlpExtractor(module.SPEC)`——行为在 `base.py` 里只有一份，平台之间的差异被收敛成 `PlatformSpec` 里的**数据**（URL 模式、格式选择器、标题清洗、字幕策略、播放器客户端、是否默认竖屏、是否在限流时重试、`notes`）。

---

## 9. 改代码前的检查清单

- 加了新模块，先想它在 §2 的哪一层，再看 `lint-imports` 是否通过（**故意写一行违规 import 验证 gate 真的在看**）。
- 新代码不得 import 前端、不得 `print()`、不得假设部署布局（§1）。
- 具体实现不要放在模块顶层 import；贵的依赖放进 `Pipeline.default()` 之类的装配函数体。
- 任何 subprocess 调用必须走 `FFmpegRunner`/`build_ydl`，不得自己拼 `subprocess.run`——`-nostdin`、utf-8 解码、stderr 尾部这三条只在那两个出口被统一保证。
- 新建文件写入必须显式 `encoding="utf-8"`；面向其他进程的文件（注册表）必须原子写。
- 用户可见的失败要变成数据（`PorterError` + `ErrorInfo`），不要变成栈回溯。
- 期望失败的异常要在**发生处**变成 warning + continue，而不是层层上抛；反之，能确定走不通的情况要在作业开始前抛 `CapabilityMissingError`，不要等跑完一小时。
- 文档与本文件冲突时改本文件，不要留两处说法。

## 10. 已知缺口

- **单阶段从磁盘恢复已实现**（§13.51）：`--only-phase X` 的前置阶段照跑，但 PREPARE（母版）、TRANSCRIBE（源 cue）、BURN（成片）各有复用判定，所以昂贵的工作会被跳过（实测：第二次运行 `reusing 19 cached source cues`，ASR 归零）。**TRANSLATE 没有缓存**：它的 `.ass` 产物与字幕样式耦合，需要一套指纹方案才能安全复用，因此重复运行会重新翻译。`--force` 关闭全部复用。
- **免 Key 的 ASR 端点已实测失效**（`bcut` / `google-web`）；但 **`[asr-local]` 提供了免 Key 的本地识别**（§13.48），所以"没有 Key 就必然失败"已不成立——除非本地后端也没装。
- **无字幕轨的视频有明确出口**：既无平台轨又无可用 ASR 时作业在 TRANSCRIBE 失败，失败信息会指向 `--subtitle-file` 或 `[asr-local]`（§13.51）。
- **MCP sampling 翻译未实现**：用宿主模型做零 Key 的 LLM 级翻译。
- **本地视频的 sidecar `.srt` 不被接管**（刻意，§13.29）：一份 `.srt` 可能是源字幕也可能是译文字幕，猜错会静默跳过 ASR 或覆盖用户文件。**要接管就显式点名：`--subtitle-file`。**
- `asr.audio_denoise`、`ffmpeg.auto_tune` 两个配置键没有任何消费者（只有 `JobOptions.audio_denoise` 生效）；`cookies_file` / `cookies_browser` 的**配置值只对 MCP 的 `porter_inspect` 路径生效**（`porter_mcp/tools/inspect.py`），`porter run` 只认命令行旗标。细节见 `docs/CONFIG.md` §4.6 与 §6。
- 若加了 cookie 之后 yt-dlp 是否真能拿到 bilibili 的 CC 轨，**未经验证**。当前修复的作用是"把路打开"。
