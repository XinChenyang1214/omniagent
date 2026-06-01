# OmniAgent 架构文档

本文档描述 OmniAgent 的当前实现架构。源代码是事实真理——本文档描述意图和模块关系，不追求穷尽细节。

## 定位

OmniAgent 是一个以 **task** 为中心的本地嵌入式 Agent Runtime：

- 单用户、单机、CLI 优先
- 面向开发者和技术用户，可调试、可审计、可演化
- Python 3.11+，Poetry 包管理
- **轻量本地运行时依赖**（stdlib 核心 + 默认本地 embedding 推理依赖 + OpenAI 兼容 LLM）
- 默认离线运行（除 LLM 调用和显式开启的真实 embedding 外）

## 核心闭环

```
Goal → Plan → Execute → Learn → Evolve
```

所有重要决策（规划、评估、对话路由、代码生成）都通过 LLM 完成，LLM 接口统一为 OpenAI Chat Completions 标准。

## 执行流程

```
用户输入 (CLI run / chat send / chat repl)
  │
  ▼
InteractionRouter (interaction.py)
  │   规则快速路径 + LLM 兜底
  │   分类为: new_task / task_update / approval_response / status_query /
  │          failure_query / replan_request / knowledge_query / system_request
  ▼
RuntimeGateway (gateway.py)
  │   协调所有子系统，是所有入口的汇聚点
  ▼
ExecutionPolicy (execution_policy.py)          [策略层]
  │   显式低风险结构化 payload 可 direct_execute，跳过 Planner
  │   高风险、指代、网络、删除、Planner context 验收等回退 Planner
  ▼
Planner (planner.py)                          [确定性快速路径 + LLM 驱动]
  │   输入: bootstrap 文档 + 混合记忆召回 + skill capability index / expanded manifests
  │   输出: TaskGraph (DAG) — 节点、依赖、retry policy
  │   必须规划时继续支持确定性计划和 LLM 兜底
  ▼
ExecutionEngine (execution_engine.py)
  │   按依赖顺序执行节点
  │   同图依赖节点的输出会以 dependency_outputs 注入下游 action 节点 payload
  │   处理: retry / degrade / replan / checkpoint 挂起恢复
  │   ├─► SkillDispatcher (dispatcher.py)
  │   │     选择 skill: explicit > registry scoring > executor hint > auto-generate
  │   │
  │   ├─► Executors (executors/)
  │   │     file.py / python.py / browser.py / llm_native.py
  │   │
  │   └─► Sandbox (sandbox.py)
  │         workspace (直接) / process (tmpdir) / container (venv)
  ▼
Critic (critic.py)                            [LLM 驱动]
  │   评估节点/任务结果，决定: accept / retry / degrade / replan / fail
  ▼
CompletionEvaluator (completion_evaluator.py) [LLM 驱动]
  │   独立判断任务是否真正满足用户目标，产出 completion evaluation artifact
  ▼
ResponseGenerator (response_generator.py)     [LLM 驱动]
  │   将执行结果、checkpoint、对话历史转为自然语言回复
  ▼
用户 (自然语言回复)
```

后台同时运行：
- **WorkflowCompiler** (`workflow_compiler.py`) — 检测重复成功的工作流，生成 skill candidate 草稿
- **SkillLifecycleManager** (`skill_lifecycle.py`) — 管理 skill 状态流转（draft → validated → approved → active → deprecated）

## 模块职责

### 入口与协调层

| 模块 | 职责 |
|------|------|
| `cli.py` | CLI 入口，子命令: `run` / `task` / `chat` / `skill` / `workflow` / `scheduler` / `llm` |
| `gateway.py` | `RuntimeGateway` — 所有子系统的装配点，`submit()` 和 `send_message()` 是两个主入口 |
| `runtime.py` | `EmbeddedRuntime` — 串联 ExecutionPolicy / Planner → ExecutionEngine → Critic 的任务执行核心 |
| `execution_policy.py` | `ExecutionPolicy` — 在 Planner 前判断 direct_execute / plan_required，并记录策略 artifact |
| `interaction.py` | `InteractionRouter` — 多轮对话意图识别，规则快路径 + LLM 兜底 |

### 规划与执行层

| 模块 | 职责 |
|------|------|
| `planner.py` | 确定性快速路径 + LLM 驱动的任务规划，产出 `TaskGraph`（DAG）。Planner payload 使用 capability index + 按需展开 manifest；支持 replan |
| `execution_engine.py` | DAG 执行引擎，处理节点状态流转、retry、degrade、replan、checkpoint |
| `critic.py` | LLM 驱动的结果评估，产出 `CriticVerdict` |
| `completion_evaluator.py` | LLM 驱动的任务完成度评估，产出 `CompletionEvaluation` |
| `dispatcher.py` | Skill 选择与执行，处理缺失/不兼容 skill 的自动生成分支 |
| `response_generator.py` | LLM 驱动的自然语言回复生成 |

