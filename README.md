# OmniAgent

OmniAgent 是一个以 **task** 为中心的本地嵌入式 Agent Runtime。它提供从多轮对话、任务规划执行、混合记忆、能力沉淀到安全治理的完整闭环。

- **单用户、单机、CLI 优先**
- **面向开发者**，可调试、可审计、可演化
- **轻量本地运行时**（stdlib 核心 + 默认本地 embedding 依赖 + OpenAI 兼容 LLM）
- **LLM 驱动**的规划、评估、对话路由、代码合成

## 核心能力

- **任务中心 Runtime** — TaskGraph DAG 执行，支持 retry/degrade/replan 动态恢复
- **多轮对话入口** — `chat repl` 持续会话，thread/checkpoint 绑定，自然语言回复
- **混合记忆** — File + Graph + Vector 三路召回融合
- **可选真实 Embedding** — 默认本地 hash fallback，可接 OpenAI-compatible `/embeddings` 或进程内 ONNX 小模型
- **Skill 生命周期** — draft → validated → approved → active → deprecated
- **受控自动生成** — LLM 合成缺失 skill 代码，带模板回退和沙箱验证
- **长期任务** — timer / external_event checkpoint 挂起与恢复
- **分级沙箱** — workspace / process / container (venv)
- **OpenAI 兼容 LLM** — 支持 OpenAI / Deepseek / Qwen / 方舟 / Ollama / vLLM 等

## 环境要求

- Python `>=3.11,<4.0`
- Poetry
- 任意 OpenAI Chat Completions 兼容的 LLM 服务

## 安装

```bash
poetry install
```

## 配置

在项目根目录创建 `omniagent.toml`：

```toml
[llm]
provider = "openai"
model = "gpt-4o-mini"
api_base = "https://api.openai.com/v1"

[embedding]
enabled = false
provider = "local_hash"
```

通过环境变量设置 API Key（推荐）：

```bash
export OMNIAGENT_LLM_API_KEY=<your-api-key>
```

真实 embedding 可按需开启：

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

进程内本地 embedding 模型：

```toml
[embedding]
enabled = true
provider = "local_model"
model = "bge-small-zh-v1.5"
model_path = "./models/embedding/bge-small-zh-v1.5/onnx/model_quantized.onnx"
tokenizer_path = "./models/embedding/bge-small-zh-v1.5/tokenizer.json"
pooling = "cls"
normalize = true
max_tokens = 512
```

`local_model` 可直接指向 `.onnx` 文件；如果 `model_path` 是目录，则默认读取目录内的 `model.onnx`。`onnxruntime`、`tokenizers`、`numpy` 已作为默认运行依赖安装，模型文件仍由本地配置提供。

其他 provider 示例：

```toml
# Deepseek
provider = "deepseek"
model = "deepseek-chat"
api_base = "https://api.deepseek.com/v1"

# 豆包方舟
provider = "ark"
model = "doubao-1-5-pro-32k-250115"
api_base = "https://ark.cn-beijing.volces.com/api/v3"

# 本地 Ollama（无需 API Key）
provider = "ollama"
model = "qwen2.5:7b"
api_base = "http://localhost:11434/v1"
```

完整配置参考：[CONFIGURATION.md](./CONFIGURATION.md)

## 快速开始

健康检查：

```bash
poetry run omniagent --workspace ./workspace llm check
```

执行一个任务：

```bash
poetry run omniagent --workspace ./workspace run "列出工作区文件" \
  --executor file \
  --payload '{"action":"list","path":"."}'
```

进入持续对话：

```bash
poetry run omniagent --workspace ./workspace chat repl --max-permission L2
```

REPL 内置命令：

- `/help` — 帮助
- `/status` — 当前任务状态
- `/thread` — 显示 thread_id
- `/show` — 查看线程详情
- `/switch <thread_id>` — 切换线程
- `/resume <thread_id>` — 恢复挂起的线程
- `/exit` — 退出

## 常用 CLI 命令

