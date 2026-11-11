# 媒体层

`core.media` 是引擎与 FFmpeg 之间的唯一边界。任何模块都不直接拼 ffmpeg 命令。

## `FFmpegRunner`

`core/media/ffmpeg.py` 是**唯一**启动 `ffmpeg`/`ffprobe` 子进程的地方。两条进程
安全规则在这里统一执行，因为它们在 v0.1 都静默出现过：

1. **`-nostdin`。** 否则 ffmpeg 会读走调用者的 stdin，在 MCP/后台场景下表现为
   进程卡住或输入被吞。
2. **stderr 只保留尾部。** ffmpeg 的错误信息很长，完整的输出会淹没真正的原因；
   runner 保留末尾若干行并在失败时附上。
3. **显式 UTF-8 + `errors="replace"`。** 非 ASCII 路径在 ASCII locale 下会炸；
   这是环境问题，不应该让作业失败在解码上。

## 标准化（PREPARE）

`standardize_video()` 把下载到的任意文件变成 `raw/video.mp4`：

- 视频流复制（`-c:v copy`）优先，只有不兼容时才转码；
- 音频统一为 AAC 44.1 kHz；
- `+faststart`，便于网页播放；
- 输出必须是**恰好一个视频流 + 一个音频流**；
- 探测确认结果可读后才算完成。

`extract_audio()` 生成 `raw/audio.wav`：16 kHz、单声道、s16le，供 ASR 使用。
`enhance_for_asr()` 再生成 `raw/audio_enhanced.wav`，滤镜链为
`highpass=f=80,lowpass=f=8000` 等广播风格处理。**增强音轨只给 ASR**，从不进入
成片；成片音频直通复制（`-c:a copy`），因此识别准确率和成片音质可以兼得。

## 编码器选择：试编码

`core/media/encode.py` 不读 `ffmpeg -encoders`，也不看设备节点，而是**真的编码
一帧 256x144 黑帧**：

```
h264_nvenc / hevc_nvenc / h264_qsv / h264_amf → libx264
```

原因：

- `-encoders` 只说明“这个构建支持”，不说明“这台机器现在能跑”。CI 容器、驱动
  未加载的笔记本、WSL2 客户机都可能列出 `h264_nvenc` 却加载不了 `libcuda`。
- 设备路径也不是正确答案：开发机既没有 `/dev/dri` 也没有 `/dev/nvidia*`，
  却可以通过 WSL2 的 `/dev/dxg` 使用 NVENC。路径探测会把这种机器误判为低档，
  白白损失实测 1.46 倍的 1080p30 编码加速。
- 误判不会当场报错，而是在作业末尾以 `Cannot load libcuda.so.1` 出现——
  此时下载、转录、翻译都已经白做。

两个例外：

- **软件回退 `libx264`** 不需要探测，preset 按 CPU 核数选；
- **VAAPI** 确实按设备节点寻址，`-vaapi_device` 必须指向存在的节点，因此保留
  路径检查，但它是“快速、诚实地拒绝”，不是“证明 GPU 可用”。

## 压制（BURN）

`core/media/burn.py` 从母版生成两个发布视频：

```
cooked/video_bilingual.mp4   ← subtitle_bilingual.ass
cooked/video_zh.mp4          ← subtitle_zh.ass
```

### 路径转义问题

v0.1 把字幕的**绝对路径**拼进 filtergraph 并转义。实测无效：filtergraph 解析器
把 `'` 当引号分隔符吃掉，包含撇号的路径会变成另一个路径。而
`sanitize_filename` **保留撇号**，所以标题 “It's a Wonderful Life” 会产生
`..._It's_a_Wonderful_Life/` 目录，每次压制都失败。

解决办法不是转义，而是**消除问题**：子进程的 `cwd` 设为字幕所在目录，
filtergraph 只写裸文件名（`subtitle_zh.ass`，常量、无特殊字符）。用户的输出
目录从不进入 filtergraph。`escape_ffmpeg_filter_path()` 仍保留，用于它能处理的
字符（如 Windows 盘符冒号），并明确标注它不是本机制的核心。

### 原子产物

压制先写到目标旁的临时文件，`ffprobe` 确认可读后才改名到位。v0.1 无条件改名，
截断的编码会被当成成品发布；质检步骤因此必须跑 `ffprobe`。

## 与管线的关系

管线只通过 `Renderer` 端口调用 BURN；媒体层的具体实现由
`Pipeline.default(ctx)` 组装。`--burn skip` 时 BURN 阶段根本不进入阶段列表，
因此不需要 libass 能力探测。
