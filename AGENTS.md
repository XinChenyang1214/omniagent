# OmniAgent 项目规则

## 适用范围

本文件是 OmniAgent 仓库的项目级规则，适用于当前目录及其子目录。

如果更深目录出现新的 `AGENTS.md`、`AGENT.md` 或 `AGENT.override.md`，以更近路径的模块规则补充本文件。

## 项目定位

OmniAgent 当前代码基础是一个以 task 为中心的本地嵌入式 Agent Runtime；当前产品目标是按 `docs/product-architecture.md` 重开发为以学习循环为中心的个人 Agent OS：

- 单用户、单机、CLI 优先
- 面向开发者和技术用户，可调试、可审计、可演化
- Python `>=3.11,<4.0`
- Poetry 管理
- 轻量本地运行时：stdlib 核心 + 默认本地 embedding 推理依赖 + OpenAI 兼容 LLM
- 默认离线运行，除 LLM 调用和显式授权网络任务外不访问外部网络

当前代码层面已完成三阶段 MVP、P0 补丁轮、`BASECAP-001`、`BASECAP-002`、`REAL-EVAL-005`、`CAP-EVAL-001` 和 `META-009` 首轮落地结果；这些作为旧实现基础和迁移输入。现阶段主线切换为 `META-010` Agent usability and learning loop MVP：Phase 1 Agent state、Phase 2 Experience records、Phase 3 Procedure memory、Phase 4 Habit fast path、Phase 5 Skill promotion、Phase 6 Learning feedback 和 Phase 7 Learning correction 已实现并接入真实评测证据；下一步进入更深的 learning-loop 治理增强和更广的稳定 habit / skill promotion 回归。

## 权威文档

优先按以下顺序理解项目现状和文档状态：

1. `README.md`：入口文档、能力概览和快速开始
2. `docs/doc-registry.md`：文档类型、生命周期状态、权威边界和替代关系
3. `docs/product-architecture.md`：目标产品架构、学习闭环和后续产品路线取舍
4. `docs/workstreams/active/AGENT_USABILITY_LEARNING_LOOP_MVP.md`：当前 `META-010` 重开发专项和验收矩阵
5. `docs/specs/agent-state.md`：Agent state 最小数据契约和验收标准
6. `docs/specs/experience-record.md`：任务经验记录 schema、写入时机和 learning gate
7. `docs/specs/procedure-memory.md`：procedure / habit 生命周期和升级规则
8. `docs/specs/skill-promotion.md`：habit -> skill promotion gate、lineage、contract 和权限规则
9. `docs/specs/learning-feedback.md`：任务结束学习反馈输出契约
10. `docs/specs/learning-correction.md`：复用失败、降级冻结、解释、复审和恢复证据契约
11. `ARCHITECTURE.md`：旧实现架构、模块职责、执行流程和迁移输入
12. `ROADMAP.md`：路线图和当前优先级
13. `issue.md`：未关闭问题与近期已关闭记录
14. `REAL_WORLD_EVAL_PLAN.md`：真实场景自主评测闭环
15. `CONFIGURATION.md`：配置项和环境变量参考
16. `docs/archive/pre-product-redesign/`：重开发前旧阶段过程文档归档
17. `CLAUDE.md`：其他 AI 编程助手的项目指南

代码是当前实现事实来源；目标产品方向以 `docs/product-architecture.md` 和 `META-010` workstream 为准。文档与代码冲突时，应区分“旧实现事实”和“新目标契约”，不要把旧实现误当成新架构要求。

## 目录与模块

- `src/omniagent/`：核心源码
- `src/omniagent/prompts/`：LLM prompt 模板
- `src/omniagent/builtin_skills/`：内置 skill 包
- `tests/`：pytest 测试
- `scripts/`：项目自动化脚本
- `evals/real_world/`：真实场景评测定义
- `workspace/`：默认运行时 workspace，包含任务产物、记忆、线程、LLM 调用日志
- `skills/generated/`：自动生成 skill 产物
- `data/`、`artifacts/`：本地运行产生的数据库和日志

修改时默认按目录作为模块边界。跨模块修改需要能说明原因、影响范围和验证方式。

## 核心模块

