# OmniAgent

OmniAgent 当前代码基础是一个以 **task** 为中心的本地嵌入式 Agent Runtime。自 2026-06-11 起，项目按 [`docs/product-architecture.md`](./docs/product-architecture.md) 重新开发为以学习循环为中心的通用个人 Agent OS；现有 runtime 将作为 Execution Runtime 下沉复用。

项目目标是通用能力 Agent，而不是代码助手或单一开发工具。早期实现以单用户、本地、CLI 优先的技术用户场景切入，但长期能力应覆盖文件整理、信息归纳、文档生成、数据处理、网页信息获取、任务规划、轻量自动化和开发维护等多类个人任务。

- **单用户、单机、CLI 优先**
- **面向技术用户的通用任务**，可调试、可审计、可演化
- **轻量本地运行时**（stdlib 核心 + 默认本地 embedding 依赖 + OpenAI 兼容 LLM）
- **LLM 驱动**的规划、评估、对话路由、内容生成、代码合成和工具选择

## 当前代码能力

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
- `/state [profile|preferences|project|goal]` — 查看 Agent state
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

# Agent state
poetry run omniagent --workspace ./workspace state show
poetry run omniagent --workspace ./workspace state show --section goal --json

# Procedure memory
poetry run omniagent --workspace ./workspace procedure list
poetry run omniagent --workspace ./workspace procedure show <procedure_id>
poetry run omniagent --workspace ./workspace procedure explain-match "写一个报告"
poetry run omniagent --workspace ./workspace procedure edit <procedure_id> \
  --patch '{"trigger":{"negative_patterns":["delete"]}}'
poetry run omniagent --workspace ./workspace procedure review <procedure_id> \
  --decision approved --evidence-ref manual_review/<procedure_id>.md
poetry run omniagent --workspace ./workspace procedure promote-habit <procedure_id>
poetry run omniagent --workspace ./workspace procedure disable <procedure_id>

# Learning governance
poetry run omniagent --workspace ./workspace learning review
poetry run omniagent --workspace ./workspace learning review --type experience --json
poetry run omniagent --workspace ./workspace learning review --type habit --json
poetry run omniagent --workspace ./workspace learning review --type skill --json
poetry run omniagent --workspace ./workspace learning show --type experience <experience_id> --json
poetry run omniagent --workspace ./workspace learning show --type procedure <procedure_id> --json
poetry run omniagent --workspace ./workspace learning show --type habit <procedure_id> --json
poetry run omniagent --workspace ./workspace learning show --type skill <skill_name> --json
poetry run omniagent --workspace ./workspace learning history --type experience <experience_id> --json
poetry run omniagent --workspace ./workspace learning history --type procedure <procedure_id> --json
poetry run omniagent --workspace ./workspace learning history --type habit <procedure_id> --json
poetry run omniagent --workspace ./workspace learning history --type skill <skill_name> --json
poetry run omniagent --workspace ./workspace learning explain-match "写一个报告" --json
poetry run omniagent --workspace ./workspace learning edit --type procedure <procedure_id> \
  --patch '{"name":"updated procedure"}'
poetry run omniagent --workspace ./workspace learning edit --type habit <procedure_id> \
  --patch '{"trigger":{"task_patterns":["write validated report"]}}'
poetry run omniagent --workspace ./workspace learning promote --type procedure <procedure_id>
poetry run omniagent --workspace ./workspace learning promote --type habit <procedure_id> \
  --skill-name generated.habit.example
poetry run omniagent --workspace ./workspace learning validate --type procedure <procedure_id> \
  --evidence-ref manual_review/<procedure_id>.md
poetry run omniagent --workspace ./workspace learning validate --type habit <procedure_id> \
  --evidence-ref manual_review/<procedure_id>.md
poetry run omniagent --workspace ./workspace learning validate --type skill <skill_name> \
  --version <version>
poetry run omniagent --workspace ./workspace learning decay --type procedure <procedure_id> \
  --note "environment changed"
