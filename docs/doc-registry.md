# 文档登记表

Last reviewed：2026-06-16

本登记表记录文档治理状态。`implementation_status` 反映本次整理时文档中记载的项目状态；除非特别说明，它不是一次新的代码审计结论。

## 状态说明

- `active`：当前有效文档。
- `confirmed`：已确认，可作为当前专项的工作契约。
- `completed`：已完成工作的过程文档，保留为历史证据。
- `superseded`：已被新文档替代，不再作为当前实现契约。
- `archived`：归档历史资料，不再作为当前实现契约。
- `draft`：讨论或想法记录，不作为实现契约。
- `not_applicable`：不直接对应实现状态，通常是索引、规则或参考文档。
- `not_started`：目标或设计已记录，但尚未进入实现。
- `partial`：曾部分实现，但不代表当前新主线已完成。
- `implemented_unverified`：文档描述了已实现行为，但本次整理没有重新跑验证。
- `verified`：文档自身记录了该专项的验证证据。
- `implementing`：当前仍有未完成工作。
- `deprecated`：旧实现或旧工作流已被新主线取代，不再继续推进。

## 顶层文档

| Path | Document type | Workstream | Document status | Implementation status | Authority scope | Replacement |
|---|---|---|---|---|---|---|
| `README.md` | current-state / entry | project overview | active | not_applicable | 公开入口、安装、快速开始、workspace 结构、当前状态摘要 | none |
| `docs/README.md` | current-state / operations | document governance | active | not_applicable | 文档阅读顺序和更新规则 | none |
| `docs/doc-registry.md` | current-state / registry | document governance | active | not_applicable | 文档权威边界、生命周期状态和替代关系 | none |
| `docs/product-architecture.md` | architecture / product strategy | smart learning agent redesign | active | not_started | 目标产品架构、设计思想、学习闭环和后续路线取舍 | none |
| `docs/workstreams/active/AGENT_USABILITY_LEARNING_LOOP_MVP.md` | implementation / architecture | `META-010` | confirmed | implementing | 按最新产品架构重开发的首个开发契约、验收矩阵和阶段计划 | none |
| `docs/specs/agent-state.md` | spec | `META-010` / Agent state | confirmed | implemented | `AgentProfile`、`UserPreference`、`ProjectState`、`CurrentGoal` 的最小数据契约和验收标准 | none |
| `docs/specs/experience-record.md` | spec | `META-010` / Experience records | confirmed | implemented | 任务经验记录的 schema、写入时机、evidence 和 promotion gate 边界 | none |
| `docs/specs/procedure-memory.md` | spec | `META-010` / Procedure memory | confirmed | implemented | procedure / verified procedure / habit 的生命周期、存储、检索、升级规则和 Phase 4 habit fast path；skill promotion 归 Phase 5 | none |
| `docs/specs/skill-promotion.md` | spec | `META-010` / Skill promotion | confirmed | implemented | stable habit -> skill promotion 的 gate、lineage、contract、权限和验证规则 | none |
| `docs/specs/learning-feedback.md` | spec | `META-010` / Learning feedback | confirmed | verified | 任务结束后向用户展示 experience / procedure / habit 学习状态、复用判断和治理入口的最小输出契约；真实评测 evidence index / dashboard 已接入 `learning_feedbacks` | none |
| `docs/specs/learning-correction.md` | spec | `META-010` / Learning correction | confirmed | verified | procedure / habit 复用失败、过期或用户修正后的纠错、降级、冻结、解释、复审和恢复证据契约；真实评测 evidence index / dashboard 已接入 `learning_corrections` | none |
| `AGENTS.md` | operations / project rules | agent collaboration | active | not_applicable | 项目规则、常用命令、安全边界、权威文档顺序 | none |
| `ARCHITECTURE.md` | current-state / architecture | legacy core runtime | active | implemented_unverified | 旧实现架构和迁移输入；不再作为目标产品架构 | `docs/product-architecture.md` for target architecture |
| `CONFIGURATION.md` | spec / operations | runtime configuration | active | implemented_unverified | 配置文件、环境变量、provider、权限和沙箱选项 | none |
| `ROADMAP.md` | roadmap | project planning | active | implementing | 当前优先级、已完成里程碑和待排期方向 | none |
| `issue.md` | change-fix / issue tracker | project planning | active | implementing | 开放 META / ISSUE、已关闭问题、真实评测状态 | none |
| `REAL_WORLD_EVAL_PLAN.md` | validation / operations | real-world evaluation | active | implemented_unverified | 真实评测 runner 流程、场景分类、diagnostics、failure attribution、安全边界 | none |
| `CLAUDE.md` | operations / assistant guide | AI coding assistant context | active | not_applicable | 面向其他 AI 编程助手的项目摘要、命令和约定 | partially covered by `AGENTS.md` and this registry |

## 模块与区域文档

