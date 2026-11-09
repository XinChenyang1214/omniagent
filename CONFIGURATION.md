# 配置参考

OmniAgent 支持两种配置方式：**配置文件** 和 **环境变量**。环境变量优先级高于配置文件。

## 配置文件: `omniagent.toml`

在项目根目录放置 `omniagent.toml` 或 `.omniagent.toml`，OmniAgent 启动时自动加载。

### 完整示例

```toml
[llm]
provider = "opencode"         # opencode / openrouter / openai / deepseek / ark / ollama / test
model = "deepseek-v4-flash-free"
api_base = "https://opencode.ai/zen/v1"
temperature = 0.0
timeout_seconds = 60
max_retries = 2
fallback_provider = ""        # 主 provider 失败时降级到的 provider
enable_runtime_fallback = false
verify_on_startup = false     # 启动时检查 LLM 可用性
opencode_free_only = true     # 阻止误用未带 -free 后缀的付费模型
opencode_allow_anonymous = false # 实验性无 Key 请求，默认关闭

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

敏感信息（如 API Key）必须通过环境变量注入，不要写入 `omniagent.toml`、文档、
测试快照或日志。通用入口是 `OMNIAGENT_LLM_API_KEY`；`opencode`、
`openrouter`、`openai` 和 `ark` 还分别读取 `OPENCODE_API_KEY`、
`OPENROUTER_API_KEY`、`OPENAI_API_KEY` 和 `ARK_API_KEY`。

LLM 连通性可用以下命令检查：

```bash
poetry run omniagent --workspace ./workspace llm check
```

如果输出 `diagnosis: llm_authentication_failed`，表示 provider 返回了 401 /
Unauthorized / invalid API key 一类认证错误。此时优先检查当前 shell 的
对应 provider 的 API Key 是否有效（OpenCode Zen 优先检查 `OPENCODE_API_KEY`），
`OMNIAGENT_LLM_API_BASE` 或 `api_base` 是否指向正确兼容端点，以及 `model`
是否对该 key 可用。

## 环境变量

### LLM 相关

| 变量 | 默认 | 说明 |
|------|------|------|
| `OMNIAGENT_LLM_PROVIDER` | `openai` | 代码默认 provider；仓库 `omniagent.toml` 已设为 `opencode`。支持任意 OpenAI 兼容服务；`test` 为本地确定性测试 adapter |
| `OMNIAGENT_LLM_MODEL` | `""` | 模型名；`opencode` 留空时默认 `deepseek-v4-flash-free`，`openrouter` 留空时默认 `openrouter/free` |
| `OMNIAGENT_LLM_API_KEY` | — | 通用 API Key；provider 专用变量可使用 `OPENCODE_API_KEY` / `OPENROUTER_API_KEY` / `OPENAI_API_KEY` / `ARK_API_KEY` |
| `OMNIAGENT_LLM_API_BASE` | — | 通用 API 地址；OpenCode 可使用 `OPENCODE_API_BASE`，OpenRouter 可使用 `OPENROUTER_API_BASE`，Ark 可使用 `ARK_BASE_URL` / `ARK_API_BASE`。适配器自动补 `/chat/completions` |
| `OMNIAGENT_LLM_TEMPERATURE` | `0` | 生成温度 |
| `OMNIAGENT_LLM_TIMEOUT` | `60` | 单次请求超时（秒） |
| `OMNIAGENT_LLM_MAX_RETRIES` | `2` | 失败重试次数 |
| `OMNIAGENT_LLM_FALLBACK_PROVIDER` | — | 降级 provider |
| `OMNIAGENT_LLM_ENABLE_RUNTIME_FALLBACK` | `true` | 启用运行时降级 |
| `OMNIAGENT_LLM_VERIFY_ON_STARTUP` | `false` | 启动时执行 LLM 健康检查 |
| `OMNIAGENT_OPENCODE_FREE_ONLY` | `true` | `opencode` provider 只允许以 `-free` 结尾的模型；显式设为 `false` 才允许其他 Chat Completions 模型 |
| `OMNIAGENT_OPENCODE_ALLOW_ANONYMOUS` | `false` | 实验性允许 OpenCode Zen `*-free` 模型不带 Key 发请求；仅绕过本地校验，不保证上游接受 |
| `OMNIAGENT_OPENROUTER_FREE_ONLY` | `true` | `openrouter` provider 只允许 `openrouter/free` 或以 `:free` 结尾的模型；显式设为 `false` 才允许其他模型 |
| `OMNIAGENT_OPENROUTER_SITE_URL` | — | 可选应用 URL；也读取 `OPENROUTER_SITE_URL`，作为 `HTTP-Referer` 请求头 |
| `OMNIAGENT_OPENROUTER_APP_TITLE` | `OmniAgent` | 可选应用名称；也读取 `OPENROUTER_APP_TITLE`，作为 `X-OpenRouter-Title` 请求头 |
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

### OpenCode Zen 免费模型

仓库默认配置使用 OpenCode Zen 当前提供的限时免费模型：

```toml
[llm]
provider = "opencode"
model = "deepseek-v4-flash-free"
api_base = "https://opencode.ai/zen/v1"
fallback_provider = ""
enable_runtime_fallback = false
opencode_free_only = true
opencode_allow_anonymous = true
```

仓库当前临时打开 `opencode_allow_anonymous`，因此不设置 Key 也会把请求发送到
OpenCode Zen。这个开关只对 `opencode` + `opencode_free_only = true` +
`*-free` 模型生效；其他 provider 和非免费模型仍要求 Key。2026-07-31 使用明确
清空所有 LLM Key 的环境运行 `llm check`，`deepseek-v4-flash-free` 返回成功，Zen
报告实际模型为 `deepseek-v4-flash`。匿名能力是当前端点的实测行为，不是官方稳定
承诺，仍可能随时改为要求 Key。

```bash
export OPENCODE_API_KEY=<your-opencode-api-key>
poetry run omniagent --workspace ./workspace llm check
```

OpenCode Zen 的免费模型使用 OpenAI-compatible Chat Completions 接口，因此可以
复用 OmniAgent 现有适配层。按 2026-07-31 的官方列表，可选的 `-free` 模型包括：

- `deepseek-v4-flash-free`
- `mimo-v2.5-free`
- `laguna-s-2.1-free`
- `ling-3.0-flash-free`
- `north-mini-code-free`
- `nemotron-3-ultra-free`

这些模型均是限时免费，可能随时下线或变为其他供应方式。`big-pickle` 当前也免费，
但模型 ID 没有 `-free` 后缀，因此项目的 free-only guard 不允许它，避免其免费期
结束后产生付费调用。

OpenCode Zen 官方接入流程要求登录、配置 billing details 并创建 API Key；项目不会
写入或管理账户支付设置。虽然当前免费端点实测接受匿名请求，这不构成官方保证。使用
Key 时仍建议在 Zen 控制台关闭 auto-reload 并设置月度上限。默认
`opencode_free_only = true` 会拒绝所有不以 `-free` 结尾的模型，并且仓库不配置
付费 fallback。

隐私方面，官方明确把当前免费模型列为零保留策略的例外：请求数据可能被保留并用于
模型改进，Nemotron 免费端点还属于 NVIDIA trial。不要提交个人信息、密钥、私有代码
或其他机密内容。

官方说明：
[OpenCode Zen 模型、价格与隐私](https://opencode.ai/docs/zen)、
[实时模型列表](https://opencode.ai/zen/v1/models)。

### OpenRouter 免费模型

仓库默认配置使用 OpenRouter 的免费模型路由：

```toml
[llm]
provider = "openrouter"
model = "openrouter/free"
api_base = "https://openrouter.ai/api/v1"
fallback_provider = ""
enable_runtime_fallback = false
openrouter_free_only = true
openrouter_app_title = "OmniAgent"
```

```bash
export OPENROUTER_API_KEY=<your-openrouter-api-key>
poetry run omniagent --workspace ./workspace llm check
```

`openrouter/free` 会在请求时从当前可用免费模型中自动选择，并依据请求需要过滤
structured outputs、tool calling 等能力；实际选中的模型记录在响应和 LLM 调用证据的
`model` 字段中。如果需要固定免费模型，使用 OpenRouter 当前提供的
`<model-id>:free` 标识。默认 `openrouter_free_only = true` 会拒绝没有 `:free`
后缀的具体模型，避免误用付费路由；项目也不配置隐式付费 fallback。

免费模型仍需要 OpenRouter API Key，并受账户级免费请求额度和上游可用性约束。
按 2026-07-31 的官方说明，免费模型合计默认限 50 次请求/日；账户累计购买至少
10 美元 credits 后为 1000 次/日。免费模型不适合作为生产稳定性承诺。OpenRouter
本身默认不保留 prompt / response，除非用户选择加入数据收集；但具体上游 provider
可能有独立的日志或训练策略，使用前应在 OpenRouter 隐私设置中限制不符合要求的
provider。

官方说明：
[Free Models Router](https://openrouter.ai/docs/guides/routing/routers/free-router)、
[Free model variant](https://openrouter.ai/docs/guides/routing/model-variants/free)、
[免费额度 FAQ](https://openrouter.ai/docs/faq)、
[Provider 数据策略](https://openrouter.ai/docs/guides/privacy/provider-logging/)。

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