poetry run omniagent --workspace ./workspace learning decay --type habit <procedure_id> \
  --note "stale validator"
poetry run omniagent --workspace ./workspace learning request-changes --type procedure <procedure_id> \
  --note "needs narrower trigger"
poetry run omniagent --workspace ./workspace learning request-changes --type habit <procedure_id> \
  --note "validator needs update"
poetry run omniagent --workspace ./workspace learning approve --type procedure <procedure_id> \
  --evidence-ref manual_review/<procedure_id>.md
poetry run omniagent --workspace ./workspace learning approve --type skill <skill_name> \
  --reviewer me --note "approved after review"
poetry run omniagent --workspace ./workspace learning reject --type procedure <procedure_id> \
  --note "not reusable"
poetry run omniagent --workspace ./workspace learning reject --type skill <skill_name> \
  --reviewer me --note "rejected after review"
poetry run omniagent --workspace ./workspace learning disable --type procedure <procedure_id> --note "stale"
poetry run omniagent --workspace ./workspace learning disable --type habit <procedure_id> --note "stale"
poetry run omniagent --workspace ./workspace learning disable --type skill <skill_name> --note "stale"
poetry run omniagent --workspace ./workspace learning enable --type procedure <procedure_id> --note "restored"
poetry run omniagent --workspace ./workspace learning enable --type habit <procedure_id> --note "restored"
poetry run omniagent --workspace ./workspace learning enable --type skill <skill_name> --note "restored"
poetry run omniagent --workspace ./workspace learning rollback --type skill <skill_name> \
  --to-version <version>

# Skill 生命周期
poetry run omniagent --workspace ./workspace skill list
poetry run omniagent --workspace ./workspace skill show <skill_name>
poetry run omniagent --workspace ./workspace skill import ./some-skill
poetry run omniagent --workspace ./workspace skill generate <skill_name> --type file --goal "..."
poetry run omniagent --workspace ./workspace skill promote-habit <procedure_id>
poetry run omniagent --workspace ./workspace skill validate <skill_name>
poetry run omniagent --workspace ./workspace skill approve <skill_name> --approver me
poetry run omniagent --workspace ./workspace skill activate <skill_name>

# Legacy workflow candidate 确认；不会绕过 habit gate 直接激活 skill
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

# 汇总已有 eval_runs 的跨 run evidence 索引（不重新执行评测）
zsh -ic 'poetry run python scripts/run_real_world_evals.py --build-evidence-index'