### 数据与记忆层

| 模块 | 职责 |
|------|------|
| `models.py` | 全部数据模型: `Task` / `TaskGraph` / `TaskNode` / `ConversationThread` / `PendingCheckpoint` / skill 生命周期模型等 |
| `storage.py` | `SQLiteStore` — 任务、事件、线程、权限、skill 记录的持久化 |
| `memory.py` | `FileMemoryManager` — 文件型记忆（任务摘要、工作流摘要、失败模式、vector entry） |
| `graph_memory.py` | `GraphMemoryStore` — 关系记忆（task↔skill↔workflow↔failure↔thread） |
| `embeddings.py` | Embedding provider 层；默认本地 hash，支持 OpenAI-compatible `/embeddings`、进程内 ONNX 小模型，并显式 fallback |
| `vector_recall.py` | 向量相似度召回；按 provider/model/dimensions/version 判断新旧向量兼容性 |
| `hybrid_memory.py` | 三路召回融合（File + Graph + Vector） |
| `workspace.py` | `WorkspaceManager` 和 `TaskArtifactManager` — workspace 目录结构和任务产物落盘 |

### Skill 与能力演化

| 模块 | 职责 |
|------|------|
| `skills/loader.py` | 扫描加载 `workspace/skills/`、builtin skills、生成的 skills |
| `skill_registry.py` | Skill 注册表 + manifest 预过滤 + action/权限/历史信号打分 |
| `skill_lifecycle.py` | Skill 状态机: draft → validated → approved → active → deprecated |
| `skill_auto_generation.py` | LLM 驱动的 Skill 代码合成（带模板回退） |
| `workflow_compiler.py` | 检测稳定工作流并编译为 skill candidate |

### 安全与观测

| 模块 | 职责 |
|------|------|
| `permissions.py` | L1–L5 权限层级，控制网络/删除/外部命令/依赖安装/记忆读取 |
| `sandbox.py` | 三级沙箱: workspace / process / container (venv) |
| `events.py` | `EventBus` — 任务事件广播和记录 |
| `logging_utils.py` | 结构化日志 |
| `scheduler.py` | 长期任务调度：timer / external_event checkpoint 的唤醒和恢复 |

### LLM 接入层

| 模块 | 职责 |
|------|------|
| `llm/base.py` | `LLMAdapter` 抽象接口 |
| `llm/openai_compatible_adapter.py` | **统一适配器**，基于 Chat Completions API，支持任意 OpenAI 兼容服务 |
| `llm/resilient_adapter.py` | Fallback/degradation 包装器 |
| `llm/factory.py` | Provider 工厂 + 测试适配器注册钩子 |
| `prompting.py` | Prompt 模板加载器 |

### Embedding 接入层

真实 embedding 是可选能力。默认配置下，`VectorRecallEngine` 使用本地 hash embedding，保持离线测试和冷启动可用。开启 `OMNIAGENT_EMBEDDING_ENABLED=true` 后，Gateway 会按 `OMNIAGENT_EMBEDDING_PROVIDER` 装配 embedding provider，将任务经验和生成 skill 学习摘要写入 `MEMORY/vector/*.json`，并记录：

- `embedding_provider`
- `embedding_model`
- `embedding_dimensions`
- `embedding_version`
- `embedding_fallback_used`

支持的 provider：

- `local_hash`：默认离线 fallback，无外部依赖
- `openai` / `openai_compatible` / 同类 provider：调用 `/v1/embeddings`
- `local_model`：进程内加载 ONNX embedding 小模型，不走 API，不启动本地服务；依赖 `onnxruntime`、`tokenizers`、`numpy` 已进入默认运行依赖

`local_model` 额外记录 `embedding_model_path`、`embedding_tokenizer_path`、`embedding_model_fingerprint`、`embedding_pooling`、`embedding_normalize`、`embedding_max_tokens`。召回时，只有 query embedding 与 stored embedding 的 provider/model/dimensions/version/fingerprint 兼容时才计算向量相似度；不兼容时保留 lexical/concept 分数，避免把旧 hash 向量或不同模型向量误当作同一空间。

### Prompt 模板（`src/omniagent/prompts/`）

| 文件 | 用途 |
|------|------|
| `planner_v1.md` | 任务规划：目标 → TaskGraph DAG |
| `critic_v1.md` | 执行评估：结果 → accept/retry/degrade/replan/fail |
| `completion_evaluator_v1.md` | 任务完成度评估：最终结果 → 满足度、证据、失败归因 |
| `router_v1.md` | 对话路由（LLM 兜底，规则无法决策时使用） |
| `responder_v1.md` | 自然语言回复生成 |
| `skill_generator_v1.md` | Skill 代码合成 |

