# 复杂能力评测前置升级任务

## 状态

- 任务 ID：`CAP-EVAL-001`
- 状态：✅ 完成
- 生效日期：2026-05-12
- 优先级：P0 已归档
- 当前阶段：已完成并归档

`CAP-EVAL-001` 是进入下一批复杂真实场景评测前必须完成的前置升级；该前置升级已完成，后续复杂场景应继续复用本任务沉淀的 diagnostics、completion evaluation、真实 embedding 和 failure attribution 归因闭环。

允许例外：

- 修复阻塞 `CAP-EVAL-001` 的 bug
- 补充本任务所需测试、文档和评测断言
- 修正会导致当前默认真实评测基线无法运行的问题
- 处理用户明确指定的高优先级任务

## 背景

`REAL-EVAL-005` 已把真实评测推进到第二批场景，覆盖错误恢复、复杂多轮、规划合理性和产物质量。但继续测试更复杂场景前，当前评测体系还有三个关键短板：

1. **模型能力与 Agent 能力没有清晰切分**
   有些失败可能是模型在已获得充分上下文、工具结果和 schema 后仍无法完成推理；这类问题不应被误判为 Agent 架构缺陷，也不应通过局部 patch 掩盖。

2. **任务级 critic 不是明确的任务完成度评估节点**
   当前已有 node-level / task-level `CriticVerdict`，但它主要服务于 retry / degrade / replan / fail 决策。复杂评测需要一个独立 completion evaluation，判断最终结果是否真正满足用户目标，并给出可审计的失败归因。

3. **向量召回已支持真实 embedding，但评测诊断仍需升级**
   `embeddings.py` 已接入 OpenAI-compatible `/embeddings` 与进程内 `local_model` ONNX provider，默认仍使用本地 hash fallback。复杂跨任务记忆、语义召回和模糊引用测试还需要在 diagnostics 中明确展示 provider/model/dimensions/fingerprint/fallback 与召回证据，否则仍可能把召回问题误混为 Planner 或模型问题。

因此下一批复杂场景不应直接扩充，而应先补齐评测与召回基础能力。

## 目标

完成后，真实评测应能回答四个问题：

1. 任务最终是否真的完成？
2. 如果没有完成，失败主要属于哪个层级？
3. 这个失败是否应归因于模型能力不足，而不是本 Agent 的流程或实现缺陷？
4. 如果场景依赖历史语义记忆，召回是否使用了真实 embedding，而不是 hash fallback？

## 失败归因层级

`CAP-EVAL-001` 完成后，真实评测至少应支持以下归因：

| 层级 | 含义 | 是否优先修 Agent |
|---|---|---|
| `agent_flow` | Runtime / task graph / 状态流转 / 上下文传递设计不合理 | 是 |
| `planner_or_skill_selection` | 计划结构、skill 选择或 contract 使用错误 | 是 |
| `tool_or_skill` | executor、builtin skill、权限声明或返回结构错误 | 是 |
| `memory_retrieval` | 召回缺失、误召回、embedding 配置或向量兼容问题 | 是 |
| `completion_evaluation` | 完成度评估节点本身误判或证据不足 | 是 |
| `model_capability` | 上下文、工具结果、schema 均充分，但模型仍做出低质量推理或判断 | 默认不修 Agent，可记录模型限制 |
| `environment` | provider、网络、token、文件系统、权限环境异常 | 视情况 |
| `test_expectation` | 场景断言过窄、预期不合理或与产品目标冲突 | 修测试 |

归因为 `model_capability` 必须满足：

- Planner / Critic / Completion Evaluator 已获得完成任务所需的关键上下文
- 可用 skill / 工具路径合理，且工具执行结果可观测
- schema、prompt 和任务目标没有明显歧义
- 失败不是由权限、召回、状态流转、上下文遗漏或 evaluator 误判直接导致
- diagnostics 中能引用可观测证据，而不是依赖隐藏思维链

## 阶段计划

### 阶段 0：文档与优先级冻结

状态：已完成。

要求：

- 新增本任务文档
- `ROADMAP.md` 声明 `CAP-EVAL-001` 在执行期为唯一 P0 主线
- `issue.md` 增加 `CAP-EVAL-001`，并声明 `REAL-EVAL-006` 的阶段性阻塞关系
- `REAL_WORLD_EVAL_PLAN.md` 标注复杂场景扩充前置条件
- 入口文档能指向本任务