# 查询已有 evidence 索引，可按 failure layer / skill / procedure / habit / lineage / scenario / promotion generation type / promotion source target / category / requires / skip reason / permission / completion layer / policy decision / skipped stage / policy risk flag / policy reason / policy context budget / request payload stats / node status / critic action / memory retrieval / LLM usage / learning kind / learning validation / skill validation / review decision / audit event 过滤
zsh -ic 'poetry run python scripts/run_real_world_evals.py --query-evidence-index --evidence-failure-layer skill_or_tool'
zsh -ic 'poetry run python scripts/run_real_world_evals.py --query-evidence-index --evidence-skill "generated.habit.*" --evidence-output eval_runs/evidence_query.md'
zsh -ic 'poetry run python scripts/run_real_world_evals.py --query-evidence-index --evidence-scenario "learning_loop_jsonl_habit_promoted_skill_lineage"'
zsh -ic 'poetry run python scripts/run_real_world_evals.py --query-evidence-index --evidence-procedure "proc_eval_jsonl_habit"'
zsh -ic 'poetry run python scripts/run_real_world_evals.py --query-evidence-index --evidence-habit "proc_eval_jsonl_habit"'
zsh -ic 'poetry run python scripts/run_real_world_evals.py --query-evidence-index --evidence-lineage-skill "generated.habit.eval_jsonl_events"'
zsh -ic 'poetry run python scripts/run_real_world_evals.py --query-evidence-index --evidence-promotion-generation-type "json"'
zsh -ic 'poetry run python scripts/run_real_world_evals.py --query-evidence-index --evidence-promotion-source-target "tsv"'
zsh -ic 'poetry run python scripts/run_real_world_evals.py --query-evidence-index --evidence-category learning_loop'
zsh -ic 'poetry run python scripts/run_real_world_evals.py --query-evidence-index --evidence-requires network --evidence-status skipped'
zsh -ic 'poetry run python scripts/run_real_world_evals.py --query-evidence-index --evidence-skip-reason "requires --include-network"'
zsh -ic 'poetry run python scripts/run_real_world_evals.py --query-evidence-index --evidence-permission "filesystem.write:*"'
zsh -ic 'poetry run python scripts/run_real_world_evals.py --query-evidence-index --evidence-completion-layer recorded'
zsh -ic 'poetry run python scripts/run_real_world_evals.py --query-evidence-index --evidence-policy-decision direct_execute'
zsh -ic 'poetry run python scripts/run_real_world_evals.py --query-evidence-index --evidence-skipped-stage planner'
zsh -ic 'poetry run python scripts/run_real_world_evals.py --query-evidence-index --evidence-policy-risk-flag planner_context_required'
zsh -ic 'poetry run python scripts/run_real_world_evals.py --query-evidence-index --evidence-policy-reason "*风险标记*"'
zsh -ic 'poetry run python scripts/run_real_world_evals.py --query-evidence-index --evidence-policy-context-budget "skill_registry_entries:json_chars:50000+"'
zsh -ic 'poetry run python scripts/run_real_world_evals.py --query-evidence-index --evidence-request-payload-stats "skill_registry_entries:json_chars:10000-49999"'
zsh -ic 'poetry run python scripts/run_real_world_evals.py --query-evidence-index --evidence-node-status status:failed'
zsh -ic 'poetry run python scripts/run_real_world_evals.py --query-evidence-index --evidence-critic-action action:accept'
zsh -ic 'poetry run python scripts/run_real_world_evals.py --query-evidence-index --evidence-memory-retrieval recorded'
zsh -ic 'poetry run python scripts/run_real_world_evals.py --query-evidence-index --evidence-llm-usage llm_called'
zsh -ic 'poetry run python scripts/run_real_world_evals.py --query-evidence-index --evidence-llm-usage "tokens:10000+,duration:60s+"'
zsh -ic 'poetry run python scripts/run_real_world_evals.py --query-evidence-index --evidence-learning-kind habit_fast_paths'
zsh -ic 'poetry run python scripts/run_real_world_evals.py --query-evidence-index --evidence-learning-validation "*"'
zsh -ic 'poetry run python scripts/run_real_world_evals.py --query-evidence-index --evidence-skill-validation passed'
zsh -ic 'poetry run python scripts/run_real_world_evals.py --query-evidence-index --evidence-learning-review-decision needs_changes'
zsh -ic 'poetry run python scripts/run_real_world_evals.py --query-evidence-index --evidence-learning-audit-event decayed'

# 生成失败修复入口、regression commands（含 eval coverage status / scenario / category / requires / skip reason、used skill、procedure id / habit id、lineage skill、promotion generation type / promotion source target / permission summary / completion layer / policy decision / skipped stage / policy risk flag / policy reason / policy context budget 高水位 / request payload stats 高水位 / node status / critic action / memory retrieval / LLM usage / learning kind / learning validation / skill validation / review decision / audit event 动态查询）、eval coverage、skipped eval、model capability、node execution、critic action、completion evaluation、memory retrieval、LLM usage / runtime cost hotspot、execution policy、Planner skipped、policy risk flag、policy reason、policy context budget、request payload stats、learning match explanation、learning gate、learning governance、learning history、learning approval、learning promotion、learning validation、learning edit、learning request-changes、learning decay、learning disable、learning restore、learning rollback、learning rejection、learning audit gap、skill lifecycle governance、skill promotion coverage、skill approval gate 和 lineage 回归看板（不重新执行评测）
zsh -ic 'poetry run python scripts/run_real_world_evals.py --evidence-dashboard'
zsh -ic 'poetry run python scripts/run_real_world_evals.py --evidence-dashboard --evidence-dashboard-commands-output eval_runs/evidence_dashboard.commands'

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

