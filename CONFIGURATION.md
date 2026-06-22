# 配置参考

OmniAgent 支持两种配置方式：**配置文件** 和 **环境变量**。环境变量优先级高于配置文件。

## 配置文件: `omniagent.toml`

在项目根目录放置 `omniagent.toml` 或 `.omniagent.toml`，OmniAgent 启动时自动加载。

### 完整示例

```toml
[llm]
provider = "openai"           # openai / deepseek / ark / ollama / test / 任意 OpenAI 兼容服务
model = "gpt-4o-mini"         # 模型名称
api_base = ""                 # API 地址，留空使用 provider 默认
temperature = 0.0
timeout_seconds = 60
max_retries = 2
fallback_provider = ""        # 主 provider 失败时降级到的 provider
enable_runtime_fallback = true
verify_on_startup = false     # 启动时检查 LLM 可用性

[embedding]
enabled = false               # 默认关闭真实 embedding，使用本地 hash fallback
provider = "local_hash"       # local_hash / openai / local_model / 任意 OpenAI 兼容服务
model = ""                    # 真实 embedding 模型名，如 text-embedding-3-small
model_path = ""               # local_model 使用；目录内需有 model.onnx
tokenizer_path = ""           # local_model 使用；留空默认 model_path/tokenizer.json
api_base = ""                 # API 地址，适配器自动补 /embeddings
# dimensions = 1536           # 可选；仅在 provider 支持 dimensions 参数时设置
pooling = "mean"              # local_model 使用：mean / cls
normalize = true              # local_model 使用：是否 L2 normalize
max_tokens = 256              # local_model 使用：tokenizer 截断长度
timeout_seconds = 30
max_retries = 1

[gateway]
app_name = "omniagent"
default_mode = "semi_auto"    # assist / semi_auto / full_auto
task_timeout_seconds = 30     # 默认任务超时（秒），CLI --timeout 可覆盖

[sandbox]
enabled = true
timeout_seconds = 30
allow_network = false
# authorized_paths = ["~/Downloads", "~/Desktop"]
# path_intents = {下载 = "~/Downloads", 桌面 = "~/Desktop"}

[playwright]
enabled = false
browser = "chromium"

[logging]
level = "INFO"
json_logs = true
# file_path = ""              # 留空使用默认 artifacts/omniagent.log
```

### 优先级规则

对于每个配置项：**环境变量 > 配置文件 > 默认值**

敏感信息（如 API Key）必须通过环境变量注入，不要写入 `omniagent.toml`、文档、测试快照或日志。运行时会优先读取 `OMNIAGENT_LLM_API_KEY`，并兼容 `OPENAI_API_KEY` / `ARK_API_KEY`。

## 环境变量

### LLM 相关

| 变量 | 默认 | 说明 |
|------|------|------|
| `OMNIAGENT_LLM_PROVIDER` | `openai` | Provider 名称，支持任意 OpenAI 兼容服务；`test` 为本地确定性测试 adapter，不调用外部 LLM |
| `OMNIAGENT_LLM_MODEL` | `""` | 模型名，如 `gpt-4o-mini` / `deepseek-chat` / `doubao-1-5-pro-32k-250115` |
| `OMNIAGENT_LLM_API_KEY` | — | API Key，也会读取 `OPENAI_API_KEY` / `ARK_API_KEY` |
| `OMNIAGENT_LLM_API_BASE` | — | API 地址，也会读取 `ARK_BASE_URL` / `ARK_API_BASE`。留空使用 `https://api.openai.com/v1`。自动补 `/chat/completions` 后缀 |
| `OMNIAGENT_LLM_TEMPERATURE` | `0` | 生成温度 |
| `OMNIAGENT_LLM_TIMEOUT` | `60` | 单次请求超时（秒） |
| `OMNIAGENT_LLM_MAX_RETRIES` | `2` | 失败重试次数 |
| `OMNIAGENT_LLM_FALLBACK_PROVIDER` | — | 降级 provider |
| `OMNIAGENT_LLM_ENABLE_RUNTIME_FALLBACK` | `true` | 启用运行时降级 |
| `OMNIAGENT_LLM_VERIFY_ON_STARTUP` | `false` | 启动时执行 LLM 健康检查 |
| `OMNIAGENT_PROMPT_DIR` | `src/omniagent/prompts/` | Prompt 模板目录 |

