# 配置解析

完整的配置键、默认值和示例在
[`skills/porter-skill/references/CONFIG.md`](../../skills/porter-skill/references/CONFIG.md)。
本页只讲**值从哪里来**以及如何排查。

## 优先级

从高到低：

1. **显式路径**：`--config <path>`。文件不存在会报错，不会静默回退。
2. **`$PORTER_CONFIG`**：同样要求文件存在。
3. **环境变量**：覆盖所选文件里的对应键（见下表）。
4. **项目配置**：从当前目录向上查找，文件名优先级为
   `porter.json` → `porter.toml` → `config.json` → 旧名 `porter_config.json`。
5. **用户配置**：`platformdirs.user_config_dir("porter")` 下的配置文件。
6. **内置默认值**。

注意：第 4 和第 5 是**二选一**，不是深合并。找到第一个存在的项目配置后，
用户配置不再参与；环境变量再覆盖该文件中的具体键。`porter config list`
的 `source` 字段会说明实际使用的文件。

## 环境变量

| 变量 | 覆盖 |
| --- | --- |
| `OPENAI_API_KEY` | `llm.api_key`，并作为 `asr.whisper_api_key` 的兜底 |
| `OPENAI_BASE_URL` / `OPENAI_API_BASE` | `llm.api_base` |
| `OPENAI_MODEL` / `PORTER_LLM_MODEL` | `llm.model` |
| `WHISPER_API_KEY` | `asr.whisper_api_key` |
| `WHISPER_API_BASE` | `asr.whisper_api_base` |
| `WHISPER_MODEL` | `asr.whisper_model` |
| `PORTER_ASR_ENGINE` | `asr.engine` |
| `PORTER_ASR_LOCAL_MODEL` | `asr.whisper_local_model` |
| `PORTER_ASR_LOCAL_DEVICE` | `asr.whisper_local_device` |
| `PORTER_ASR_LOCAL_COMPUTE_TYPE` | `asr.whisper_local_compute_type` |
| `PORTER_OUTPUT_DIR` | `output_dir` |
| `PORTER_CONFIG` | 显式配置文件路径 |
| `PORTER_LOG_LEVEL` | CLI 日志级别（仅当未传 `--log-level`） |
| `PORTER_MIRROR` | 下载镜像策略：`cn` 强制启用、`off` 强制关闭 |
| `PORTER_CACHE_DIR` | 作业注册表所在目录（受限宿主/测试用） |

密钥放环境变量比放配置文件安全：配置文件可能被提交进仓库。MCP 的
`porter_config` 只读且密钥打码，**不能通过 MCP 写入密钥**。

## `porter config`

```bash
porter config list                 # 有效配置 + source，密钥打码
porter config get llm              # 查看某一段
porter config get llm.model
porter config set llm.model=deepseek-chat
porter config set llm.api_key=sk-... --file ./local.json
porter config path                 # 当前生效的文件
```

`set` 默认写**用户级**配置，而不是最近的项目配置；这是刻意的，避免在别人的
仓库里留下修改。`set` 和 WebUI 只写 JSON；TOML 配置可以读取，但不支持自动
写入，请手工编辑。

## 配置文件格式

JSON 与 TOML 都支持；扩展名决定解析器。最小示例：

```json
{
  "output_dir": "./porter_output",
  "llm": {
    "api_key": "sk-...",
    "api_base": "https://api.deepseek.com/v1",
    "model": "deepseek-chat"
  },
  "asr": {
    "engine": null,
    "audio_denoise": true
  }
}
```

完整模板见
[`skills/porter-skill/assets/config.example.json`](../../skills/porter-skill/assets/config.example.json)。

## 配置错误

解析失败时 CLI 以非零退出码报错，并说明是哪个文件、哪一行；不会静默使用默认值
继续运行。未知键被忽略（模型配置为 `extra="ignore"`），因此升级不会因为旧键
而中断，但也意味着拼写错误不会报错——用 `porter config list` 确认实际生效的值。