## 数据模型核心

### 任务模型

```
Task
├── TaskGraph (DAG)
│   ├── TaskNode (action/approval/decision/wait/replan/finalize)
│   │   ├── retry_policy: {max_retries, backoff_seconds, degrade_to, replan_on_failure}
│   │   ├── dependencies: [node_id]
│   │   └── input_payload / output
│   └── dependencies (显式边)
├── TaskRequest (输入快照)
├── PlanResult (Planner 输出)
└── TaskEvent[] (执行事件流)
```

节点类型：
- `action` — 具体执行步骤
- `finalize` — 汇总节点
- `approval` — 需要用户确认
- `decision` — 需要用户选择分支
- `wait` — 等待定时器或外部事件
- `replan` — 标记需要重新规划

### 对话模型

```
ConversationThread
├── active_task_id
├── pending_checkpoint_id
└── ConversationTurn[] (user / assistant / system)

PendingCheckpoint
├── type: APPROVAL / DECISION / PERMISSION / TIMER / EXTERNAL_EVENT
├── task_id / node_id
└── status: PENDING / RESOLVED / REJECTED / CANCELLED
```

### Skill 模型

```
SkillRegistryEntry
├── lifecycle_status: DRAFT / VALIDATED / APPROVED / ACTIVE / DEPRECATED
├── version / success_rate / source
└── permission_requirements / sandbox_level

SkillDraft → ValidationReport → ApprovalRecord → RegistrationRecord
```

当前 active builtin skill 包括基础包装 `builtin.file.basic` / `builtin.python.basic` / `builtin.browser.basic`，以及 `BASECAP-001` 落地的 `builtin.data.json` / `builtin.datetime.format` / `builtin.data.csv` / `builtin.http.client` / `builtin.shell.safe`。Planner 接收的是按任务 action、tags、executor hint、权限和历史信号预过滤后的 capability index；完整 manifest / contract 只对显式 skill、executor 映射或 action 命中的少量候选展开，兼容字段 `skill_registry_entries` 等同于本轮 expanded manifests。

## Workspace 布局

首次运行时自动创建：

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
├── WORKFLOWS/                  # workflow 候选
├── tasks/<task_id>/
│   ├── plan/                   # 规划快照
│   ├── nodes/                  # 节点输出
│   ├── artifacts/              # 产物
│   ├── critic/                 # critic 判定
│   ├── completion/             # 任务完成度评估
│   └── llm_calls/              # LLM 调用日志
└── skills/                     # 本地 skill 包
```

## 关键设计决策

### 1. 所有智能决策走 LLM

Planner / Critic / Router (兜底) / ResponseGenerator / SkillGenerator 均调用 `llm.generate_structured()`。不再有硬编码的"智能"规则（规则路由只处理明确场景，ambiguous 场景交给 LLM）。

### 2. 统一 Chat Completions 接口

所有 LLM provider 通过 `OpenAICompatibleAdapter` 访问，使用标准 `/v1/chat/completions` 端点。结构化输出有 3 层回退：

1. `response_format: json_schema` (OpenAI/Deepseek 原生)
2. `response_format: json_object` + schema 在 system prompt
3. 无 response_format，纯 prompt-based schema instruction (Ollama 等)

### 3. 零运行时依赖

`pyproject.toml` 中 `dependencies = []`。所有 HTTP 调用走 `urllib.request`。配置文件使用 Python 3.11+ 内置的 `tomllib`。

### 4. 规则路由 + LLM 兜底

`interaction.py` 的 `_rule_based()` 用手写 token 列表处理明确场景（approval 回复、状态查询等），`_llm_based()` 处理模糊场景。快路径避免了每轮对话都调 LLM 的延迟和成本。

### 5. 规划-执行-评估闭环

Planner 产出 TaskGraph → ExecutionEngine 执行 → Critic 判定 → 失败时 ExecutionEngine 根据 verdict 触发 retry/degrade/replan，形成动态图重构。

### 6. Workflow 学习与 Skill 生命周期

成功的执行模式被 `WorkflowCompiler` 检测并沉淀为 skill candidate，进入统一生命周期平台（不绕过验证直接激活）。

### 7. 可审计一切

SQLite 记录任务、事件、权限决策、skill 生命周期；workspace 落盘请求快照、计划、节点输出、LLM 调用；Graph Memory 记录结构化关系。

## 不是什么

- ❌ 不是 hosted multi-agent 框架
- ❌ 不是有 UI 的 Agent 应用
- ❌ 不是 cloud-native / 分布式
- ❌ 不是聊天机器人（它的核心是任务执行）
- ❌ 不依赖特定 LLM provider