| Path pattern | Document type | Workstream | Document status | Implementation status | Authority scope | Replacement |
|---|---|---|---|---|---|---|
| `scripts/README.md` | operations | real-world evaluation tooling | active | implemented_unverified | `scripts/run_real_world_evals.py` 用法和证据产物 | none |
| `sandbox/README.md` | spec / operations | sandboxing | active | implemented_unverified | 沙箱职责与边界 | none |
| `memory/files/README.md` | spec | file memory | active | implemented_unverified | 文件记忆模块行为 | none |
| `memory/graph/README.md` | spec | graph memory | active | implemented_unverified | 图记忆模块行为 | none |
| `memory/vector/README.md` | spec | vector memory | active | implemented_unverified | 向量记忆模块行为 | none |
| `skills/builtin/README.md` | spec / inventory | builtin skills | active | implemented_unverified | builtin skill 包结构和约束 | none |
| `skills/registry/README.md` | spec / inventory | skill registry | active | implemented_unverified | skill registry 包结构和约束 | none |
| `skills/generated/README.md` | operations / inventory | generated skills | active | not_applicable | 自动生成 skill 产物位置和用途 | none |
| `shared/skills/README.md` | inventory | shared skill assets | active | not_applicable | 共享 skill 资源位置 | none |
| `agents/main/skills/README.md` | inventory | main agent skills | active | not_applicable | main agent skill 位置 | none |
| `src/omniagent/builtin_skills/*/README.md` | spec / operations | builtin skill contract | active | implemented_unverified | 单个 builtin skill 的用途、actions、权限和示例 | none |
| `src/omniagent/builtin_skills/*/tests/README.md` | validation | builtin skill tests | active | implemented_unverified | 单个 builtin skill 的测试意图和覆盖说明 | none |
| `src/omniagent/prompts/*.md` | spec | LLM prompt contracts | active | implemented_unverified | Planner、Critic、Router、Responder、Skill generation、Knowledge query、Completion evaluation 的 prompt 输入输出契约 | none |

## 归档文档

| Path | Document type | Workstream | Document status | Implementation status | Authority scope | Replacement |
|---|---|---|---|---|---|---|
| `docs/archive/pre-product-redesign/RUNTIME_ORCHESTRATION_PERFORMANCE_UPGRADE.md` | implementation / architecture | `META-009` | superseded | deprecated | Runtime fast path、Planner manifest budget、policy artifact 的旧专项记录；只作迁移参考 | `docs/workstreams/active/AGENT_USABILITY_LEARNING_LOOP_MVP.md` |
| `docs/archive/pre-product-redesign/COMPLEX_CAPABILITY_EVAL_UPGRADE.md` | implementation / validation | `CAP-EVAL-001` / `REAL-EVAL-006` | archived | verified | Completion evaluation、failure attribution、真实 embedding diagnostics、复杂评测矩阵历史证据 | `docs/workstreams/active/AGENT_USABILITY_LEARNING_LOOP_MVP.md` for new validation direction |
| `docs/archive/pre-product-redesign/BASIC_CAPABILITIES_UPGRADE.md` | implementation / validation | `BASECAP-001` | archived | verified | JSON、datetime、CSV、HTTP、safe shell 基础能力升级历史记录 | `docs/workstreams/active/AGENT_USABILITY_LEARNING_LOOP_MVP.md` for new capability direction |
| `docs/archive/pre-product-redesign/BASIC_CAPABILITIES_V1_UPGRADE.md` | implementation / validation | `BASECAP-002` | archived | implemented_unverified | SQLite、Markdown、Git readonly、HTML、Archive、Config builtin skill 首批历史记录 | `docs/workstreams/active/AGENT_USABILITY_LEARNING_LOOP_MVP.md` for new capability direction |
| `docs/archive/pre-product-redesign/IDEAS.md` | exploration | agent memory and learning model | archived | not_applicable | 早期概念捕获，已被正式产品架构吸收 | `docs/product-architecture.md` |

## 整理发现

- 本次整理前没有集中式 registry，文档权威边界分散在 `README.md`、`AGENTS.md`、`ROADMAP.md` 和 `issue.md` 中。
- `README.md` 和 `AGENTS.md` 曾把 `CAP-EVAL-001` 写成仍处于最高优先级；之后项目一度以 `META-009` 为活跃主线，现已切换到 `META-010`。
- 2026-06-11 起，项目按 `docs/product-architecture.md` 重新开发，当前 active workstream 切换为 `META-010`。
- 旧阶段开发文档已归档到 `docs/archive/pre-product-redesign/`，不再作为当前实现契约。
- `ARCHITECTURE.md` 保留为旧实现 current-state / migration input；目标产品架构以 `docs/product-architecture.md` 为准。
- 2026-06-12 已补齐 `META-010` Phase 1-5 specs：Agent state、Experience records、Procedure memory、Skill promotion。
- 2026-06-12 已实现 Phase 1 Agent state、Phase 2 Experience records、Phase 3 Procedure memory、Phase 4 Habit fast path 和 Phase 5 Skill promotion；下一步进入 learning-loop 真实评测与治理增强。
- 2026-06-15 新增并验证 Learning feedback spec，用于把已有学习闭环状态呈现在 task-ending reply 中，提高用户可感知性；`learning_loop_feedback_sequence` 已把连续任务学习反馈接入 evidence index / dashboard。
- 2026-06-16 新增并验证 Learning correction spec，用于约束复用失败后的降级、冻结、解释、fresh evidence 复审恢复和 dashboard evidence；`learning_loop_correction_recovery` 已把纠错闭环接入 `learning_corrections` evidence kind。

## 待确认治理项

- 是否新增 `docs/validations/` 收纳跨专项评测证据索引。