- [docs/README.md](./docs/README.md) — 文档治理入口、阅读顺序和整理规则
- [docs/doc-registry.md](./docs/doc-registry.md) — 文档类型、状态、权威边界和替代关系登记表
- [docs/product-architecture.md](./docs/product-architecture.md) — 目标产品架构、学习闭环和 Agent 重新设计方向
- [AGENT_USABILITY_LEARNING_LOOP_MVP.md](./docs/workstreams/active/AGENT_USABILITY_LEARNING_LOOP_MVP.md) — 当前主线：`META-010` Agent usability and learning loop MVP
- [docs/specs/agent-state.md](./docs/specs/agent-state.md) — Agent state 最小数据契约和验收标准
- [docs/specs/experience-record.md](./docs/specs/experience-record.md) — 任务经验记录 schema、写入时机和 learning gate
- [docs/specs/procedure-memory.md](./docs/specs/procedure-memory.md) — procedure / habit 生命周期和升级规则
- [docs/specs/skill-promotion.md](./docs/specs/skill-promotion.md) — habit -> skill promotion gate、lineage、contract 和权限规则
- [ARCHITECTURE.md](./ARCHITECTURE.md) — 旧实现架构和迁移输入
- [CONFIGURATION.md](./CONFIGURATION.md) — 完整配置参考
- [ROADMAP.md](./ROADMAP.md) — 路线图与优先级
- [pre-product-redesign archive](./docs/archive/pre-product-redesign/) — 重开发前旧阶段过程文档归档
- [REAL_WORLD_EVAL_PLAN.md](./REAL_WORLD_EVAL_PLAN.md) — 真实场景自主评测闭环
- [CLAUDE.md](./CLAUDE.md) — 给 AI 编程助手的项目指南
- [AGENTS.md](./AGENTS.md) — 项目规则
- [issue.md](./issue.md) — 问题追踪与近期已关闭记录

## 当前状态

代码层面仍保留三阶段 MVP、P0 补丁轮、`BASECAP-001`、`BASECAP-002`、`REAL-EVAL-005`、`CAP-EVAL-001` 和 `META-009` 首轮落地结果作为旧实现基础；相关过程文档已归档到 [`docs/archive/pre-product-redesign/`](./docs/archive/pre-product-redesign/)。

当前开发主线是 [`META-010` Agent usability and learning loop MVP](./docs/workstreams/active/AGENT_USABILITY_LEARNING_LOOP_MVP.md)。Phase 1 Agent state、Phase 2 Experience records、Phase 3 Procedure memory、Phase 4 Habit fast path、Phase 5 Skill promotion、Phase 6 Learning feedback 和 Phase 7 Learning correction 已实现，学习反馈、学习纠错、治理入口、evidence index 和 evidence dashboard 已形成第一轮学习闭环。当前新增了 `TaskIntakeAssessment`，任务执行前会记录任务类型、风险等级、候选工具、验证方式和是否需要确认，写入 `workspace/tasks/<task_id>/plan/intake_assessment.json`，并通过 CLI 非 JSON 事件流提前输出 `任务预判`。

下一步是推进通用 Agent 初步可用版：用多类真实个人任务验证目标理解、风险判断、工具选择、执行验证、记忆复用和学习纠错，而不是把开发维护任务或 skill 类型扩展当成产品边界。首批 `general_agent_usability_*` 场景覆盖文件整理、信息归纳、文档生成、数据处理和网页信息获取 5 类任务，并为每类补了学习序列、自然语言 tool-choice 和独立权限边界场景，已通过 `scripts/run_real_world_evals.py --check-only`；离线 `OMNIAGENT_LLM_PROVIDER=test` 执行基线已在 `eval_runs/general_agent_usability_offline_latest/` 下跑通，真实 LLM provider 基线仍待建立。