- `gateway.py`：`RuntimeGateway`，装配并协调所有子系统
- `runtime.py`：任务执行主流程，串联 Planner、ExecutionEngine、Critic
- `interaction.py`：多轮对话意图识别和路由
- `planner.py`：LLM 驱动 TaskGraph 规划
- `execution_engine.py`：DAG 节点执行、retry、degrade、replan、checkpoint
- `dispatcher.py`：skill / executor 选择与执行
- `skill_registry.py`：skill manifest 预过滤与 action/权限/历史信号打分
- `critic.py`：执行结果评估
- `completion_evaluator.py`：任务完成度评估与失败归因
- `response_generator.py`：自然语言回复生成
- `storage.py`：SQLite 持久化
- `graph_memory.py`、`memory.py`、`hybrid_memory.py`、`vector_recall.py`、`embeddings.py`：记忆、召回与 embedding
- `skill_lifecycle.py`、`skill_auto_generation.py`、`workflow_compiler.py`：skill 生命周期、自动生成和 workflow 沉淀
- `permissions.py`、`sandbox.py`：权限和沙箱
- `llm/`：OpenAI Chat Completions 兼容 LLM 适配层

当前 active builtin skill 包括：

- `builtin.file.basic`
- `builtin.python.basic`
- `builtin.browser.basic`
- `builtin.data.json`
- `builtin.datetime.format`
- `builtin.data.csv`
- `builtin.http.client`
- `builtin.shell.safe`
- `builtin.data.sqlite`
- `builtin.text.markdown`
- `builtin.vcs.git`
- `builtin.web.html`
- `builtin.archive.basic`
- `builtin.data.config`

## 常用命令

安装依赖：

```bash
poetry install
```

全量测试：

```bash
poetry run python -m pytest tests/
```

单文件测试：

```bash
poetry run python -m pytest tests/test_mvp_runtime.py
```

单测试用例：

```bash
poetry run python -m pytest tests/test_mvp_runtime.py::TestMVPRuntime::test_method_name
```

不要用 `python -m unittest discover -s tests` 跑本项目测试；`tests/conftest.py` 会注册 `test` LLM provider，unittest 不会自动加载它，会产生大量误报。

Lint：

```bash
poetry run ruff check src/ tests/ scripts/run_real_world_evals.py
```

Format：

```bash
poetry run ruff format src/ tests/
```

CLI 健康检查：

```bash
poetry run omniagent --workspace ./workspace llm check
```

执行任务：

```bash
poetry run omniagent --workspace ./workspace run "列出工作区文件" \
  --executor file \
  --payload '{"action":"list","path":"."}'
```

进入对话：

```bash
poetry run omniagent --workspace ./workspace chat repl --max-permission L2
```

真实场景评测：

```bash
poetry run python scripts/run_real_world_evals.py --check-only
poetry run python scripts/run_real_world_evals.py --list
poetry run python scripts/run_real_world_evals.py
```

网络和 L4 shell 场景默认跳过，需要显式开启：

```bash
poetry run python scripts/run_real_world_evals.py --include-network
poetry run python scripts/run_real_world_evals.py --include-l4
poetry run python scripts/run_real_world_evals.py --include-complex
```

复杂场景中的 `local_model` embedding 评测必须使用 Poetry 环境运行，避免系统 `python3` 缺少 `onnxruntime` / `tokenizers` / `numpy` 时降级到 `local_hash` 并造成误判。

## 配置规则

- 项目根目录读取 `omniagent.toml` 或 `.omniagent.toml`
- 敏感信息必须通过环境变量注入
- API Key 使用 `OMNIAGENT_LLM_API_KEY`，并兼容 `OPENAI_API_KEY` / `ARK_API_KEY`
- 不在 `omniagent.toml`、文档、测试快照、日志或提交信息中写入真实 key
- 真实 LLM 调用默认会产生 token 消耗；评测和手工运行前应确认 provider、model、api_base 指向预期环境

常用环境变量：

- `OMNIAGENT_LLM_PROVIDER`
- `OMNIAGENT_LLM_MODEL`
- `OMNIAGENT_LLM_API_BASE`
- `OMNIAGENT_LLM_API_KEY`
- `OMNIAGENT_EMBEDDING_ENABLED`
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
- `OMNIAGENT_TASK_TIMEOUT`
- `OMNIAGENT_SQLITE_PATH`
- `OMNIAGENT_LOG_PATH`
- `OMNIAGENT_SANDBOX_AUTHORIZED_PATHS`

## 测试与评测

代码改动后优先运行与改动范围最相关的测试，再按影响范围决定是否跑全量测试。

能力层、Planner、Dispatcher、ExecutionEngine、权限、沙箱、skill registry 或 builtin skill 改动后，优先跑：

```bash
poetry run python -m pytest tests/
poetry run ruff check src/ tests/ scripts/run_real_world_evals.py
```

