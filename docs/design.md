# 设计思路与架构权衡

这份文档用来解释 [`architecture.md`](architecture.md) 背后的取舍——不是条款清单，而是记录「为什么做成现在这样，而不是另一种常见做法」。

---

## 1. 为什么收敛为「单核引擎 + WebUI + 自包含 Skill」

早期版本同时在仓库里维护了 CLI、MCP Server、WebUI 和 Agent Skill 四个入口。实际迭代中，这带来了两个问题：

- **同一逻辑多处漂移**：参数默认值、cookie 兜底、画质选项、配置读写很容易在不同入口里各写一份，修一个 bug 要改三四个地方。
- **入口定位重叠**：本地视频处理以长耗时批处理为主，不像 Blender / DaVinci 那样需要高频细粒度 GUI 操控，MCP 协议层反而显得冗余；而对 AI Agent 来说，直接调用 PyPI 核心包的独立 JSON 脚本更轻量，也避免了把 CLI 当成核心包的一部分长期背负。

因此 `v0.4.0` 把所有业务逻辑收拢到 `porter.core` 门面（`plan_source`、`run_plan`、`run_doctor` 等）：
- **人**用可选的 Gradio WebUI 工作台（`porter-workflow[web]`）；
- **Agent** 用自包含的 [`skills/porter-workflow/`](../skills/porter-workflow/SKILL.md)，通过内置纯 JSON 脚本直接调用 PyPI 上的 `porter-workflow`；
- **开发者/脚本**直接 `import porter`。

修复链路变成单向的 **Core → WebUI / Skill**，从源头避免多端行为漂移。

---

## 2. 为什么「先 Plan 后 Run、每阶段单一能力」

很多音视频工具喜欢在内部做静默回退链：平台字幕拉不到就悄悄切 Whisper，免费翻译挂了就悄悄调 LLM API，画质选不到就闷声下 360p。这在人工点鼠标时看似方便，但在自动化和 Agent 场景下非常危险：

- 用户或 Agent 事先不知道会不会花钱（调用付费 API）、会不会把音频/文本外发、会不会首次下载近 500 MB 本地模型；
- 一旦运行失败，日志里夹杂多层回退痕迹，很难定位到底哪一步出了问题。

Porter 的做法是把**决策期**和**执行期**拆开：
- `plan_source()` 先探测源，为每个阶段锁定**恰好一个能力**并给出结构化 `WorkflowPlan`（含成本、数据外发方向与风险提示，例如画质不足时的 `QUALITY_DEGRADED`）；
- `run_plan()` 只按计划执行，同能力内可做容错重试（如批量翻译拆句），不做跨能力静默切换；若失败，返回具体的 `alternatives` 与 `suggested_workflow`，把「换路线」的决定交还给调用方重新 `plan`；
- 付费能力默认关闭，环境变量里有 API Key 只算配置就绪，不算授权扣费。

---

## 3. 为什么内置离线 Whisper，唯独把 Gradio 留在 `[web]`

过去把 `faster-whisper`、`openai`、`pillow` 拆成一堆 extras，结果用户和 Agent 光是记装哪几个 `[extra]` 就很容易踩坑。实际上，对一个「视频本地化工作流」而言，下载、离线识别、翻译和封面处理本来就是核心闭环的一部分，打包在一起才能开箱即用。

唯独 `gradio` 体量大、依赖树深，且只有启动浏览器工作台时才需要。把它隔离到 `[web]` 并在 `create_app()` 里延迟导入，既不影响 WebUI 一键安装，也能让 `import porter` 和 Agent Skill 在无界面环境下保持干净轻量。

---

## 4. 为什么媒体与字幕层按当前方式切分

- **FFmpeg 留在子进程边界外**：既避免 GPL/LGPL 构建污染 MIT 核心代码，也能直接利用用户本机装好的硬件加速构建。所有调用收敛在 `FFmpegRunner`，统一处理 `-nostdin` 和 UTF-8 解码，绕开跨平台常见坑。
- **试编码而非查列表**：`ffmpeg -encoders` 会列出驱动未就绪或无对应显卡的编码器；实际编码一帧 256×144 黑帧只需要几十毫秒，却能彻底避免压制中途报错。
- **降噪音轨只给 ASR，不进成片**：高通/低通滤波能明显提升语音识别准确率，但会破坏人声自然度；把 `audio_enhanced.wav` 和母版音轨分开，识别准度和成片听感可以兼得。
- **先写临时文件再原子落盘**：压制耗时较长，半途中断或磁盘满时留下的截断 MP4 不应被后续流程误认为成品，通过 `ffprobe` 校验后再重命名成本很低、收益很高。
- **句子重构 + CJK 自检**：YouTube 自动字幕和 Whisper 原始切分常为 2–3 词碎片，逐条直译语序很差，因此先合并重构整句再翻译和意群断句；而免费翻译端点限流时偶尔返回 HTTP 200 原文回显，加一层中文字符自检能防止产出「双语视频两条都是英文」的假熟肉。

---

## 5. 为什么 WebUI 围绕 4 个 Tab 与跨进程 `JobRegistry` 组织

WebUI 的定位是**可视化与人机协同（HITL）工作台**，而不是独立后台服务：
- 四个 Tab（工作台 / 任务 / 设置 / 体检）分别对应核心门面的计划执行、作业注册表、配置读写与 `run_doctor()`，不复制业务逻辑；
- 作业状态通过磁盘上的共享 `JobRegistry` 同步，因此在 Agent Skill 或脚本里启动的长任务，打开 WebUI 的「任务」页同样能看到进度、取消或回应 `AWAITING_USER` 交互；
- 界面本身用 Gradio 原生组件加轻量中英字典，支持注入 `JobStore` / `JobRegistry`，方便在 CI 里脱离真实 Gradio 做回归测试。