`OMNIAGENT_LLM_PROVIDER=test` 只用于单元测试、CLI smoke test 和离线 real-world eval 基线。它会返回结构合法但非智能的确定性结果，可验证 runtime、权限、artifact、learning feedback 和 eval runner wiring；不要把它的通过结果当作真实 LLM 能力证明。

### Embedding 相关

默认不调用真实 embedding 服务，vector memory 使用本地 hash fallback。开启真实 embedding 后，历史向量条目会记录 provider/model/dimensions/version；召回时如果新旧向量不兼容，只使用 lexical/concept 分数，避免误算向量相似度。

`local_model` provider 在进程内加载 ONNX embedding 模型，不走 API，也不启动本地服务。`onnxruntime`、`tokenizers`、`numpy` 已进入默认运行依赖；是否启用本地模型仍由 `OMNIAGENT_EMBEDDING_PROVIDER=local_model` 控制。

| 变量 | 默认 | 说明 |
|------|------|------|
| `OMNIAGENT_EMBEDDING_ENABLED` | `false` | 是否启用真实 embedding provider；关闭时使用本地 hash |
| `OMNIAGENT_EMBEDDING_PROVIDER` | `local_hash` | Provider 名称；`local_model` 走进程内 ONNX，`openai` / `openai_compatible` / `ark` / `ollama` 等 OpenAI 兼容服务走 `/embeddings` |
| `OMNIAGENT_EMBEDDING_MODEL` | `""` | Embedding 模型名，如 `text-embedding-3-small` / `bge-small-local` |
| `OMNIAGENT_EMBEDDING_MODEL_PATH` | — | `local_model` 使用；ONNX 文件路径或包含 `model.onnx` 的目录 |
| `OMNIAGENT_EMBEDDING_TOKENIZER_PATH` | — | `local_model` 使用；`tokenizer.json` 路径，留空默认取模型目录下的 `tokenizer.json` |
| `OMNIAGENT_EMBEDDING_API_KEY` | — | Embedding API Key；必须通过环境变量注入 |
| `OMNIAGENT_EMBEDDING_API_BASE` | — | API 地址，留空使用 `https://api.openai.com/v1`；适配器自动补 `/embeddings` 后缀 |
| `OMNIAGENT_EMBEDDING_DIMENSIONS` | — | 可选维度参数；仅在 provider 支持时设置 |
| `OMNIAGENT_EMBEDDING_POOLING` | `mean` | `local_model` 使用；支持 `mean` / `cls` |
| `OMNIAGENT_EMBEDDING_NORMALIZE` | `true` | `local_model` 使用；是否对输出向量做 L2 normalize |
| `OMNIAGENT_EMBEDDING_MAX_TOKENS` | `256` | `local_model` 使用；tokenizer 截断长度 |
| `OMNIAGENT_EMBEDDING_TIMEOUT` | `30` | 单次 embedding 请求超时（秒） |
| `OMNIAGENT_EMBEDDING_MAX_RETRIES` | `1` | embedding 请求失败重试次数 |

### 存储与日志

| 变量 | 默认 | 说明 |
|------|------|------|
| `OMNIAGENT_SQLITE_PATH` | `data/omniagent.sqlite3` | SQLite 数据库路径 |
| `OMNIAGENT_LOG_PATH` | `artifacts/omniagent.log` | 日志文件路径 |
| `OMNIAGENT_LOG_LEVEL` | `INFO` | 日志级别 |
| `OMNIAGENT_JSON_LOGS` | `true` | JSON 格式日志 |

### 沙箱与权限

| 变量 | 默认 | 说明 |
|------|------|------|
| `OMNIAGENT_SANDBOX_ENABLED` | `true` | 启用沙箱 |
| `OMNIAGENT_SANDBOX_TIMEOUT` | `30` | 沙箱执行超时（秒） |
| `OMNIAGENT_SANDBOX_ALLOW_NETWORK` | `false` | 允许网络访问 |
| `OMNIAGENT_SANDBOX_AUTHORIZED_PATHS` | — | 外部授权路径，逗号分隔，如 `~/Downloads,~/Desktop` |
| `OMNIAGENT_SANDBOX_PATH_INTENTS` | — | 路径意图映射，逗号分隔的 `别名=路径`，如 `下载=~/Downloads,桌面=~/Desktop` |

### Gateway 与 Playwright