### 阶段 1：任务完成度评估节点

状态：已完成。

目标：在任务执行结束后生成独立 completion evaluation artifact。

要求：

- 新增独立的完成度评估模型，不复用 `CriticVerdict` 的 action 语义
- 输入包括原始用户目标、TaskGraph、节点结果、执行结果、critic verdict、召回摘要和历史失败摘要
- 输出包括：
  - `task_satisfied`
  - `completion_score`
  - `missing_requirements`
  - `unexpected_side_effects`
  - `evidence`
  - `primary_failure_layer`
  - `model_capability_suspected`
  - `recommended_next_action`
  - `confidence`
- 写入 `tasks/<task_id>/completion/`
- 在 `TaskExecutionReport.completion_evaluation` 中可见
- 不记录隐藏思维链，只记录可审计证据

已落地内容：

- `CompletionEvaluation` 数据模型
- `CompletionEvaluator` + `completion_evaluator_v1` prompt
- Runtime 任务图完成后自动评估
- `TaskArtifactManager.write_completion_evaluation`
- `tasks/<task_id>/completion/evaluation.json` 与 `summary.md`
- completion evaluator LLM 调用写入 `tasks/<task_id>/llm_calls/`
- 新增单元测试 `tests/test_completion_evaluation.py`

验证：

- `poetry run pytest tests/test_completion_evaluation.py`：2 passed
- `poetry run pytest`：379 passed
- `poetry run ruff check .`：通过
- `python3 scripts/run_real_world_evals.py --check-only`：20 scenarios validated

### 阶段 2：真实 embedding 接入

状态：已完成。

目标：把 vector memory 从 hash fallback 升级为可配置真实 embedding。

要求：

- 新增 OpenAI-compatible embeddings adapter，使用 `/v1/embeddings`
- 新增进程内 `local_model` adapter，加载本地 ONNX 小模型，不走 API / 本地服务
- 增加 embedding 配置，支持环境变量和 `omniagent.toml`
- 当前 hash embedding 保留为 fallback / 离线测试路径
- vector entry 记录 `embedding_provider`、`embedding_model`、`embedding_dimensions`、`embedding_version`
- 召回时避免新旧维度或 provider 不兼容导致误评分
- 没有配置真实 embedding 时，基础测试仍可运行
- 真实 embedding 失败时要有明确降级或错误记录，不能静默伪装为真实召回

已落地内容：

- `EmbeddingConfig` 配置模型，支持 `omniagent.toml` 与环境变量
- `embeddings.py` provider 层：
  - `LocalHashEmbeddingProvider`
  - `OpenAICompatibleEmbeddingProvider`
  - `LocalModelEmbeddingProvider`
  - `FallbackEmbeddingProvider`
- Gateway 根据配置装配 embedding provider，并注入 `VectorRecallEngine`
- vector entry 写入 `embedding_provider`、`embedding_model`、`embedding_dimensions`、`embedding_version`、`embedding_fallback_used`
- `local_model` 额外写入 `embedding_model_path`、`embedding_tokenizer_path`、`embedding_model_fingerprint`、`embedding_pooling`、`embedding_normalize`、`embedding_max_tokens`
- 召回时按 provider/model/dimensions/version 判断向量兼容性，不兼容时跳过 vector score，保留 lexical/concept 分数
- 真实 embedding 配置缺失或调用失败时降级到 local hash，并在 metadata / match_details 记录 fallback 与错误原因
- 新增单元测试 `tests/test_embeddings.py`

阶段 2 验证：

- `poetry run pytest tests/test_embeddings.py tests/test_vector_recall.py`：10 passed
- `poetry run ruff check src/omniagent/embeddings.py src/omniagent/vector_recall.py src/omniagent/config.py src/omniagent/memory.py src/omniagent/gateway.py tests/test_embeddings.py tests/test_vector_recall.py`：通过
- 阶段 2 当时全量验证 `poetry run pytest`：388 passed
- `poetry run ruff check .`：通过
- `python3 scripts/run_real_world_evals.py --check-only`：20 scenarios validated

建议环境变量：