当前复杂场景评测已归档为旧实现回归集。`META-010` 期间真实场景评测应继续维护旧基线，同时新增 learning-loop evidence，验证 Agent state、ExperienceRecord、procedure / habit 命中、validation evidence、skill promotion lineage、learning feedback 和 learning correction。运行后重点查看：

- `eval_runs/<run_id>/summary.md`
- `eval_runs/<run_id>/cases/<scenario_id>/result.json`
- `eval_runs/<run_id>/workspaces/<scenario_id>/tasks/<task_id>/plan/`
- `eval_runs/<run_id>/workspaces/<scenario_id>/tasks/<task_id>/plan/policy_decision.json`
- `eval_runs/<run_id>/workspaces/<scenario_id>/tasks/<task_id>/nodes/`
- `eval_runs/<run_id>/workspaces/<scenario_id>/tasks/<task_id>/critic/`
- `eval_runs/<run_id>/workspaces/<scenario_id>/tasks/<task_id>/completion/`
- `eval_runs/<run_id>/workspaces/<scenario_id>/tasks/<task_id>/llm_calls/`
- `eval_runs/<run_id>/workspaces/<scenario_id>/tasks/<task_id>/learning/`（如已实现）
- `eval_runs/<run_id>/workspaces/<scenario_id>/tasks/<task_id>/experience/`（如已实现）

评测暴露的问题应按失败层级归因：Router、Planner、skill selection、permission、executor、Critic、Responder、LLM provider 或场景配置。

测试或评测发现不合理行为时，不允许只盯着当前报错做局部补丁。必须同时判断：

- 这个错误是否暴露了架构边界、模块职责或执行流程的不合理
- 是否存在同类路径会重复触发相同问题
- 当前修复应该落在测试断言、prompt、skill contract、权限策略、dispatcher、executor、critic 还是更上层流程
- 是否需要把问题升级为 `issue.md` 中的 META / ISSUE，而不是只修一个样例

只有确认问题确实是局部实现缺陷时，才做局部修复；否则应先调整流程或架构层约束，再补回归测试。

## 明确目标的自主收敛机制

当用户给出明确目标、修复方向、验收标准或“持续测试修复直到达标”类指令时，默认进入自主收敛流程，不因缺少逐步授权而停在方案说明。

自主收敛流程应按以下闭环执行：

1. 将目标整理为可验证的验收标准；用户未明确验收标准时，根据项目现有文档、测试和评测体系推导最小可测标准，并在结果中说明假设。
2. 快速定位相关代码、测试、评测场景、历史任务产物和文档，判断问题属于架构流程、模块职责、能力边界、prompt / skill contract、权限策略、executor 行为、评测配置还是细节实现。
3. 优先补充或生成与目标相似的回归测试、真实场景评测或诊断用例，避免只针对单个样例做不可泛化补丁。
4. 运行相关测试或评测，读取失败证据，按失败层级归因。
5. 做最小必要修复，并同步更新受影响的文档、评测场景或 issue 记录。
6. 重新运行相关验证；若仍未达标，继续归因和修复，直到达成验收标准、遇到权限/外部依赖阻塞，或发现需要用户重新决策的架构取舍。

自主收敛过程中的边界：

- 可以自主修改代码、测试、项目文档和本地评测场景。
- 涉及真实 LLM 调用、联网、可能产生费用、外部系统写入、真实账号、生产数据、删除或不可逆操作时，必须先确认。
- 不通过跳过测试、降低断言、删除失败场景或掩盖错误来制造达标。
- 若发现问题具有重复性或系统性，应优先修复流程、契约或架构约束，再补具体回归测试。
- 若目标过大，应维护阶段性检查清单，持续推进到当前可验证边界，并说明剩余阻塞和下一轮收敛入口。

## 文档维护

以下变化需要同步文档：

- 公开命令、配置项或环境变量变化
- 运行时 workspace 结构变化
- TaskGraph、skill contract、权限模型或生命周期行为变化
- builtin skill 增删或 action contract 变化
- 路线图状态、issue 状态或真实评测流程变化

路线图状态更新写入 `ROADMAP.md`，问题归档和新问题写入 `issue.md`。

## 运行产物

以下目录通常是本地运行产物，不应作为源码事实来源：

- `workspace/tasks/`
- `workspace/threads/`
- `workspace/MEMORY/`
- `workspace/WORKFLOWS/`
- `eval_runs/`
- `artifacts/`
- `data/`
- `__pycache__/`
- `.pytest_cache/`

可以清理明显的 Python 缓存和评测临时产物；不要删除用户明确保留的 workspace 任务记录、生成 skill 或数据库，除非当前任务要求清理。