```bash
# 任务管理
poetry run omniagent --workspace ./workspace task list
poetry run omniagent --workspace ./workspace task show <task_id>

# 对话线程
poetry run omniagent --workspace ./workspace chat send "帮我写一个文件" --max-permission L2
poetry run omniagent --workspace ./workspace chat list --status active
poetry run omniagent --workspace ./workspace chat show <thread_id>

# Skill 生命周期
poetry run omniagent --workspace ./workspace skill list
poetry run omniagent --workspace ./workspace skill show <skill_name>
poetry run omniagent --workspace ./workspace skill import ./some-skill
poetry run omniagent --workspace ./workspace skill generate <skill_name> --type file --goal "..."
poetry run omniagent --workspace ./workspace skill validate <skill_name>
poetry run omniagent --workspace ./workspace skill approve <skill_name> --approver me
poetry run omniagent --workspace ./workspace skill activate <skill_name>

# Workflow 提升为 skill
poetry run omniagent --workspace ./workspace workflow promote <task_id> \
  --skill-name <candidate> --approver me --rationale "..."

# 长期任务调度
poetry run omniagent --workspace ./workspace scheduler run --at 2099-01-01T00:00:00+00:00
poetry run omniagent --workspace ./workspace scheduler signal <event_name> --payload '{...}'
```

## 开发

```bash
# 运行测试（使用 StubLLMAdapter，无需真实 LLM）
poetry run python -m pytest tests/

# 运行默认真实场景评测（会调用当前配置的 LLM provider）
zsh -ic 'poetry run python scripts/run_real_world_evals.py --verbose'

# 运行 REAL-EVAL-006 复杂场景（会调用当前配置的 LLM provider）
zsh -ic 'poetry run python scripts/run_real_world_evals.py --include-complex --verbose'

# 评测结果写入 eval_runs/<run_id>/，每个 case 生成 diagnostics.md

# Lint
poetry run ruff check src/ tests/

# Format
poetry run ruff format src/ tests/
```

复杂场景里的 `local_model` embedding 断言依赖 Poetry 环境中的默认运行依赖；不要用系统 `python3` 直接跑真实评测，否则可能降级到 `local_hash` fallback。

## Workspace 结构

首次运行自动创建：

```
workspace/
├── AGENT.md / USER.md / POLICY.md / TOOLS.md   # bootstrap 文档
├── MEMORY/
│   ├── short_term/
│   ├── long_term/
│   ├── skills/
│   ├── knowledge/
│   ├── vector/
│   └── failures/
├── WORKFLOWS/           # workflow 候选
├── tasks/<task_id>/     # 任务产物
│   ├── plan/
│   ├── nodes/
│   ├── artifacts/
│   ├── critic/
│   ├── completion/
│   └── llm_calls/
└── skills/              # 本地 skill 包
```

## 文档

- [ARCHITECTURE.md](./ARCHITECTURE.md) — 架构概览、模块职责、数据流
- [CONFIGURATION.md](./CONFIGURATION.md) — 完整配置参考
- [ROADMAP.md](./ROADMAP.md) — 路线图与优先级
- [COMPLEX_CAPABILITY_EVAL_UPGRADE.md](./COMPLEX_CAPABILITY_EVAL_UPGRADE.md) — 当前任务：复杂能力评测前置升级
- [BASIC_CAPABILITIES_UPGRADE.md](./BASIC_CAPABILITIES_UPGRADE.md) — 已完成任务：基础能力升级
- [BASIC_CAPABILITIES_V1_UPGRADE.md](./BASIC_CAPABILITIES_V1_UPGRADE.md) — 标准基础能力库 v1：SQLite / Markdown / Git / HTML / Archive / Config
- [REAL_WORLD_EVAL_PLAN.md](./REAL_WORLD_EVAL_PLAN.md) — 真实场景自主评测闭环
- [CLAUDE.md](./CLAUDE.md) — 给 AI 编程助手的项目指南
- [AGENTS.md](./AGENTS.md) — 项目规则
- [issue.md](./issue.md) — 问题追踪与近期已关闭记录

## 当前状态

代码层面已完成三阶段 MVP、P0 补丁轮、[`BASECAP-001` 基础能力升级](./BASIC_CAPABILITIES_UPGRADE.md)、`REAL-EVAL-005` 第二批真实场景矩阵，并已落地 [`BASECAP-002` 标准基础能力库 v1](./BASIC_CAPABILITIES_V1_UPGRADE.md) 首批能力：SQLite / Markdown / Git 只读 / HTML / Archive / Config。当前 P0 是 [`CAP-EVAL-001` 复杂能力评测前置升级](./COMPLEX_CAPABILITY_EVAL_UPGRADE.md)：任务完成度评估、真实 embedding、runner diagnostics 与 failure attribution 均已落地，下一步启动 `REAL-EVAL-006` 复杂真实场景矩阵。