- `OMNIAGENT_EMBEDDING_PROVIDER`
- `OMNIAGENT_EMBEDDING_MODEL`
- `OMNIAGENT_EMBEDDING_MODEL_PATH`
- `OMNIAGENT_EMBEDDING_TOKENIZER_PATH`
- `OMNIAGENT_EMBEDDING_API_BASE`
- `OMNIAGENT_EMBEDDING_API_KEY`
- `OMNIAGENT_EMBEDDING_DIMENSIONS`
- `OMNIAGENT_EMBEDDING_POOLING`
- `OMNIAGENT_EMBEDDING_NORMALIZE`
- `OMNIAGENT_EMBEDDING_MAX_TOKENS`
- `OMNIAGENT_EMBEDDING_TIMEOUT`
- `OMNIAGENT_EMBEDDING_MAX_RETRIES`
- `OMNIAGENT_EMBEDDING_ENABLED`

### 阶段 3：评测诊断与归因升级

状态：已完成。

目标：让真实评测报告能区分 Agent 缺陷、模型能力限制、召回问题和测试预期问题。

要求：

- `result.json` 增加 completion evaluation 摘要
- `diagnostics.md` 增加 completion evaluation、embedding provider、failure attribution 证据
- `failure_layer` 扩展为阶段化归因：粗分类 + completion evaluation + 人工/自动分析结论
- 增加 `model_capability` / `memory_retrieval` / `completion_evaluation` 等断言能力
- 评测修复流程明确：不能只修当前错误，必须判断整体架构或流程是否合理

已落地内容：

- `result.json` 增加 `failure_attribution`
- `evidence` 增加：
  - `completion_evaluations`
  - `vector_matches`
  - `vector_memory_entries`
  - `embedding_summary`
- `diagnostics.md` 增加：
  - `Failure Attribution`
  - `Completion Evaluation`
  - `Embedding And Vector Recall`
- `_diagnose` 可根据 completion evaluation 产出的 `primary_failure_layer` 归因为：
  - `model_capability`
  - `memory_retrieval`
  - `completion_evaluation`
  - `agent_flow`
  - `planner_or_skill_selection`
  - `tool_or_skill`
  - `test_expectation`
- 评测断言新增：
  - `required_completion_failure_layer_any`
  - `forbid_model_capability_suspected`
  - `required_embedding_provider_any`
  - `forbid_embedding_fallback`
  - `forbid_embedding_errors`
- 新增回归测试 `tests/test_real_world_eval_diagnostics.py`

阶段 3 增量验证：

- `poetry run pytest tests/test_real_world_eval_diagnostics.py`：9 passed
- `poetry run ruff check scripts/run_real_world_evals.py tests/test_real_world_eval_diagnostics.py`：通过

### 阶段 4：复杂真实场景评测

状态：已完成。`REAL-EVAL-006` 继续作为复杂回归集维护。

目标：在前置能力具备后，再启动 `REAL-EVAL-006`。

候选场景：

- 需要真实 embedding 的跨任务语义召回
- 多轮复杂指代和目标切换
- 工具成功但最终任务不满足用户目标
- 工具失败后多路径恢复
- Planner 与 completion evaluator 意见冲突
- 模型能力不足但 Agent 流程正确的可记录样例
- 长上下文与召回裁剪组合场景

已启动内容：

- `scripts/run_real_world_evals.py` 新增 `--include-complex`
- `evals/real_world/scenarios.json` 新增首批 `REAL-EVAL-006` 复杂场景，默认通过 `requires: ["complex"]` 跳过：
  - `semantic_memory_recall_incident_brief`
  - `chat_nested_reference_revision`
  - `completion_detects_incomplete_requirements`
- 2026-05-20 追加 5 个复杂专项场景：
  - `context_budget_many_memories_keeps_relevant_matches`
  - `chat_reference_survives_status_and_correction`
  - `config_missing_nested_key_fails_with_tool_layer`
  - `archive_extract_rejects_traversal_entry`
  - `completion_overrides_successful_wrong_file_payload`
- runner 支持 `setup_vector_memory`，用于在隔离 workspace 中按当前 embedding provider 写入种子 vector memory
- runner 支持 `setup_archives`，用于生成带原始 entry name 的 zip 测试输入，包括路径穿越防护场景
- 新增断言：
  - `min_vector_match_count`
  - `required_vector_match_entry_any`
  - `require_vector_compatible`