| 变量 | 默认 | 说明 |
|------|------|------|
| `OMNIAGENT_APP_NAME` | `omniagent` | 应用名称 |
| `OMNIAGENT_DEFAULT_MODE` | `semi_auto` | 默认自治模式: `assist` / `semi_auto` / `full_auto` |
| `OMNIAGENT_TASK_TIMEOUT` | `30` | 默认任务超时（秒），用于 CLI / chat 创建的 `TaskConstraints.timeout_seconds`；命令行 `--timeout` 优先级更高 |
| `OMNIAGENT_PLAYWRIGHT_ENABLED` | `false` | 启用 Playwright 浏览器执行器 |
| `OMNIAGENT_PLAYWRIGHT_BROWSER` | `chromium` | 浏览器类型 |

## Provider 配置示例

### OpenAI

```toml
[llm]
provider = "openai"
model = "gpt-4o-mini"
api_base = "https://api.openai.com/v1"
```

```bash
export OMNIAGENT_LLM_API_KEY=<your-api-key>
```

真实 embedding 可单独开启：

```toml
[embedding]
enabled = true
provider = "openai"
model = "text-embedding-3-small"
api_base = "https://api.openai.com/v1"
```

```bash
export OMNIAGENT_EMBEDDING_API_KEY=<your-api-key>
```

### 本地 ONNX embedding

当前仓库的 `omniagent.toml` 已配置默认本地模型：

```toml
[embedding]
enabled = true
provider = "local_model"
model = "bge-small-zh-v1.5"
model_path = "models/embedding/bge-small-zh-v1.5/onnx/model_quantized.onnx"
tokenizer_path = "models/embedding/bge-small-zh-v1.5/tokenizer.json"
dimensions = 512
pooling = "cls"
normalize = true
max_tokens = 512
```

模型来源是 `Xenova/bge-small-zh-v1.5` 的 ONNX 量化版本，保存在 `models/embedding/bge-small-zh-v1.5/`。

```toml
[embedding]
enabled = true
provider = "local_model"
model = "bge-small-local"
model_path = "./models/embedding/bge-small"
pooling = "mean"
normalize = true
max_tokens = 256
```

模型目录约定：

```text
models/embedding/bge-small/
├── model.onnx
└── tokenizer.json
```

`local_model` 不读取 `api_base`，不需要 API Key。切换模型文件、tokenizer、pooling 或 normalize 策略后，应重建 `workspace/MEMORY/vector/` 下的旧向量条目。

### Deepseek

```toml
[llm]
provider = "deepseek"
model = "deepseek-chat"
api_base = "https://api.deepseek.com/v1"
```

```bash
export OMNIAGENT_LLM_API_KEY=<your-api-key>
```

### 豆包方舟 (Volcengine Ark)

```toml
[llm]
provider = "ark"
model = "doubao-1-5-pro-32k-250115"
api_base = "https://ark.cn-beijing.volces.com/api/v3"
```

```bash
export OMNIAGENT_LLM_API_KEY=<your-ark-api-key>
```

**注意**：`api_base` 只写到 `/api/v3`，适配器会自动补 `/chat/completions`。不要使用旧版 Responses API 路径。

### 本地 Ollama

```toml
[llm]
provider = "ollama"
model = "qwen2.5:7b"
api_base = "http://localhost:11434/v1"
```

本地 endpoint 不需要 API Key。

### vLLM / LiteLLM / 其他兼容服务

任何暴露 `/v1/chat/completions` 端点的服务都可以用。填入对应的 `api_base` 和 `model` 即可。

## 权限等级 (L1–L5)

在任务请求时通过 `--max-permission` 指定：

| 等级 | 权限范围 |
|------|---------|
| `L1` | 只读 workspace |
| `L2` | 可写 workspace（默认） |
| `L3` | 可执行 Python 代码 |
| `L4` | 可执行外部命令 |
| `L5` | 可安装依赖、可访问网络 |

## 沙箱级别

Skill manifest 中的 `sandbox_level` 字段：

| 级别 | 隔离方式 |
|------|---------|
| `workspace` | 直接在 workspace 执行（默认） |
| `process` | 独立临时目录镜像 |
| `container` | 独立临时目录 + Python venv |

## 路径意图映射

通过别名访问 workspace 外的目录。配置后可在任务里直接使用：

```toml
[sandbox]
authorized_paths = ["~/Downloads", "~/Desktop"]
path_intents = {下载 = "~/Downloads", 桌面 = "~/Desktop"}
```

```bash
poetry run omniagent run "列出下载目录" --payload '{"action":"list","path":"下载"}'
```

内置别名：`下载`/`downloads`、`桌面`/`desktop`、`文档`/`documents`、`图片`/`images`。