- 2026-05-21 修复首轮复杂专项初跑暴露的问题：
  - 路由状态查询规则补充 summarize/report 等任务型信号，避免上下文里的“status question”误路由。
  - 显式 `executor=file` + `payload` 直接生成确定性单节点计划，payload 作为权威工具输入。
  - `builtin.data.config` 明确报告任务直接生成确定性 config report 计划，避免真实 Planner provider 超时污染评测。
  - completion evaluator 节点证据纳入 `input_payload`，并归一化 `model_capability_suspected` 与 `primary_failure_layer` 的一致性。

阶段 4 当前验证：

- `poetry run python scripts/run_real_world_evals.py --check-only`：38 scenarios validated
- `poetry run python -m pytest tests/test_planner_decision.py tests/test_completion_evaluation.py tests/test_interaction_router.py tests/test_real_world_eval_diagnostics.py`：38 passed
- `poetry run ruff check src/omniagent/interaction.py src/omniagent/planner.py src/omniagent/completion_evaluator.py src/omniagent/models.py tests/test_interaction_router.py tests/test_planner_decision.py tests/test_completion_evaluation.py scripts/run_real_world_evals.py`：通过
- `poetry run python scripts/run_real_world_evals.py --include-complex --scenario context_budget_many_memories_keeps_relevant_matches --scenario chat_reference_survives_status_and_correction --scenario config_missing_nested_key_fails_with_tool_layer --scenario archive_extract_rejects_traversal_entry --scenario completion_overrides_successful_wrong_file_payload --verbose`：`eval_runs/20260520_165848/`，5 passed / 0 failed / 0 skipped

阶段 4 最终归档验证：

- `poetry run python scripts/run_real_world_evals.py --check-only`：38 scenarios validated
- `poetry run python -m pytest tests/`：456 passed
- `poetry run ruff check src/ tests/ scripts/run_real_world_evals.py`：通过
- 默认非 Git 基线：`eval_runs/20260521_073254/`，27 passed / 0 failed / 11 skipped
- 复杂矩阵：`eval_runs/20260521_092801/`，8 passed / 0 failed / 0 skipped
- Git 场景：`git_status_readonly_report` 按用户要求未执行

最终修复摘要：

- Planner 增加确定性文件 / JSON / config / browser payload 计划，覆盖明确结构化任务并降低真实 provider 波动。
- 多轮 continue fork 仅用本轮 continuation 作为新 goal / expected_output，避免旧任务目标污染完成度评估。
- `builtin.file.basic` 与 file executor append 补分隔换行，避免追加内容粘连。
- task-level Critic LLM 失败时保留工具执行结果并输出 fallback verdict，避免 CLI 因 provider 波动出现空 stdout。
- 复杂指代支持 `first` / `second` 历史文件、替换行后追加、源文件读取后写报告和字段报告直接写入。

## 验收标准

`CAP-EVAL-001` 完成必须满足：

1. completion evaluation artifact 能在任务目录中稳定生成。
2. 默认测试环境不依赖真实 embedding，仍使用 hash fallback 通过单元测试。
3. 配置真实 embedding 后，vector entry 能记录 provider/model/dimensions，并参与召回评分。
4. 真实评测 diagnostics 能展示 completion evaluation 和失败归因证据。
5. 至少一个评测场景能明确归因为 `model_capability` 或证明该归因路径可用。
6. `python3 scripts/run_real_world_evals.py --check-only` 通过。
7. 相关单元测试通过。
8. `poetry run ruff check .` 通过。
9. `ROADMAP.md` / `issue.md` / `REAL_WORLD_EVAL_PLAN.md` 更新完成状态。

## 非目标

- 不在本阶段引入外部向量数据库
- 不实现多模型自动编排
- 不把模型隐藏思维链写入日志或报告
- 不用真实账号、生产数据或敏感凭据构造评测
- 不为了让复杂场景通过而降低断言或跳过失败

## 影响范围

预计会涉及：

- `config.py`：embedding 配置
- `vector_recall.py` / `memory.py` / `hybrid_memory.py`：真实 embedding 与 fallback 兼容
- `critic.py` 或新增 `completion_evaluator.py`：完成度评估
- `models.py`：完成度评估数据模型
- `runtime.py` / `execution_engine.py`：任务结束后评估节点接入
- `workspace.py`：completion artifact 落盘
- `scripts/run_real_world_evals.py`：诊断和断言升级
- `evals/real_world/scenarios.json`：复杂场景新增
- `CONFIGURATION.md` / `ARCHITECTURE.md` / `README.md`：配置和架构文档同步
